"""시뮬레이션 엔드포인트."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from app.db import repository as repo
from app.schemas.models import (
    ContributingFactors,
    PredictRequest,
    PredictResult,
    RiskScoreDto,
    RiskTrendPoint,
    ScenarioRequest,
    ScenarioResult,
    SnapshotRequest,
    SnapshotResponse,
    ZoneRiskPoint,
)
from app.simulation.agents import VisitorState
from app.simulation.model import (
    MarketDigitalTwin,
    MarketLayout,
    SimulationMode,
    ZoneObservation,
    apply_event_triggers,
    apply_gate_closures,
    apply_scenario_overrides,
)
from app.simulation.risk import score_to_level

STEP_DURATION_SECONDS = 10

router = APIRouter(prefix="/simulate", tags=["simulate"])


def _validate_event_trigger_steps(events: list, steps: int) -> None:
    """triggerStep이 steps 범위를 벗어나면 그 이벤트는 영원히 발동하지 않고
    아무 경고도 없이 조용히 무시된다(스텝 루프가 triggerStep == 현재 스텝일
    때만 이벤트를 적용하기 때문). 사용자가 값을 잘못 넣었을 때 "화재를 냈는데
    반응이 없다"처럼 오해하기 쉬우므로, 요청 단계에서 미리 에러로 알려준다."""
    out_of_range = [e.triggerStep for e in events if e.triggerStep > steps]
    if out_of_range:
        raise HTTPException(
            status_code=400,
            detail=(
                f"이벤트 triggerStep({sorted(set(out_of_range))})이 steps({steps})를 "
                "초과해 해당 이벤트가 절대 발동하지 않습니다. triggerStep을 steps 이하로 "
                "설정하거나 steps를 늘려주세요."
            ),
        )


def _load_layout(market_id: int) -> MarketLayout:
    market = repo.fetch_market(market_id)
    if market is None:
        raise HTTPException(status_code=404, detail=f"시장을 찾을 수 없습니다: {market_id}")

    zones = repo.fetch_zones(market_id)
    if not zones:
        raise HTTPException(status_code=400, detail="구역 데이터가 없습니다")

    adjacency = repo.fetch_adjacency(market_id)
    gates = repo.fetch_gates(market_id)
    stalls = repo.fetch_stalls(market_id)
    # 2026-08-XX 변경: 이동 가능 영역은 통로 좌표(mrkadjc01m) 기반으로 계산하되,
    # 통로 데이터가 없는 구역(폴백 시 구역 폴리곤 전체를 쓰는 경우)에서도
    # 에이전트가 건물 안으로 들어가지 않도록 건물 폴리곤(mrkbldg01m)을 함께
    # 가져와 model.py에서 걸어다닐 수 있는 영역에서 뺀다.
    buildings = repo.fetch_buildings(market_id)
    return MarketLayout.from_db_rows(market, zones, adjacency, gates, stalls, buildings)


@router.post("/snapshot", response_model=SnapshotResponse)
def simulate_snapshot(req: SnapshotRequest) -> SnapshotResponse:
    layout = _load_layout(req.marketId)

    frames = repo.fetch_latest_pedestrian_frames(req.marketId, req.capturedAt)

    observations: dict[int, ZoneObservation] = {}
    zone_coord_ids: dict[int, int] = {}
    for row in frames:
        zid = row["zone_id"]
        observations[zid] = ZoneObservation(
            zone_id=zid,
            visitor_count=repo.count_people_in_frame(row["bev_xyz_json"]),
        )
        zone_coord_ids[zid] = row["coord_id"]

    model = MarketDigitalTwin(layout, observations, mode=SimulationMode.MIRROR)
    snap = model.snapshot()

    persisted = 0
    if req.persistRisk:
        # mrkrisk01m.coord_id는 NOT NULL FK라, 이번 조회에서 CCTV 프레임이
        # 없었던 구역(zone_coord_ids에 없음)은 저장 대상에서 제외한다.
        assessments = [
            {
                "coordId": zone_coord_ids.get(z["zoneId"]),
                "riskScore": z["riskScore"],
                "riskLevel": z["riskLevel"],
                "reason": z["reason"],
                "totalCount": z["visitorCount"],
            }
            for z in snap["zones"]
            if zone_coord_ids.get(z["zoneId"]) is not None
        ]
        persisted = repo.insert_risk_results(assessments)

    if not req.includeAgents:
        snap["agents"] = []

    return SnapshotResponse(**snap, persistedRiskRows=persisted)


def _frame_agents(model: MarketDigitalTwin) -> list[dict]:
    projection = model.layout.projection
    frame = []
    for agent in model.agents:
        lat, lon = projection.to_latlon(agent.x, agent.y)
        frame.append(
            {
                **agent.to_dict(),
                "latitude": round(lat, 8),
                "longitude": round(lon, 8),
            }
        )
    return frame


@router.post("/scenario", response_model=ScenarioResult)
def simulate_scenario(req: ScenarioRequest) -> ScenarioResult:
    """
    파이프라인 B: 사용자 지정 What-if 시나리오.

    2026-07-25: 대피 완료 판정 로직 변경. 예전엔 "대피 중인 사람이 전부
    출구 구역에 도착했다"만 확인했는데(실제 퇴장 동작이 없었음), 이제는
    사람이 게이트를 통과해 실제로 제거(퇴장)되므로, "한 번이라도 대피가
    시작됐고(ever_evacuating) 지금은 대피 중인 사람이 하나도 안 남았다"를
    기준으로 삼는다. 게이트가 다 닫혀 대피가 막히면 이 조건이 영영 안
    채워지고, 응답의 evacuationTimeSeconds는 None(대피 미완료)으로 남는다.
    """
    requested_at = datetime.now(timezone.utc)
    scenario_id = str(uuid.uuid4())

    _validate_event_trigger_steps(req.events, req.steps)

    layout = _load_layout(req.marketId)

    apply_scenario_overrides(layout, req.objects, req.corridorPolicies)
    apply_gate_closures(layout, set(req.closedGateIds))

    zone_ids = list(layout.zones.keys())
    if not zone_ids:
        raise HTTPException(status_code=400, detail="구역 데이터가 없습니다")

    total_area = sum(z.area_m2 for z in layout.zones.values())
    observations = {
        zid: ZoneObservation(
            zone_id=zid,
            visitor_count=int(req.agentCount * (layout.zones[zid].area_m2 / total_area)),
        )
        for zid in zone_ids
    }

    model = MarketDigitalTwin(layout, observations, mode=SimulationMode.SCENARIO, seed=42)
    # 2026-08-XX: 평상시(화재 없음) 유동인구를 이 목표치 근처로 유지한다.
    model.target_population = req.agentCount

    frames: list[list[dict]] = []
    risk_trend: list[RiskTrendPoint] = []
    evacuation_seconds: int | None = None
    for step_index in range(req.steps):
        # 2026-07-29 추가: 이벤트가 "시작하자마자 전부 발동"하는 대신, 각자
        # 지정된 triggerStep이 됐을 때만 발동하도록 스텝 루프 안에서 적용한다.
        # model.step() 호출 전에 적용해서, triggerStep=1(기본값)이면 예전처럼
        # 첫 스텝의 이동 판단에서부터 반영되게 한다.
        current_step_number = step_index + 1
        due_events = [e for e in req.events if e.triggerStep == current_step_number]
        if due_events:
            apply_event_triggers(model, due_events)

        model.step()
        frames.append(_frame_agents(model))
        # 2026-08-XX 추가: 개입 후(After)도 스텝별 위험도 추이를 기록해, FE에서
        # 개입 전(Before)과 겹쳐 비교할 수 있게 한다(개입 전과 동일한 형식).
        risk_trend.append(_risk_trend_point(model, step_index))

        if evacuation_seconds is None and model.ever_evacuating:
            still_evacuating = any(a.state is VisitorState.EVACUATING for a in model.agents)
            if not still_evacuating:
                evacuation_seconds = (step_index + 1) * STEP_DURATION_SECONDS

    risk_by_zone = model.risk
    if risk_by_zone:
        top = max(risk_by_zone.values(), key=lambda r: r.score)
        overall_score = top.score
        overall_level = top.level.value
        factors = ContributingFactors(
            density=top.density_score,
            bottleneck=top.bottleneck_score,
        )
        # 2026-07-27 추가: 마지막 스텝 기준 구역별 밀집도(명/m^2)의 평균/최댓값과,
        # 그 최댓값이 발생한 구역. BE가 simrslt01d에 그대로 적재하고, 보고서에서
        # "중앙통로에서 최대 X명/m^2" 같은 문장을 만드는 데 쓴다.
        densities = [r.density for r in risk_by_zone.values()]
        average_density = sum(densities) / len(densities)
        max_density_zone_id, max_density_assessment = max(
            risk_by_zone.items(), key=lambda item: item[1].density
        )
        max_density = max_density_assessment.density
        max_density_zone_name = layout.zones[max_density_zone_id].zone_name
    else:
        overall_score = 0.0
        overall_level = score_to_level(0.0).value
        factors = ContributingFactors(density=0.0, bottleneck=0.0)
        average_density = 0.0
        max_density = 0.0
        max_density_zone_id = None
        max_density_zone_name = None

    final_timestamp = requested_at + timedelta(seconds=req.steps * STEP_DURATION_SECONDS)

    return ScenarioResult(
        scenarioId=scenario_id,
        requestedAt=requested_at,
        frames=frames,
        riskTrend=risk_trend,
        evacuationTimeSeconds=evacuation_seconds,
        finalRiskScore=RiskScoreDto(
            timestamp=final_timestamp,
            score=overall_score,
            level=overall_level,
            contributingFactors=factors,
        ),
        averageDensity=average_density,
        maxDensity=max_density,
        maxDensityZoneId=max_density_zone_id,
        maxDensityZoneName=max_density_zone_name,
        evacuatedCount=model.evacuated_count,
    )


def _risk_trend_point(model: MarketDigitalTwin, step_index: int) -> RiskTrendPoint:
    zones = [
        ZoneRiskPoint(zoneId=zid, riskScore=r.score, riskLevel=r.level.value)
        for zid, r in model.risk.items()
    ]
    overall = max((r.score for r in model.risk.values()), default=0.0)
    return RiskTrendPoint(step=step_index + 1, overallRiskScore=overall, zones=zones)


@router.post("/predict", response_model=PredictResult)
def simulate_predict(req: PredictRequest) -> PredictResult:
    """
    파이프라인 A': Before(개입 전) 시뮬레이션.

    2026-08-XX 변경: Before가 오브젝트/이벤트 개입이 하나도 없을 때는
    After(/scenario)와 완전히 동일하게 동작하도록 통일했다. 예전에는 Before가
    실측 CCTV 위치(fetch_latest_pedestrian_frames)에서 출발해 totalInflow만큼
    게이트로 서서히 유입되는 방식이었고, After는 agentCount를 구역 면적
    비례로 한 번에 배치하는 방식이라 두 시뮬레이션의 "기준선" 자체가 처음부터
    달랐다 - 그러면 오브젝트/이벤트를 하나도 안 넣어도 두 결과가 다르게 나와서
    뭐가 개입 때문에 달라진 건지 비교가 안 됐다.

    이제 totalInflow(FE에서는 "인원 수" 입력값)를 ScenarioRequest.agentCount와
    동일하게 "구역 면적 비례로 시작 시점에 한 번에 배치하는 인원 수"로 다루고,
    실행 중 게이트로 서서히 유입되는 로직(inject_inflow)은 더 이상 쓰지 않는다.
    시드도 명시적으로 안 주면 scenario와 같은 42를 쓴다. 따라서 개입 전과 후에
    동일한 입력(현행)을 주면 두 시뮬레이션이 프레임 단위로 완전히 동일하게 나오고,
    After에서 사용자가 오브젝트/통로정책/게이트를 삭제·추가한 부분만 순수하게 그
    개입의 효과로 드러난다.

    2026-08-12 변경: 오브젝트 배치/통로정책도 Before가 받는다(objects/corridorPolicies).
    시장 구조 등록에서 저장한 "현행"을 개입 전에도 반영해야 Before가 실제 시장 모습이
    되고, 비교가 "현행 vs 현행+개입"으로 성립하기 때문이다. (게이트 폐쇄는 이전부터
    Before가 반영하고 있었다.) capturedAt은 더 이상 초기 배치에 쓰이지 않는다(과거
    요청과의 하위 호환을 위해 필드는 남겨둠).
    """
    requested_at = datetime.now(timezone.utc)
    prediction_id = str(uuid.uuid4())

    _validate_event_trigger_steps(req.events, req.steps)

    layout = _load_layout(req.marketId)

    # 2026-08-12: 현행 오브젝트/통로정책을 Before에도 반영(scenario와 동일 순서).
    apply_scenario_overrides(layout, req.objects, req.corridorPolicies)
    # 2026-08-XX: 게이트 개폐는 개입 전/후 독립적으로 조절 가능하므로 Before도 반영.
    apply_gate_closures(layout, set(req.closedGateIds))

    zone_ids = list(layout.zones.keys())
    if not zone_ids:
        raise HTTPException(status_code=400, detail="구역 데이터가 없습니다")

    total_area = sum(z.area_m2 for z in layout.zones.values())
    observations: dict[int, ZoneObservation] = {
        zid: ZoneObservation(
            zone_id=zid,
            visitor_count=int(req.totalInflow * (layout.zones[zid].area_m2 / total_area)),
        )
        for zid in zone_ids
    }

    seed = req.seed if req.seed is not None else 42
    model = MarketDigitalTwin(layout, observations, mode=SimulationMode.SCENARIO, seed=seed)
    # 2026-08-XX: 평상시 유동인구를 이 목표치 근처로 유지한다.
    model.target_population = req.totalInflow

    frames: list[list[dict]] = []
    risk_trend: list[RiskTrendPoint] = []
    evacuation_seconds: int | None = None
    for step_index in range(req.steps):
        # 2026-08-XX 추가: 화재 이벤트를 지정된 triggerStep에 발동시킨다.
        # simulate_scenario()의 이벤트 발동 루프와 동일한 로직.
        current_step_number = step_index + 1
        due_events = [e for e in req.events if e.triggerStep == current_step_number]
        if due_events:
            apply_event_triggers(model, due_events)

        model.step()
        frames.append(_frame_agents(model))
        risk_trend.append(_risk_trend_point(model, step_index))

        # 2026-08-XX 추가: 개입 전(Before)도 대피 완료 시간을 계산해, 개입 후와
        # 나란히 비교할 수 있게 한다(scenario와 동일한 판정: 한 번이라도 대피가
        # 시작됐고 지금은 대피 중인 사람이 없으면 그 시점을 완료로 본다).
        if evacuation_seconds is None and model.ever_evacuating:
            still_evacuating = any(a.state is VisitorState.EVACUATING for a in model.agents)
            if not still_evacuating:
                evacuation_seconds = (step_index + 1) * STEP_DURATION_SECONDS

    total_agent_count = sum(obs.visitor_count for obs in observations.values())

    risk_by_zone = model.risk
    if risk_by_zone:
        densities = [r.density for r in risk_by_zone.values()]
        average_density = sum(densities) / len(densities) if densities else 0.0
        max_density_zone_id, max_density_assessment = max(
            risk_by_zone.items(), key=lambda item: item[1].density
        )
        max_density = max_density_assessment.density
        max_density_zone_name = layout.zones[max_density_zone_id].zone_name
    else:
        average_density = 0.0
        max_density = 0.0
        max_density_zone_id = None
        max_density_zone_name = None

    final_overall = risk_trend[-1].overallRiskScore if risk_trend else 0.0

    return PredictResult(
        predictionId=prediction_id,
        requestedAt=requested_at,
        frames=frames,
        riskTrend=risk_trend,
        finalOverallRiskScore=final_overall,
        agentCount=total_agent_count,
        averageDensity=average_density,
        maxDensity=max_density,
        maxDensityZoneId=max_density_zone_id,
        maxDensityZoneName=max_density_zone_name,
        evacuatedCount=model.evacuated_count,
        evacuationTimeSeconds=evacuation_seconds,
    )