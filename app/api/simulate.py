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
    return MarketLayout.from_db_rows(market, zones, adjacency, gates, stalls)


@router.post("/snapshot", response_model=SnapshotResponse)
def simulate_snapshot(req: SnapshotRequest) -> SnapshotResponse:
    layout = _load_layout(req.marketId)

    densities = repo.fetch_crowd_density(req.marketId, req.capturedAt)

    observations: dict[int, ZoneObservation] = {}
    for row in densities:
        zid = row["zone_id"]
        observations[zid] = ZoneObservation(
            zone_id=zid,
            visitor_count=row["visitor_count"] or 0,
        )

    model = MarketDigitalTwin(layout, observations, mode=SimulationMode.MIRROR)
    snap = model.snapshot()

    persisted = 0
    if req.persistRisk:
        persisted = repo.insert_risk_results(snap["zones"])

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

    # 2026-07-25 추가: 화재/음향 이상 이벤트 반영. 에이전트가 스폰된 뒤(model 생성 후)
    # 호출해야 음향 이상의 반경 판정이 실제 좌표 기준으로 정확히 이뤄진다.
    apply_event_triggers(model, req.events)

    frames: list[list[dict]] = []
    evacuation_seconds: int | None = None
    for step_index in range(req.steps):
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
    else:
        overall_score = 0.0
        overall_level = score_to_level(0.0).value
        factors = ContributingFactors(density=0.0, bottleneck=0.0)

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

    densities = repo.fetch_crowd_density(req.marketId, req.capturedAt)
    observations: dict[int, ZoneObservation] = {}
    for row in densities:
        zid = row["zone_id"]
        observations[zid] = ZoneObservation(
            zone_id=zid,
            visitor_count=row["visitor_count"] or 0,
        )

    model = MarketDigitalTwin(
        layout, observations, mode=SimulationMode.SCENARIO, seed=req.seed
    )

    inflow_schedule = _build_inflow_schedule(req.totalInflow, req.steps, req.seed)

    frames: list[list[dict]] = []
    risk_trend: list[RiskTrendPoint] = []
    for step_index in range(req.steps):
        if inflow_schedule[step_index] > 0:
            model.inject_inflow(inflow_schedule[step_index])
        model.step()
        frames.append(_frame_agents(model))
        risk_trend.append(_risk_trend_point(model, step_index))

    final_overall = risk_trend[-1].overallRiskScore if risk_trend else 0.0

    return PredictResult(
        predictionId=prediction_id,
        requestedAt=requested_at,
        frames=frames,
        riskTrend=risk_trend,
        finalOverallRiskScore=final_overall,
    )