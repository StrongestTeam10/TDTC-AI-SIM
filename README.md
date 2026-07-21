# TDTC-AI-SIM

전통시장 AI 안전탐지 관제 솔루션 — 디지털 트윈 시뮬레이션 엔진 (FastAPI + Mesa)

## 아키텍처 상 위치

```
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
| B. 시나리오 | `POST /simulate/scenario` | SCENARIO | 사용자 지정 What-if 실험 (이벤트 모델 미구현) |

## 폴더 구조

```
app/
├── main.py              FastAPI 진입점
├── config.py            환경설정
├── api/
│   ├── health.py        헬스체크 (/health, /health/db)
│   └── simulate.py      시뮬레이션 엔드포인트
├── schemas/
│   └── models.py        요청/응답 스키마 (Spring Boot DTO와 camelCase 일치)
├── db/
│   ├── connection.py    커넥션 풀
│   └── repository.py    DB 조회 계층 (Mesa 모델은 SQL을 직접 쓰지 않음)
└── simulation/
    ├── space.py         GeoJSON 파싱, 위경도 ↔ 로컬 미터 좌표 변환
    ├── placement.py     구역 폴리곤 내 오브젝트(유동인구) 배치
    ├── risk.py          위험도 스코어링 (공인 기준 근거)
    ├── agents.py        VisitorAgent
    └── model.py         MarketDigitalTwin (Mesa Model)
```

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
| 이동 흐름 | 0.20 | 레이더 `avg_speed`, 자유보행 130cm/s 대비 저하율 |
| 이상 음향 | 0.15 | 음향 이벤트 건수 × 신뢰도 |
| 통로 병목 | 0.10 | 구역 인원의 대피 소요 시간 (5분 초과 시 최고점) |

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
- **음향 이벤트는 위험 점수에만 반영**된다. 비명 감지는 밀집도와 무관한 별개의 사건이므로, 밀집 위험 점수에 섞기보다 독립적인 알림 체계로 분리하는 것이 적절하다. (후속 과제)
- **파이프라인 B의 이벤트 모델 미구현**: 화재 확산, 음향 전파, 통로 폐쇄 영향 시뮬레이션.
- **캘리브레이션 필요**: 현재 임계값은 일반 인파 기준이며, 실제 시장 특성(점포 배치, 상시 체류 인원 등)에 맞춘 보정이 필요하다.

## 공간 데이터 전제

`TDTC-AI-BE`의 `seed-market-data.sql`로 아래가 적재되어 있어야 한다.

- `mrkaddr01m` — 시장 (중심 위경도)
- `mrkaddr01d` — 구역 (GeoJSON `Polygon`, 좌표 순서는 `[경도, 위도]`)
- `mrkadjc01m` — 구역 인접 관계 (통로 폭, 거리)
- `mrkfcts01m` — 출입구 (`facility_type='GATE'`, 위경도)
