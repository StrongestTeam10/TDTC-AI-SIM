# TDTC-AI-SIM

전통시장 AI 안전탐지 관제 솔루션 — 디지털 트윈 시뮬레이션 엔진

Python · FastAPI · Mesa · psycopg2 · OpenAI(RAG·보고서) · Gemini(공문 분석)

> 변경 이력은 [CHANGELOG.md](./CHANGELOG.md) 참고

---

## 빠른 시작

```bash
pip install -r requirements.txt
cp .env.example .env      # 값 채우기 (아래 환경변수 표 참고)
uvicorn app.main:app --reload --port 8000
```

API 문서: http://localhost:8000/docs

| 확인 | 방법 |
|---|---|
| 서버 살아있음 | `GET /health` → `{"status":"UP"}` |
| **DB 연결** | `GET /health/db` |
| 보고서 엔진 상태 | `GET /simulation/reports/status` |

> DB 접속이 안 되면 기동은 되지만 시뮬레이션 요청이 500으로 떨어집니다. 먼저 `/health/db`를 확인하세요.

---

## 아키텍처 상 위치

```text
[TDTC-AI-FE]  →  [TDTC-AI-BE]  →  [TDTC-AI-SIM (이 저장소)]
 React/S3        Spring Boot :8080      FastAPI + Mesa :8000
      │                │                        │
      └────────────────┴──►  [PostgreSQL]  ◄────┘
```

- Spring Boot의 `SimulationEngineClient`가 이 서비스를 REST로 호출합니다. **FE가 직접 부르지 않습니다.**
- 외부에 노출하지 않고 내부 네트워크(VPC)에서만 접근하는 것을 전제로 합니다.
- 시뮬레이션은 DB를 직접 읽지만, **보고서 생성은 DB를 쓰지 않습니다**(BE가 필요한 행을 JSON으로 실어 보냄).

> ⚠️ 포트 주의: **SIM은 8000**, CCTV AI 파이프라인은 **8088**입니다. FE가 8000으로 WebSocket을 열면 SIM에 그 라우트가 없어 Starlette가 accept 없이 닫고, uvicorn 로그에는 `403 Forbidden`으로 찍혀 권한 문제로 오인하기 쉽습니다.

---

## API 한눈에

| 엔드포인트 | 용도 |
|---|---|
| `POST /simulate/snapshot` | **파이프라인 A** — 센서 실측값으로 현재 상태 스냅샷 (MIRROR 모드) |
| `POST /simulate/predict` | **개입 전(Before)** — 현행 유지 상태로 앞을 내다봄 |
| `POST /simulate/scenario` | **개입 후(After)** — 사용자 지정 What-if 실험 (SCENARIO 모드) |
| `POST /policy/analyze` | 공문 텍스트·문서를 LLM으로 읽어 시나리오 파라미터로 변환 |
| `POST /simulation/reports/file` | 정책 보고서 생성 → **DOCX 파일 자체**를 응답 (운영 경로) |
| `POST /simulation/reports` | 같은 로직, **저장 경로를 JSON**으로 응답 (로컬 디버깅용) |
| `GET /simulation/reports/{id}/docx` · `/analysis` | 생성된 DOCX · 분석 JSON 조회 |
| `POST /simulation/reports/mock/{name}` | Mock 데이터 기반 보고서 생성 (SIM 단독 확인용) |
| `GET /health` · `/health/db` · `/simulation/reports/status` | 상태 확인 |

### Before / After 비교

FE의 시뮬레이션 비교 화면은 `predict`와 `scenario`를 **동시에** 호출해 나란히 그립니다.

| | Before (`/simulate/predict`) | After (`/simulate/scenario`) |
|---|---|---|
| 성격 | 비교 기준 (현행 유지) | 개입 반영 |
| 공통 입력 | 유입 인원 · 스텝 수 · 화재 이벤트 · 현행 오브젝트/통로 정책 | |
| After 전용 | | 오브젝트 추가·삭제, 통로 제어, 게이트 개폐 |

두 실행이 **같은 시드(42)** 를 쓰므로, 개입을 하나도 넣지 않으면 프레임 단위로 완전히 동일한 결과가 나옵니다. 그래야 달라진 부분이 곧 개입의 효과가 됩니다.

---

## 시뮬레이션 모델

### 화재 이벤트 (구현 완료)

`EventTrigger`로 받은 화재를 등록하고, 매 스텝 생애주기에 따라 구역 위험도를 계산합니다.

```text
발화 ──► 연소 ──────────► 진압 ──► 복구 ──────► 정상
        발화 구역 75~100점        위험도가 선형으로 0까지 감쇠
        인접 구역은 홉당 절반씩    (유동인구 재유입 재개)
        감쇠(FIRE_SPREAD_DECAY)
```

| 필드 | 의미 |
|---|---|
| `triggerStep` | 발화 스텝 (1부터) |
| `burnSteps` | 연소 기간. FE는 `진압 스텝 - 발생 스텝`으로 계산해 보냅니다 |
| `recoverySteps` | 진압 후 위험도가 0까지 내려가는 기간 |
| `intensity` | 0.0~1.0. 발화 구역 점수를 `75 + 25 × intensity`로 정합니다 |

> ⚠️ `zoneId`가 실제 구역이 아니면 **그 화재는 조용히 무시됩니다**(`apply_event_triggers`). 등록은 됐는데 아무 일도 일어나지 않는 상태가 되므로, 호출 측에서 유효한 구역 ID를 보내야 합니다.
> ⚠️ `triggerStep`이 `steps`를 넘으면 400으로 거절합니다. 영원히 발동하지 않는 이벤트를 조용히 넘기면 "화재를 냈는데 반응이 없다"로 오해하게 됩니다.

**음향 이상(`acoustic_anomaly`)은 2026-07-23부로 완전히 제거되었습니다.** 이벤트 타입은 `fire` 하나뿐입니다.

### 그 외 개입 (구현 완료)

| 개입 | 처리 |
|---|---|
| 오브젝트 배치 | `apply_scenario_overrides` — 푸드트럭·행사존·휴게공간은 매력도 상승, 장애물은 통행 차단 |
| 통로 제어 | 같은 함수 — 폐쇄 / 개방 / 일방통행 |
| 게이트 개폐 | `apply_gate_closures` — 닫힌 출입구로는 대피할 수 없습니다 |

---

## 위험도 산출 근거

임의 가중치가 아니라 공인 기준에 근거합니다.

| 기준 | 값 | 출처 |
|---|---|---|
| 수용 한계 | 1인당 0.46 m² (≈ 2.17명/m²) | 행정안전부「다중운집인파사고 안전관리 가이드라인」(2024.9), NFPA 101 준용 |
| 위험 임계 | 5명/m² | G. Keith Still 군중안전 기준 |
| 참사 사례 | 5.6~6.6명/m² | 10.29 이태원 참사 당시 추정 밀집도 |
| 보행 유동 용량 | 통로 폭 1m 당 1.3명/초 | Fruin/SFPE 보행자 유동 이론 |

### 종합 점수 구성

| 지표 | 가중치 | 근거 |
|---|---|---|
| 밀집도 | 0.55 | 압사의 직접 원인 |
| 통로 병목 | 0.10 | 구역 인원의 대피 소요 시간 (5분 초과 시 최고점) |

레이더 기반 "이동 흐름"(0.20)과 음향 기반 "이상 음향"(0.15)은 센서 제거로 삭제됐습니다. 남은 두 지표에 재정규화가 적용되어 실질 반영 비율은 **84.6% : 15.4%** 입니다.

- **가중치 재정규화** — 데이터가 결측이면 그 가중치를 빼고 나머지로 100%를 재배분합니다. 이 처리가 없으면 결측 지표가 0점으로 반영되어, 밀집도가 아무리 높아도 상위 등급에 도달할 수 없습니다.
- **안전 오버라이드** — 밀집도 단독으로 임계를 넘으면 다른 지표와 무관하게 등급을 강제 상향합니다. 압사는 밀집도만으로 발생하므로 평균에 희석되면 안 됩니다.

### 검증 결과 (망원시장 3구역)

| 시나리오 | 인원 | 밀집도 | 1인당 면적 | 점수 | 등급 |
|---|---|---|---|---|---|
| 평시 | 110명 | 0.06명/m² | 15.7m² | 1.6 | low |
| 주말 오후 | 1,100명 | 0.68명/m² | 1.47m² | 15.6 | low |
| 축제 | 3,300명 | 2.04명/m² | 0.49m² | 46.7 | medium |
| 특정구역 병목 | 2,750명 | 4.25명/m² | 0.24m² | 81.0 | critical |

---

## 환경변수

`.env`에 넣습니다(Git 커밋 금지). 템플릿은 [`.env.example`](./.env.example).

### DB — 없으면 시뮬레이션이 동작하지 않습니다

| 변수 | 기본값 | 비고 |
|---|---|---|
| `SIM_DB_HOST` | **없음** | 비면 `init_pool()`이 명확한 에러로 막습니다 |
| `SIM_DB_PORT` | `5432` | |
| `SIM_DB_NAME` | `postgres` | |
| `SIM_DB_USER` | **없음** | Supabase **pooler**를 쓰면 `postgres.<project-ref>` 형식이어야 합니다 |
| `SIM_DB_PASSWORD` | **없음** | |
| `SIM_DB_SSLMODE` | `require` | |
| `SIM_DB_CONNECT_TIMEOUT` | `5` | 미지정 시 psycopg2가 무한 대기해 앱이 안 뜹니다 |

> `password authentication failed for user "postgres"`가 뜨면 `SIM_DB_USER`를 먼저 보세요. pooler 주소인데 계정이 `postgres`로만 되어 있으면 이 오류가 납니다.

### LLM

| 변수 | 기본값 | 용도 |
|---|---|---|
| `GEMINI_API_KEY` | 없음 | 공문 분석(`/policy/analyze`) |
| `GEMINI_MODEL` | **없음 (필수)** | 예: `gemini-flash-latest` |
| `OPENAI_API_KEY` | 없음 | 보고서 RAG 검색 · 본문 생성 |
| `OPENAI_MODEL` | `gpt-4.1-mini` | 보고서 본문 |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | PDF·질의 임베딩 |

> **`GEMINI_MODEL`에 기본값을 두지 않습니다.** 예전에는 `gemini-2.0-flash`가 코드에 박혀 있었는데 구글이 모델을 내리면서 `/policy/analyze`가 404 → 500으로 죽었습니다. 조용히 옛 모델로 떨어지는 것보다 설정 누락이 바로 드러나는 편이 낫다고 판단했습니다.
> 쓸 수 있는 모델은 `GET https://generativelanguage.googleapis.com/v1beta/models`로 확인하세요. 회전이 빠릅니다.

### 보고서 · 서버

| 변수 | 기본값 |
|---|---|
| `NARRATIVE_MODE` | `template` (`template` \| `openai`) |
| `NARRATIVE_STRICT` | `false` — LLM 오류 시 생성을 중단할지 |
| `RAG_MIN_VECTOR_SCORE` | `0.35` |
| `REPORT_OUTPUT_DIR` | `outputs` |
| `REPORT_VECTOR_INDEX_PATH` | `knowledge/vector_index.json` |
| `DOCX_FONT_NAME` | `맑은 고딕` |
| `KOREAN_FONT_PATH` | 자동 탐색 (Matplotlib 차트용) |
| `SIM_API_HOST` · `SIM_API_PORT` | `0.0.0.0` · `8000` |

---

## 정책 보고서 생성

```text
[FE]  시나리오 실행 → 결과 저장 → 사용자가 [보고서 생성]
        ↓  POST /api/simulation/reports        (BE)
[BE]  같은 시장의 현행안을 찾아 시장·구역·시나리오·결과를 JSON으로 조립
        ↓  POST /simulation/reports/file       (SIM · 이 저장소)
[SIM] 지표 비교 + 공공문서 RAG 검색 + LLM 본문 생성 + 차트 렌더
        ↓  DOCX 바이트 + X-Report-Title 헤더
[BE]  S3 업로드 → DB에 경로·제목 기록 → presigned URL
        ↓
[FE]  DOCX 다운로드
```

다시 받을 때는 BE의 `GET /api/simulation/reports/{scenarioId}/download`로 presigned URL만 재발급합니다. SIM을 다시 부르지 않습니다.

**BE가 `/reports` 대신 `/reports/file`을 쓰는 이유** — 전자가 돌려주는 경로는 SIM 컨테이너의 로컬 디스크(`outputs/`)라 재시작하면 사라지고, 인스턴스가 2대 이상이면 다른 인스턴스가 그 파일을 찾지 못합니다. 파일 자체를 받아 S3에 올려야 보관이 보장됩니다.

### 입력 계약 (`DbReportBundle`)

**보고서 생성에는 DB를 쓰지 않습니다.** BE가 필요한 행을 모두 읽어 JSON으로 보내고, SIM은 받은 것만으로 문서를 만듭니다. 스키마는 `app/schemas/report_db_models.py`.

| 필드 | 내용 |
|---|---|
| `report_meta` | `report_id`(경로에 쓰이므로 `[A-Za-z0-9_-]{1,100}`), 제목, 의사결정 질문 |
| `market`, `zones` | 시장 기본정보와 구역 목록 |
| `baseline_scenario`, `baseline_result` | **현행안** (`simbsln01m` / `simbsln01d`) |
| `scenario_rows`, `result_rows` | **대안 시나리오** (`simscnr01m` / `simrslt01d`), 최소 1건 |
| `density_timeseries_rows` | 시간대별 밀집도. BE에 대응 테이블이 없어 현재는 늘 비어 있음 |

현행안을 별도 필드로 둔 이유: 현행안과 시나리오는 **다른 테이블이라 ID가 각각 1부터 시작해 겹칠 수 있습니다.** 한 배열에 담으면 `scenario_id`로 둘을 구분할 수 없습니다.

### 위험 이벤트 대응 방안 절

화재가 상정된 시나리오에 한해 "7. 종합 검토 의견" 아래 **"위험 이벤트 대응 방안"** 절이 생성됩니다. 이벤트가 없으면 절 자체가 없습니다.

내용은 전달받은 값으로만 조립합니다 — 상정된 이벤트(구역·강도·건수·발동 스텝), 대피 인원, 최대 밀집 구역, 위험점수 변화. LLM 모드에서도 이 템플릿 결과를 바닥에 깔아, LLM이 키를 누락하거나 형식을 어겨도 절이 사라지지 않게 합니다.

예측 결과표(4절)의 "대피 인원"·"최대 밀집 구역" 열도 데이터가 있을 때만 생깁니다. 대피는 혼잡만으로도 발생하므로, 값이 하나라도 양수이거나 이벤트가 있으면 열을 만듭니다.

---

## 폴더 구조

```text
app/
├─ main.py            FastAPI 진입점
├─ config.py          환경설정 (.env 로드)
├─ api/
│  ├─ health.py       /health, /health/db
│  ├─ simulate.py     snapshot · predict · scenario
│  ├─ reports.py      보고서 생성·다운로드·분석 조회
│  └─ policy.py       공문 LLM 분석
├─ schemas/           요청·응답 스키마 (BE DTO와 camelCase 일치)
├─ services/
│  └─ policy_service.py   Gemini 멀티모달 공문 분석 (PDF·이미지·DOCX)
├─ db/
│  ├─ connection.py   커넥션 풀 (지연 초기화)
│  ├─ repository.py   DB 조회 — Mesa 모델은 SQL을 직접 쓰지 않음
│  └─ report_adapter.py   BE DTO → 보고서 내부 모델 변환
├─ simulation/
│  ├─ space.py        GeoJSON 파싱, 위경도 ↔ 로컬 미터 좌표
│  ├─ gridspace.py    이동 가능 영역 격자
│  ├─ placement.py    구역 폴리곤 내 배치
│  ├─ risk.py         위험도 스코어링
│  ├─ agents.py       VisitorAgent
│  └─ model.py        MarketDigitalTwin (Mesa Model) · 화재 생애주기
└─ reporting/
   ├─ analytics.py    현행안 vs 대안 지표 비교
   ├─ charting.py     밀집도·위험도 차트
   ├─ evidence.py     OpenAI Embedding 기반 RAG 검색
   ├─ narrative.py    LLM 또는 템플릿 본문 생성
   ├─ docx_renderer.py  DOCX 렌더
   └─ service.py      전체 순서 조율
```

---

## 공간 데이터 전제

`TDTC-AI-BE`의 `seed-market-data.sql`로 아래가 적재되어 있어야 합니다.

| 테이블 | 내용 |
|---|---|
| `mrkaddr01m` | 시장 (중심 위경도) |
| `mrkaddr01d` | 구역 (GeoJSON `Polygon`, 좌표 순서 `[경도, 위도]`) |
| `mrkadjc01m` | 구역 인접 관계 (통로 폭·거리·경로 좌표) |
| `mrkfcts01m` | 시설. `facility_type='GATE'`가 출입구 |
| `mrkbldg01m` | 건물 폴리곤 — 에이전트가 건물 안으로 들어가지 않도록 이동 영역에서 제외 |

---

## RAG 근거 문서 준비

공공문서 PDF 원문과 벡터 인덱스는 **Git에 포함하지 않습니다.**

1. PDF를 `knowledge/source_docs/`에 배치
2. `OPENAI_API_KEY` 설정 후 인덱스 생성

```bash
python scripts/build_rag_index.py     # → knowledge/vector_index.json
```

필요 문서:

1. 지속가능한 관광지 혼잡도 운영 관리 매뉴얼
2. 2025 행정업무운영 편람
3. 쉬운 공문서 쓰기 길잡이
4. 2025 전통시장 안전관리 매뉴얼
5. 전통시장 화재안전점검 운영지침 (중소벤처기업부고시 제2024-62호)
6. 정부 전통시장 화재 예방 및 안전관리 대책 (2023.07.03)

> 팀에서 `knowledge/vector_index.json`을 공유받았다면 PDF 배치와 인덱스 생성을 건너뛸 수 있습니다.

---

## 테스트

```bash
python -m pytest tests/reporting -m "not integration" -q   # OpenAI 호출 없음
python -m pytest tests/reporting -m integration -q         # API 키 + 벡터 인덱스 필요
```

### SIM 단독으로 보고서 확인 (Mock)

BE 없이 SIM만으로 보고서 파이프라인을 돌려볼 수 있습니다.

```bash
uvicorn app.main:app --reload --port 8000        # 1번 터미널

curl -X POST http://127.0.0.1:8000/simulation/reports/mock/night_market   # 2번 터미널
```

결과는 아래에 생성됩니다.

```text
outputs/{report_id}/
├─ {report_id}.docx
├─ {report_id}_analysis.json
└─ assets/
```

> Mock 3종은 모두 **이벤트가 없는** 정책 시나리오라, "위험 이벤트 대응 방안" 절과 대피 인원·최대 밀집 구역 열은 나오지 않습니다.

---

## 알려진 한계

- **개별 보행자 좌표는 근사값입니다.** CCTV/LiDAR는 구역 단위 집계(`visitor_count`)만 제공하므로 실제 개인 위치는 복원할 수 없고, 폴리곤 내부에 통계적 분포로 배치합니다.
- **캘리브레이션이 필요합니다.** 현재 임계값은 일반 인파 기준이며, 시장 특성(점포 배치·상시 체류 인원)에 맞춘 보정이 필요합니다.
- **`outputs/` 정리 정책이 없습니다.** BE가 S3에 올린 뒤에도 로컬 산출물이 `{report_id}/` 단위로 계속 쌓여 수동으로 지워야 합니다.
- **레이더/음향 센서는 완전히 제거되었습니다** (2026-07-23). 관련 테이블·리포지토리 함수·위험도 가중치 항목까지 코드에서 전부 삭제했습니다. 비명 감지 같은 사건은 밀집 위험 점수에 섞기보다 독립 알림 체계로 분리하는 것이 적절하다는 판단이었습니다.

---

## 관련 저장소

| 저장소 | 역할 |
|---|---|
| [TDTC-AI-FE](https://github.com/StrongestTeam10) | React 관제 화면 |
| [TDTC-AI-BE](https://github.com/StrongestTeam10) | Spring Boot API · 이 서비스의 유일한 호출자 |
| [TDTC-AI-CCTV](https://github.com/StrongestTeam10) | CCTV 영상 AI 파이프라인 |
| [TDTC-AI-INFRA](https://github.com/StrongestTeam10) | Terraform 인프라 |
