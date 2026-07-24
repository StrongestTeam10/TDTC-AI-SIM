# TDTC-AI-SIM
전통시장 AI 안전탐지 관제 솔루션 — 디지털 트윈 시뮬레이션 엔진 (FastAPI + Mesa)

## 변경 이력

### 2026-07-24 (격자 기반 이동으로 전면 교체 — 폴리곤 이탈 근본 해결 + 오브젝트 회피)
- **근본 원인 재확인**: 앞선 수정(경계 웨이포인트, 회전 사각형 샘플링)에도 여전히
  폴리곤 밖으로 나가는 모습이 보고됨. 진짜 원인은 **실제 구역 폴리곤이 오목한
  (concave) 모양**이라는 것 — 두 점이 각각 폴리곤 내부에 있어도 그 사이 직선이
  오목한 부분을 가로질러 밖으로 나갈 수 있음. 직선 보간 방식 자체의 근본적 한계
- **해결**: 연속 좌표계 직선 이동을 폐기하고 **격자(그리드) 기반 이동**으로 전면 교체
- 🆕 `simulation/gridspace.py` 신규: `WalkableGrid` 클래스
  - 모든 구역 폴리곤의 union을 1m×1m 격자로 래스터화 (셀 중심점이 폴리곤 내부인지로 판정)
  - 매대/푸드트럭 등 오브젝트는 반경만큼 "막힌 셀"로 표시 → 자동 회피
  - `mrkadjc01m.path_coordinates`(통로 중심선) 근처 셀은 이동 비용을 낮춰서
    "선호 경로"로 반영 (다익스트라 기반 최단 경로, 대각선 이동은 sqrt(2)배 거리 반영)
  - 대각선 이동 시 양옆 두 칸이 모두 막혀 있으면 모서리를 스쳐 지나가는 걸 금지
- ✏️ `MarketLayout`: `boundary_waypoints` 필드를 `walkable_grid: WalkableGrid`로 교체
- ✏️ `MarketDigitalTwin.build_path()`: 시그니처를 `(from_zone, to_zone)`에서
  `(from_x, from_y, to_x, to_y, arrive_zone)`로 변경 — 격자 최단 경로를 그대로
  웨이포인트로 반환. 구역 간 이동뿐 아니라 같은 구역 내 배회(wander)에도 동일하게
  적용해서 오목한 구역 안에서도 안전하게 이동함
- ✏️ `agents.py`: 새 `build_path` 시그니처에 맞춰 호출부 수정
- ✏️ `random_point_in_zone()`/`_spawn_agents()`: 목적지/초기 배치가 오브젝트 위에
  떨어지면 재시도하도록 보강 (매대 위에 사람이 서 있는 것처럼 보이는 것 방지)
- 🆕 `db/repository.py`: `fetch_stalls()`가 `footprint_radius_m`도 함께 조회
- 실제(오목한) 시드 폴리곤 + 가상 오브젝트로 통합 스모크 테스트: 20스텝 동안 820회
  검사에서 폴리곤 이탈 0건, 오브젝트 반경 침범 0건 확인. 별도로 오브젝트를 사이에
  둔 두 지점 이동 테스트에서 실제로 오브젝트 경계를 스치며 돌아가는 것도 확인함
- ⚠️ 성능 참고: 격자 크기는 시장 전체 넓이에 비례(현재 시드 기준 245×73칸,
  1668개 보행 가능 셀). 에이전트가 많고 스텝 수가 매우 크면 다익스트라 호출
  빈도가 늘어날 수 있음 — 지금까지 테스트한 규모(수십~수백 명, 수십 스텝)에서는
  체감 지연 없음

### 2026-07-24 (유입 방식을 "스텝당 고정 인원"에서 "총 인원 랜덤 분산"으로 변경)
- **요청**: 스텝마다 고정된 인원(`inflowPerStep`)이 아니라, 총 유입 인원수를 지정하면
  그 인원이 스텝 전체에 걸쳐 무작위로 흩어져서 유입되도록
- ✏️ `schemas/models.py`: `PredictRequest.inflowPerStep` → `totalInflow`로 변경
  (전체 시뮬레이션 동안 유입될 총 인원)
- 🆕 `api/simulate.py`: `_build_inflow_schedule(total, steps, seed)` 추가 — 스텝별
  무작위 가중치를 뽑아 정규화한 뒤 total을 곱해 배분(스텝마다 들쭉날쭉, 합계는
  total에 근접). `simulate_predict`가 이 스케줄을 매 스텝 `inject_inflow()`에 적용
- 스모크 테스트로 total=100, steps=30 기준 합계가 101(반올림 오차 범위 내)로
  나오고 스텝별 인원수가 실제로 들쭉날쭉한 것 확인함

### 2026-07-24 (실제 구역 모양에서 에이전트가 폴리곤 밖에 뭉쳐 보이는 버그 수정)
- **증상**: 실제 시장 데이터(대각선으로 길게 뻗은 좁은 구역)에서, 구역 경계 근처에
  에이전트들이 폴리곤 바깥에 뭉쳐서(blob) 나타나는 현상 (스크린샷으로 재현 확인)
- **원인 1 (핵심)**: `boundary_waypoints` fallback이 `exterior.intersection()`으로
  "두 폴리곤이 정확히 맞닿아 있다"고 가정했는데, 실제 지도에서 그려진 폴리곤은
  완벽하게 안 맞닿는 경우가 흔함. 그러면 `intersection()`이 떨어진 점들의 집합 같은
  이상한 도형을 반환하고, 그 `.centroid`가 구역 사이 엉뚱한 위치로 튐 → 그 지점을
  지나가는 모든 에이전트가 거기서 뭉쳐 보임
  - ✏️ `model.py`: "각 폴리곤에서 상대 구역 중심에 가장 가까운 경계점"(`nearest_points`)
    의 중점을 쓰는 방식으로 교체. 두 폴리곤이 정확히 안 맞닿아 있어도(약간의 틈이
    있어도, 심지어 꼭짓점 하나만 스치듯 닿아도) 항상 안정적으로 두 구역 사이
    경계 근처에 웨이포인트가 잡힘
- **원인 2**: `_random_point_in_polygon`(에이전트 배치/목적지 샘플링)이 축 정렬
  경계 상자(bounding box) 안에서 거부 샘플링했는데, 대각선으로 긴 좁은 폴리곤은
  경계 상자가 실제 면적보다 훨씬 커서 실패율이 높았고, 실패 시 같은 대표점에
  계속 쌓여서 뭉쳐 보이는 문제도 겹쳐 있었음
  - ✏️ `placement.py`: 축 정렬 경계 상자 대신 **최소 회전 사각형**
    (`minimum_rotated_rectangle`) 안에서 후보를 뽑도록 변경. 폴리곤 방향에 상관없이
    거부율이 크게 낮아짐
- 합성 대각선 폴리곤(60도 회전, 긴 좁은 직사각형)으로 200회 샘플링 테스트 —
  실패 0회, 전부 폴리곤 내부 확인. 꼭짓점 하나만 맞닿는 극단 케이스로도 웨이포인트가
  정확한 위치에 잡히는 것 확인함

### 2026-07-24 (통로 중심선 데이터 지원 — 정확한 동선 구현)
- **배경**: 직전 수정(경로 기반 이동)은 구역 경계 중점 1개만 거치는 근사라서, 구역
  모양이 복잡하면 여전히 폴리곤을 살짝 벗어날 수 있었음. 실제 통로 모양(꺾인 골목
  등)을 반영하려면 경계 중점 근사로는 한계가 있음
- 🆕 `mrkadjc01m.path_coordinates`(GeoJSON LineString, WGS84) 신규 컬럼: 레이아웃
  에디터로 실제 통로를 따라 그린 선. NULL이면 기존 경계 중점 근사로 자동 fallback
  (하위 호환, 기존 시장 데이터는 그대로 동작함)
- 🆕 `space.py`: `parse_linestring()` 추가 (`parse_polygon()`과 동일 패턴)
- ✏️ `db/repository.py`: `fetch_adjacency()`가 `path_coordinates`도 함께 조회
- ✏️ `model.py`: `MarketLayout.boundary_waypoints`를 "점 1개"에서 "점 목록(중심선
  전체)"으로 확장. `path_coordinates`가 있으면 그 점들을 순서대로, 없으면 기존
  경계 중점 근사를 1개짜리 리스트로 사용. 역방향(to→from)은 자동으로 순서를 뒤집어
  재사용(에디터에서 양방향 각각 그릴 필요 없음)
- ✏️ `MarketDigitalTwin.build_path()`가 이제 여러 웨이포인트를 순서대로 지나가는
  전체 경로를 반환 (기존엔 최대 2점, 이제는 통로 중심선의 꺾임 수만큼)
- 일부러 꺾인(L자형) 통로 중심선으로 스모크 테스트: 웨이포인트 4개를 순서대로
  거치며 최대 스텝 이동거리(6.0) 제한이 그대로 지켜지는 것, 역방향 순서가 올바르게
  뒤집히는 것 확인함
- ⚠️ 아직 미완료: 실제 통로 중심선 데이터 입력 — 재재님이 레이아웃 에디터로 직접
  그려서 입력 예정. 그 전까지는 모든 구역 쌍이 기존 경계 중점 근사로 동작함 (에러
  없이 정상 동작하지만 완벽한 동선은 아님)

### 2026-07-24 (경로 기반 이동으로 전면 교체 — 순간이동/폴리곤 이탈 문제 해결)
- **증상**: 프레임 재생에서 여전히 순간이동하는 모습이 많이 보이고, 구역 A에서 B로
  옮겨갈 때 직선 보간 경로가 두 구역 폴리곤 바깥 여백을 가로지르는 경우가 많았음
- **원인**: 목적지를 정하면 한 스텝 만에 그 자리로 이동하는 구조였음(거리 제한 없음).
  구역을 넘어갈 때도 목적지 구역의 완전히 무작위 지점으로 바로 이동해서, 실제로
  맞닿아 있지 않은 두 지점 사이를 직선으로 가로지르는 경우가 흔했음
- ✏️ `agents.py` 전면 재작성: `_path`(웨이포인트 목록)를 도입해 매 스텝 최대
  `MOVE_SPEED_M`(6.0, 임시 캘리브레이션 값)만큼만 목적지 쪽으로 이동. 목적지에
  도착하기까지 여러 스텝이 걸림 (도착 시에만 `zone_id` 갱신 — 그 전까지는 이전
  구역 소속으로 집계됨, 실제로 이동 중인 사람을 자연스럽게 표현)
- 🆕 `MarketLayout.boundary_waypoints`: 인접한 두 구역이 실제로 맞닿은 경계선의
  중점을 미리 계산해둠 (`shapely` `exterior.intersection()` 사용, 안 맞닿아 있으면
  두 중심점의 중점으로 대체)
- 🆕 `MarketDigitalTwin.build_path(from_zone, to_zone)`: 구역을 넘어갈 때 경계
  웨이포인트를 먼저 지나가는 경로를 만들어줌 → 폴리곤 바깥을 가로지르지 않고 실제
  통로를 지나가는 것처럼 보이게 됨 (완벽한 경로망은 아니고 직선 2구간 근사)
- ✏️ CONGESTED 상태에서는 새 경로를 잡지 않고 기존 경로를 0.4배 속도로 느리게 진행
  (급하지 않게, 혼잡하니 천천히 걷는 느낌)
- ✏️ `inject_inflow()`: 게이트 근처 임의 지점이 아니라 게이트의 실제 물리적 좌표에서
  바로 스폰하도록 변경 (문으로 들어오는 것처럼 보이게)
- 스모크 테스트로 1스텝 최대 이동 거리가 정확히 `MOVE_SPEED_M` 이하로 제한되는 것,
  경계 웨이포인트가 정상 계산되는 것, 15스텝 동안 구역 이동이 점진적으로(한 번에
  안 몰리고) 일어나는 것 확인함
- ⚠️ 한계: 완전한 경로망(코너를 도는 실제 통로 폴리라인)은 아니고 "경계 중점 1개"만
  거치는 근사이므로, 구역 모양이 복잡(오목한 형태 등)하면 여전히 아주 짧은 구간
  폴리곤을 살짝 벗어날 수 있음. 완벽하게 하려면 통로 중심선 데이터가 추가로 필요함

### 2026-07-24 (정상 보행 baseline wander 추가 — 매대 데이터 없어도 사람이 움직이게)
- **증상**: 예측 시뮬레이션을 재생해도 사람이 전혀 안 움직이는 것처럼 보임
- **원인**: `_maybe_move_toward_attraction()`이 "더 매력적인 인접 구역이 있을 때만" 이동하는
  구조였는데, 아직 매대(오브젝트) 데이터가 하나도 없어 모든 구역의 attraction이 0이라
  이동 조건 자체가 절대 성립하지 않았음. 여기에 실측 위험도가 낮아 대부분 NORMAL
  상태(=이동 로직이 도는 유일한 상태)에 머물러 있어서 사실상 전원이 정지 상태였음
- ✏️ `agents.py`: 인접 구역으로 옮기지 않기로 한 경우에도, 정상 보행 중이면
  `WANDER_PROBABILITY`(0.4) 확률로 같은 구역 안에서 위치를 다시 뽑아 걸어다니는
  것처럼 보이게 함. 매대 데이터가 들어와 attraction이 생기면 그쪽으로 쏠리는 이동이
  이 baseline wander 위에 우선 적용됨
- 스모크 테스트로 attraction=0인 구역에서도 개별 에이전트가 여러 스텝에 걸쳐 실제로
  위치가 바뀌는 것 확인함 (10스텝 중 5회 이동)

### 2026-07-24 (예측 시뮬레이션 `/simulate/predict` 신규 추가)
- **목적**: 실측 관제(MIRROR)는 "지금 이 순간"만 보여줄 뿐 시간이 흐르지 않고,
  시나리오(SCENARIO)는 이동은 하지만 가상 초기값(면적 비례 배분)에서 출발함.
  "실제 관측 상태에서 출발해 인구 유입이 몰렸을 때 어떻게 되는지"를 보여주는 기능이
  빠져 있었음 → 이 둘을 조합해 신규 엔드포인트로 추가
- 🐛 **버그 수정**: `VisitorAgent._maybe_move_toward_attraction()`이 `model.attraction_of()`,
  `model.movement_graph`를 호출하는데 정작 `MarketDigitalTwin`에 둘 다 정의돼 있지 않아서
  NORMAL 상태 에이전트가 한 스텝만 진행돼도 `AttributeError`로 죽는 버그가 있었음.
  실제 관측치처럼 위험도가 낮은 상황(=거의 항상 NORMAL)에서는 즉시 재현됨
  - `movement_graph` → 기존 `layout.graph`(구역 인접 그래프)를 그대로 노출하는 property로 구현
  - `attraction_of(zone_id)` → 신규 `ZoneSpec.attraction` 필드(해당 구역 매대 weight 합)를
    반환하도록 구현
  - ⚠️ **`agents.py`도 함께 수정함** (`_maybe_move_toward_attraction`의 확률 계산이
    attraction 값을 그대로 확률로 써서 weight 합이 1 넘으면 매 스텝 100% 이동하던 문제를
    `min(best_attraction * 0.1, 0.8)`로 완화). **이 파일은 파이프라인 B(`/simulate/scenario`,
    팀원 담당)와 공유하는 코드라 반드시 공유 필요**
- 🆕 매대(오브젝트) 데이터 반영: `mrkfcts01m`에 `weight` 컬럼 추가(GATE=유입 가중치,
  그 외=매력도 가중치). `db/repository.py`에 `fetch_stalls()` 추가, `fetch_gates()`도
  `weight` 컬럼을 함께 조회하도록 수정. `MarketLayout.from_db_rows()`가 매대 weight를
  가장 가까운 구역에 집계해 `ZoneSpec.attraction`으로 저장 (게이트를 가장 가까운
  구역에 귀속시키는 기존 로직과 동일 패턴)
- 🆕 `MarketDigitalTwin.inject_inflow(count)`: 게이트 weight 비례로 신규 방문객을
  유입시키는 메서드 추가 (스텝 사이에 호출)
- 🆕 `POST /simulate/predict`: 실측 관측값으로 초기 배치 + `mode=SCENARIO`로 이동
  로직만 켜서 `steps`만큼 진행. 매 스텝 `inflowPerStep`만큼 게이트로 신규 유입 주입.
  스텝별 `frames`(에이전트 상태)와 `riskTrend`(구역별/종합 위험도 추이) 반환.
  화재 등 외부 이벤트는 다루지 않음(그건 `/simulate/scenario` 영역)
- 🆕 `schemas/models.py`: `PredictRequest`/`ZoneRiskPoint`/`RiskTrendPoint`/`PredictResult` 추가
- 합성 데이터로 직접 스모크 테스트 실행 (`MarketLayout.from_db_rows` → `MarketDigitalTwin`
  → `inject_inflow`/`step` 5회 반복) — 버그 없이 정상 동작, 게이트 가중치(3:1) 비례로
  유입이 분배되는 것까지 확인함. 단, 실제 API 엔드투엔드 테스트(DB 연결)는 아직 안 함

### 2026-07-23 (레이더/음향 센서 완전 제거)
- **결정**: 레이더 제거 결정과 함께, 지난번 "호출만 비활성화, 코드는 유지"했던 음향 센서
  관련 코드도 이번에 전부 삭제
- `risk.py`: `WEIGHT_FLOW`/`WEIGHT_ACOUSTIC` 상수, `flow_to_score()`/`acoustic_to_score()`
  함수, `RiskAssessment.flow_score`/`acoustic_score` 필드 전부 삭제. 위험도 가중치는
  density(0.55)/bottleneck(0.10) 2개만 남았고, 기존 "결측 지표 재정규화" 로직을 그대로
  재사용해 두 지표만으로 100%를 채우도록 함 (실질 반영 비율 84.6% : 15.4%)
- `model.py`: `ZoneObservation`에서 `avg_speed_cm_s`/`acoustic_event_count`/
  `acoustic_max_confidence` 필드 제거, `snapshot()`의 `breakdown`도 density/bottleneck만 반환
- `db/repository.py`: `fetch_radar_speed()`, `fetch_acoustic_events()` 함수 완전 삭제
- `schemas/models.py`: `RiskBreakdown`, `ContributingFactors`에서 flow/acoustic 필드 제거
  (⚠️ `ContributingFactors`는 파이프라인 B `ScenarioResult.finalRiskScore` 응답 계약이라
  BE `RiskScoreDto.ContributingFactors`도 함께 수정함 — 담당 팀원 공유 필요)
- `sensor-seed.sql`: 레이더 데이터 섹션(1,728건) 삭제, 센서 등록을 라이다 1대/구역으로 축소.
  `scripts/generate_sensor_seed.py`로 재생성해서 검증함 (스크립트 자체도 레이더/음향 생성
  로직 완전 삭제)
- 모의 테스트로 `/simulate/snapshot`, `/simulate/scenario` 둘 다 정상 동작 확인 완료

## 변경 이력 (이전)

### 2026-07-23
- **파이프라인 B(`/simulate/scenario`) 응답 계약을 BE `ScenarioResultDto`에 맞춰 정식 구현**
  - 기존에는 `{scenarioType, steps, finalSnapshot, note}` 형태의 임시 dict를 반환했으나,
    `scenarioId`(UUID), `requestedAt`, 스텝별 전체 에이전트 상태 `frames`,
    `evacuationTimeSeconds`, `finalRiskScore`를 포함하는 `ScenarioResult` 스키마로 교체
  - `evacuationTimeSeconds`: `VisitorState.EVACUATING` 상태 에이전트 전원이 출구 구역(`is_exit_zone`)에
    도달한 시점을 기준으로 산출. `STEP_DURATION_SECONDS`(현재 10초로 가정)는 임시 캘리브레이션 값이며,
    구역 간 실제 거리(`mrkadjc01m.distance_m`)와 평균 보행속도 기반 재산정이 후속 과제로 남아있음
  - `finalRiskScore`: 마지막 스텝에서 위험도가 가장 높은 구역의 점수/등급/세부지표를 시장 전체 대표값으로 사용
  - BE와의 실제 통합 테스트 완료 (BE가 이 응답을 정상적으로 역직렬화하는 것까지 확인)
- **음향 센서 데이터 사용 중단**
  - `repo.fetch_acoustic_events()` 함수 자체는 남겨두되, `/simulate/snapshot`(`simulate.py`)에서의 실제 호출을 비활성화(주석 처리)하고 `acoustics = {}`로 대체
  - `risk.py`의 가중치 재정규화 로직에 의해 `acoustic_event_count=0`이면 위험도 종합 계산에서 음향 지표가 자동으로 제외됨 (별도 로직 변경 불필요)
  - `sensor-seed.sql`의 "5) 음향 이벤트" 섹션(18건) 삭제, `scripts/generate_sensor_seed.py`의 해당 INSERT 생성(`emit`) 호출 비활성화
  - ⚠️ `sensor-seed.sql`, `scripts/generate_sensor_seed.py`는 이 저장소에 커밋된 적이 없던 파일이라(로컬 전용), 이번 변경분도 별도로 전달받아 적용 필요

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
정책 시나리오 실행
    ↓
시뮬레이션 결과 저장
    ↓
사용자가 [보고서 생성] 버튼 선택
    ↓
Spring Boot가 change_id 기준으로 기준안·대안 결과 조회
    ↓
POST /simulation/reports
    ↓
지표 비교 + 공공문서 RAG 검색 + LLM 본문 생성
    ↓
DOCX 및 분석 JSON 생성
    ↓
GET /simulation/reports/{report_id}/docx
```

#### 보고서 관련 API
| 엔드포인트 |	용도 |
|---|---|
| `POST /simulation/reports` | 저장된 시뮬레이션 결과를 바탕으로 보고서 생성 |
| `GET /simulation/reports/{report_id}/docx` | 생성된 DOCX 다운로드 |
| `GET /simulation/reports/{report_id}/analysis` | 지표 비교와 RAG 근거 JSON 조회 |
| `GET /simulation/reports/status` | 보고서 검색기와 본문 생성기 상태 확인 |
| `POST /simulation/reports/file` | DOCX를 즉시 반환하는 테스트·시연용 API |
| `POST /simulation/reports/mock/{mock_name}` | ERD Mock 기반 개발용 API |

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
3 passed, 1 deselected
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

### 8. 보고서 생성 확인

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
