"""시뮬레이션 엔드포인트."""
from __future__ import annotations

import random
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
    # 2026-08-XX 변경: 이동 가능 영역을 이제 통로 좌표(mrkadjc01m) 기반으로
    # 계산해서, 건물 데이터는 더 이상 여기서 안 쓴다(model.py 참고). 건물은
    # 지도 표시(FE)용으로만 BE의 /buildings API를 통해 쓰인다.
    return MarketLayout.from_db_rows(market, zones, adjacency, gates, stalls)
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

    frames: list[list[dict]] = []
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


def _build_inflow_schedule(total: int, steps: int, seed: int | None) -> list[int]:
    if total <= 0 or steps <= 0:
        return [0] * steps
    rng = random.Random(seed)
    weights = [rng.random() for _ in range(steps)]
    weight_sum = sum(weights) or 1.0
    schedule = [round(total * w / weight_sum) for w in weights]
    return schedule


def _risk_trend_point(model: MarketDigitalTwin, step_index: int) -> RiskTrendPoint:
    zones = [
        ZoneRiskPoint(zoneId=zid, riskScore=r.score, riskLevel=r.level.value)
        for zid, r in model.risk.items()
    ]
    overall = max((r.score for r in model.risk.values()), default=0.0)
    return RiskTrendPoint(step=step_index + 1, overallRiskScore=overall, zones=zones)


@router.post("/predict", response_model=PredictResult)
def simulate_predict(req: PredictRequest) -> PredictResult:
    requested_at = datetime.now(timezone.utc)
    prediction_id = str(uuid.uuid4())

    layout = _load_layout(req.marketId)

    densities = repo.fetch_latest_pedestrian_frames(req.marketId, req.capturedAt)
    observations: dict[int, ZoneObservation] = {}
    for row in densities:
        zid = row["zone_id"]
        observations[zid] = ZoneObservation(
            zone_id=zid,
            visitor_count=repo.count_people_in_frame(row["bev_xyz_json"]),
        )

    model = MarketDigitalTwin(
        layout, observations, mode=SimulationMode.SCENARIO, seed=req.seed
    )

    inflow_schedule = _build_inflow_schedule(req.totalInflow, req.steps, req.seed)

    frames: list[list[dict]] = []
    risk_trend: list[RiskTrendPoint] = []
    for step_index in range(req.steps):
        # 2026-08-XX 추가: 화재/음향이상 이벤트를 지정된 triggerStep에 발동시킨다.
        # simulate_scenario()의 이벤트 발동 루프와 동일한 로직.
        current_step_number = step_index + 1
        due_events = [e for e in req.events if e.triggerStep == current_step_number]
        if due_events:
            apply_event_triggers(model, due_events)

        if inflow_schedule[step_index] > 0:
            model.inject_inflow(inflow_schedule[step_index])
        model.step()
        frames.append(_frame_agents(model))
        risk_trend.append(_risk_trend_point(model, step_index))

    initial_count = sum(obs.visitor_count for obs in observations.values())
    total_agent_count = initial_count + req.totalInflow

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
    )