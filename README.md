# TDTC-AI-SIM
전통시장 AI 안전탐지 관제 솔루션 — 디지털 트윈 시뮬레이션 엔진 (FastAPI + Mesa)


> 변경 이력은 [CHANGELOG.md](./CHANGELOG.md) 참고

## 아키텍처 상 위치

```text
[TDTC-AI-FE]  →  [TDTC-AI-BE]  →  [TDTC-AI-SIM (이 저장소)]
 React/S3        Spring Boot        FastAPI + Mesa
      │                │                   │
      └────────────────┴──► [Supabase PostgreSQL] ◄──┘
```

- Spring Boot(`TDTC-AI-BE`)의 `SimulationEngineClient`가 이 서비스를 REST로 호출한다.
- 외부에 직접 노출하지 않고 내부 네트워크(VPC)에서만 접근하는 것을 전제로 한다.
- DB는 `TDTC-AI-BE`와 동일한 Supabase 인스턴스를 공유한다.

## 두 개의 파이프라인

| 구분 | 엔드포인트 | 모드 | 설명 |
|---|---|---|---|
| A. 관제/분석 | `POST /simulate/snapshot` | MIRROR | 센서 실측값을 로드해 오브젝트 배치 + 위험도 산출 |
| B. 시나리오 | `POST /simulate/scenario` | SCENARIO | 사용자 지정 What-if 실험. 응답 계약(frames/evacuationTimeSeconds/finalRiskScore) 구현 완료, 화재/음향전파 등 이벤트 모델 자체는 미구현 |

### 정책 시뮬레이션 결과 보고서 생성

```text
[FE]  정책 시나리오 실행 → 시뮬레이션 결과 저장
        ↓
      사용자가 [보고서 생성] 선택 (scenarioId 하나만 지목)
        ↓
      POST /api/simulation/reports        ← BE의 API
        ↓
[BE]  그 시나리오의 market_id로 같은 시장의 현행안을 찾아
      시장·구역·시나리오·결과를 JSON 한 덩어리로 조립
        ↓
      POST /simulation/reports/file       ← SIM의 API (이 저장소)
        ↓
[SIM] 지표 비교 + 공공문서 RAG 검색 + LLM 본문 생성 + 차트 렌더
        ↓
      DOCX 바이트를 응답 본문으로, 문서 제목을 X-Report-Title 헤더로 반환
        ↓
[BE]  S3에 업로드하고 경로·제목을 DB에 기록 → presigned URL 반환
        ↓
[FE]  받은 URL로 DOCX 다운로드
```

만들어 둔 보고서를 다시 받을 때는 `GET /api/simulation/reports/{scenarioId}/download`(BE)로
presigned URL만 재발급한다. S3에 파일이 그대로 있으므로 SIM을 다시 부르지 않는다.

#### 보고서 관련 API

| 엔드포인트 | 용도 |
|---|---|
| `POST /simulation/reports/file` | 보고서를 만들어 **DOCX 파일 자체**를 응답 본문으로 돌려준다. 운영에서 BE가 호출하는 경로 |
| `POST /simulation/reports` | 생성 로직은 같고 **파일이 저장된 경로를 JSON**으로 돌려준다. 로컬 디버깅용 |
| `GET /simulation/reports/{report_id}/docx` | 위에서 만들어진 DOCX를 내려받는다. 로컬 디버깅용 |
| `GET /simulation/reports/{report_id}/analysis` | 지표 비교와 RAG 근거 JSON 조회 |
| `GET /simulation/reports/status` | 보고서 검색기와 본문 생성기 상태 확인 |
| `POST /simulation/reports/mock/{mock_name}` | `data/db/*.json` Mock 기반 개발용 API |

BE가 `POST /simulation/reports` 대신 `/file`을 쓰는 이유: 전자가 돌려주는 경로는 SIM
컨테이너의 로컬 디스크(`outputs/`)라 재시작하면 사라지고, 인스턴스가 2대 이상이면 다른
인스턴스가 그 파일을 찾지 못한다. 파일 자체를 받아 S3에 올려야 보관이 보장된다.

#### 입력 계약 (`DbReportBundle`)

**SIM은 보고서 생성에 DB를 쓰지 않는다.** 필요한 행을 BE가 모두 읽어 JSON으로 실어
보내고, SIM은 받은 것만으로 문서를 만든다. 스키마는 `app/schemas/report_db_models.py`.

| 필드 | 내용 |
|---|---|
| `report_meta` | `report_id`(경로에 쓰이므로 `[A-Za-z0-9_-]{1,100}`), 제목, 의사결정 질문 |
| `market`, `zones` | 시장 기본정보와 구역 목록 |
| `baseline_scenario`, `baseline_result` | **현행안**(`simbsln01m` / `simbsln01d`) |
| `scenario_rows`, `result_rows` | **대안 시나리오**(`simscnr01m` / `simrslt01d`), 최소 1건 |
| `density_timeseries_rows` | 시간대별 밀집도. BE에 대응 테이블이 없어 현재는 늘 비어 있다 |

현행안을 별도 필드로 둔 이유: 현행안과 시나리오는 서로 다른테이블이라 **ID가 각각 1부터 시작해 겹칠 수 있다.** 
한 배열에 담으면 `scenario_id`로 둘을 구분할 수 없다.

#### 위험 이벤트 대응 방안 절

화재 이벤트가 상정된 시나리오에 한해 보고서 "7. 종합 검토 의견" 아래
"위험 이벤트 대응 방안" 절이 생성된다. 이벤트가 없으면 절 자체가 생성되지 않는다.

내용은 전달받은 값으로만 조립한다 — 상정된 이벤트(구역·강도·건수·발동 스텝), 대피 인원,
최대 밀집 구역, 위험점수 변화. LLM 모드에서도 이 템플릿 결과를 바닥에 깔아, LLM이 키를
누락하거나 형식을 어겨도 절이 사라지지 않도록 한다.

시나리오별 예측 결과표(4절)의 "대피 인원"·"최대 밀집 구역" 열 또한 해당 데이터가
있을 때만 생긴다. 대피는 구역 위험도가 임계를 넘으면 발생하므로 이벤트가 없어도 혼잡만으로
생길 수 있어, 값이 하나라도 양수이거나 이벤트가 있으면 열을 만든다.

## 폴더 구조

```text
app/
├── main.py                 FastAPI 진입점
├── config.py               환경설정
├── api/
│   ├── health.py           헬스체크 (/health, /health/db)
│   ├── simulate.py         시뮬레이션 엔드포인트
│   └── reports.py          보고서 생성·DOCX 다운로드·분석 JSON 조회 엔드포인트
├── schemas/
│   ├── models.py           요청/응답 스키마 (Spring Boot DTO와 camelCase 일치)
│   ├── report_db_models.py Spring Boot가 전달하는 ERD 조회 DTO
│   └── report_models.py    보고서 파이프라인 내부 모델
├── db/
│   ├── connection.py       커넥션 풀
│   ├── repository.py       DB 조회 계층 (Mesa 모델은 SQL을 직접 쓰지 않음)
│   └── report_adapter.py   ERD 조회 DTO를 보고서 내부 모델로 변환
├── simulation/
│   ├── space.py            GeoJSON 파싱, 위경도 ↔ 로컬 미터 좌표 변환
│   ├── placement.py        구역 폴리곤 내 오브젝트(유동인구) 배치
│   ├── risk.py             위험도 스코어링 (공인 기준 근거)
│   ├── agents.py           VisitorAgent
│   └── model.py            MarketDigitalTwin (Mesa Model)
└── reporting/
    ├── analytics.py        기준안과 복수 대안의 지표 변화량 비교
    ├── charting.py         밀집도·위험도·시간대별 차트 생성
    ├── evidence.py         OpenAI Embedding 기반 공공문서 RAG 검색
    ├── narrative.py        LLM 또는 템플릿 기반 보고서 본문 생성
    ├── docx_renderer.py    수정 가능한 DOCX 정책 보고서 생성
    └── service.py          검색·분석·서술·차트·문서 생성 순서 조율
```

## 보고서 및 RAG 환경설정

보고서 생성에는 다음 환경변수를 사용한다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | 없음 | Embedding 검색 및 LLM 본문 생성에 필요한 API 키 |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | PDF 및 검색 질의 Embedding 모델 |
| `OPENAI_MODEL` | `gpt-4.1-mini` | 보고서 본문 생성 모델 |
| `NARRATIVE_MODE` | `template` | `template` 또는 `openai` |
| `NARRATIVE_STRICT` | `false` | LLM 오류 발생 시 보고서 생성을 중단할지 여부 |
| `RAG_MIN_VECTOR_SCORE` | `0.35` | 벡터 검색 결과의 최소 유사도 |
| `REPORT_OUTPUT_DIR` | `outputs` | DOCX와 분석 JSON 저장 위치 |
| `REPORT_VECTOR_INDEX_PATH` | `knowledge/vector_index.json` | 벡터 인덱스 경로 |
| `DOCX_FONT_NAME` | `맑은 고딕` | DOCX 본문 한글 글꼴 |
| `KOREAN_FONT_PATH` | 자동 탐색 | Matplotlib 차트용 한글 글꼴 경로 |

## 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env      # 값 채우기
uvicorn app.main:app --reload --port 8000
```

API 문서: http://localhost:8000/docs

## 위험도 산출 근거

임의 가중치가 아니라 공인 기준에 근거한다.

| 기준 | 값 | 출처 |
|---|---|---|
| 수용 한계 | 1인당 0.46 m² (≈ 2.17명/m²) | 행정안전부「다중운집인파사고 안전관리 가이드라인」(2024.9), 미국 NFPA 101 준용 |
| 위험 임계 | 5명/m² | G. Keith Still 군중안전 기준 |
| 참사 사례 | 5.6~6.6명/m² | 10.29 이태원 참사 당시 추정 밀집도 |
| 보행 유동 용량 | 통로 폭 1m 당 1.3명/초 | Fruin/SFPE 보행자 유동 이론 통상 설계값 |

### 종합 점수 구성

| 지표 | 가중치 | 산출 근거 |
|---|---|---|
| 밀집도 | 0.55 | 압사의 직접 원인 |
| 통로 병목 | 0.10 | 구역 인원의 대피 소요 시간 (5분 초과 시 최고점) |

(2026-07-23: 레이더 기반 "이동 흐름"(0.20), 음향 기반 "이상 음향"(0.15) 지표는 센서 완전
제거로 삭제됨. 남은 두 지표에 재정규화 로직이 그대로 적용되어 실질 반영 비율은
84.6% : 15.4%)

**가중치 재정규화**: 센서 미설치 등으로 데이터가 결측이면 해당 가중치를 제외하고 나머지로 100%를 재배분한다. 이 처리가 없으면 결측 지표가 0점으로 반영되어 밀집도가 아무리 높아도 상위 등급에 도달할 수 없다.

**안전 오버라이드**: 밀집도 단독으로 임계를 넘으면 다른 지표와 무관하게 등급을 강제 상향한다. 압사는 밀집도만으로도 발생하므로 종합 평균에 희석되어선 안 된다.

### 검증 결과 (망원시장 3구역 기준)

| 시나리오 | 인원 | 밀집도 | 1인당 면적 | 점수 | 등급 |
|---|---|---|---|---|---|
| 평시 | 110명 | 0.06명/m² | 15.7m² | 1.6 | low |
| 주말 오후 | 1,100명 | 0.68명/m² | 1.47m² | 15.6 | low |
| 축제 | 3,300명 | 2.04명/m² | 0.49m² | 46.7 | medium |
| 특정구역 병목 | 2,750명 | 4.25명/m² | 0.24m² | 81.0 | critical |

## 알려진 한계 / 후속 작업

- **개별 보행자 좌표는 근사값**이다. CCTV/LiDAR는 구역 단위 집계(`visitor_count`)만 제공하므로 실제 개인 위치는 복원 불가하며, 폴리곤 내부에 통계적 분포로 배치한다.
- **레이더/음향 센서는 2026-07-23부로 완전히 제거**되었다. 관련 DB 테이블(`senradr01m/h`, `audevnt01m/h`), 리포지토리 함수, 위험도 가중치 항목까지 코드에서 전부 삭제되었다. (참고로 제거 전에는 비명 감지 등 밀집도와 무관한 사건을 밀집 위험 점수에 섞기보다 독립 알림 체계로 분리하는 것이 적절하다는 논의가 있었다.)
- **파이프라인 B의 이벤트 모델 미구현**: 화재 확산, 음향 전파, 통로 폐쇄 영향 시뮬레이션.
- **캘리브레이션 필요**: 현재 임계값은 일반 인파 기준이며, 실제 시장 특성(점포 배치, 상시 체류 인원 등)에 맞춘 보정이 필요하다.
- **`outputs/` 정리 정책이 없다.** BE가 S3에 올린 뒤에도 로컬 산출물이 `{report_id}/` 단위로 계속 쌓여 수동으로 지워야 한다.

## 공간 데이터 전제

`TDTC-AI-BE`의 `seed-market-data.sql`로 아래가 적재되어 있어야 한다.

- `mrkaddr01m` — 시장 (중심 위경도)
- `mrkaddr01d` — 구역 (GeoJSON `Polygon`, 좌표 순서는 `[경도, 위도]`)
- `mrkadjc01m` — 구역 인접 관계 (통로 폭, 거리)
- `mrkfcts01m` — 출입구 (`facility_type='GATE'`, 위경도)

## RAG 근거 문서 준비

공공문서 PDF 원문과 생성된 벡터 인덱스는 Git에 포함하지 않는다.

필요 문서:

1. 지속가능한 관광지 혼잡도 운영 관리 매뉴얼
2. 2025 행정업무운영 편람
3. 쉬운 공문서 쓰기 길잡이

PDF를 다음 경로에 배치한다.

```text
knowledge/source_docs/
```

API 키를 설정한 뒤 인덱스를 생성한다.
  ```powershell
  python scripts/build_rag_index.py
  ```
생성 결과:
```text
knowledge/vector_index.json
```
신규 개발 환경과 배포 환경에서는 팀 공유 저장소 또는
Object Storage를 통해 PDF 또는 벡터 인덱스를 별도로 준비해야 한다.

## 보고서 테스트 방법

### 1. 필요 패키지 설치

```powershell
python -m pip install -r requirements.txt
```

### 2. `.env` 파일 설정

앞의 `보고서 및 RAG 환경설정` 항목을 참고하여 프로젝트 루트에
`.env` 파일을 생성한다.

최소한 다음 항목이 필요하다.

```env
OPENAI_API_KEY=발급받은_API_KEY
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
OPENAI_MODEL=gpt-4.1-mini
NARRATIVE_MODE=openai
REPORT_OUTPUT_DIR=outputs
REPORT_VECTOR_INDEX_PATH=knowledge/vector_index.json
```

`.env`는 API 키와 DB 접속정보를 포함할 수 있으므로 Git에 커밋하지 않는다.

### 3. 공공문서 PDF 준비

다음 공공문서 PDF를 `knowledge/source_docs/`에 배치한다.

1. 지속가능한 관광지 혼잡도 운영 관리 매뉴얼
2. 2025 행정업무운영 편람
3. 쉬운 공문서 쓰기 길잡이
4. 2025 전통시장 안전관리 매뉴얼
5. 전통시장 화재안전점검 운영지침(중소벤처기업부고시)(제2024-62호)(20240913)
6. 230703(석간) 정부 전통시장 화재 예방 및 안전관리 대책 발표(재난안전조사과)

PDF 원문은 Git에 포함하지 않는다.

### 4. 벡터 인덱스 생성

다음 스크립트를 실행하여 RAG 검색에 사용할 벡터 인덱스를 생성한다.

```powershell
python scripts/build_rag_index.py
```

생성 여부 확인:

```powershell
Test-Path .\knowledge\vector_index.json
```

결과가 `True`이면 정상이다.

팀에서 생성된 `knowledge/vector_index.json`을 별도로 공유받았다면
PDF 배치와 인덱스 생성 단계는 생략할 수 있다.

### 5. OpenAI 호출 없는 단위·파이프라인 테스트

```powershell
python -m pytest tests/reporting -m "not integration" -q
```

현재 검증 결과:

```text
7 passed, 1 deselected
```

### 6. OpenAI Vector RAG 통합 테스트

통합 테스트에는 `OPENAI_API_KEY`와
`knowledge/vector_index.json`이 필요하다.

```powershell
python -m pytest tests/reporting -m integration -q
```

현재 검증 결과:

```text
1 passed, 3 deselected
```

### 7. 서버 실행

프로젝트 루트에서 FastAPI 서버를 실행한다.

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

Swagger API 문서:

```text
http://127.0.0.1:8000/docs
```

서버 실행 명령이 현재 PowerShell을 점유하므로,
다음 단계는 새로운 PowerShell 창에서 실행한다.

### 8. Mock으로 SIM 단독 확인
새 PowerShell에서 프로젝트 루트로 이동한다.

```powershell
cd "C:\path\to\TDTC-AI-SIM"
```

가상환경을 사용한다면 다시 활성화한다.

```powershell
.\.venv\Scripts\Activate.ps1
```

보고서 엔진 상태 확인:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/simulation/reports/status" |
ConvertTo-Json -Depth 5
```

Mock 보고서 생성:

```powershell
$response = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/simulation/reports/mock/night_market"

$response |
ConvertTo-Json -Depth 5
```

생성된 파일 확인:

```powershell
$reportDir = ".\outputs\$($response.report_id)"

Get-ChildItem `
  -LiteralPath $reportDir `
  -Recurse |
Select-Object FullName, Length
```

결과 파일은 다음 경로에 생성된다.

```text
outputs/{report_id}/
├─ {report_id}.docx
├─ {report_id}_analysis.json
└─ assets/
```

DOCX 열기:

```powershell
$docxPath = ".\outputs\$($response.report_id)\$($response.report_id).docx"

Start-Process $docxPath
```

분석 JSON API 확인:

```powershell
$analysisUrl = "http://127.0.0.1:8000$($response.analysis_url)"

Invoke-RestMethod -Uri $analysisUrl |
ConvertTo-Json -Depth 10
```

> `night_market`을 비롯한 기존 Mock 3개는 이벤트가 없는 정책 시나리오다.
> 위험 이벤트 대응 방안 절과 대피 인원·최대 밀집 구역 열은 나오지 않는다.
