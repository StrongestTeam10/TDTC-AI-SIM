"""시뮬레이션 엔드포인트."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from app.db import repository as repo
from app.schemas.models import (
    ContributingFactors,
    RiskScoreDto,
    ScenarioRequest,
    ScenarioResult,
    SnapshotRequest,
    SnapshotResponse,
)
from app.simulation.agents import VisitorState
from app.simulation.model import (
    MarketDigitalTwin,
    MarketLayout,
    SimulationMode,
    ZoneObservation,
)
from app.simulation.risk import score_to_level

# 시나리오 시뮬레이션의 1 step이 실제 몇 초에 해당하는지에 대한 가정값.
# 현재 이동 모델(VisitorAgent._move_toward_exit)은 거리와 무관하게 매 스텝마다
# 인접 구역 1칸을 이동하므로, 실측 보행 속도에 기반한 정확한 환산이 아니라
# 대피소요시간을 대략적으로 가늠하기 위한 임시 캘리브레이션 값이다.
# 추후 구역 간 실제 거리(mrkadjc01m.distance_m)와 평균 보행속도를 반영해 재산정 필요.
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
    return MarketLayout.from_db_rows(market, zones, adjacency, gates)


@router.post("/snapshot", response_model=SnapshotResponse)
def simulate_snapshot(req: SnapshotRequest) -> SnapshotResponse:
    """
    파이프라인 A: 센서 실측값을 로드해 오브젝트를 배치하고 위험도를 산출한다.

    CCTV(인구 밀집도)만 반영한다. 레이더(이동 속도)/음향(이상 이벤트)은
    2026-07-23부로 완전히 제거되었다 (관련 테이블·리포지토리 함수·위험도
    가중치 항목까지 전부 삭제).
    """
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
    """현재 스텝의 에이전트 상태를 AgentState 스키마 형태(dict)로 직렬화."""
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

    레이아웃 로드 → 초기 배치 → steps만큼 진행하며 매 스텝의 에이전트 상태를
    frames로 누적하고, 대피 완료 시점과 최종 위험도를 산출해 반환한다.

    주의: eventZoneId/eventIntensity/scenarioType으로 지정하는 화재/음향전파 등
    "외부 충격 이벤트"는 아직 구현되어 있지 않다. 현재는 밀집도 기반 위험도가
    임계치를 넘으면 에이전트가 자발적으로 대피를 시작하는 기본 이동 모델만 동작한다.
    """
    requested_at = datetime.now(timezone.utc)
    scenario_id = str(uuid.uuid4())

    layout = _load_layout(req.marketId)

    zone_ids = list(layout.zones.keys())
    if not zone_ids:
        raise HTTPException(status_code=400, detail="구역 데이터가 없습니다")

    # 면적 비례로 초기 인원 배분
    total_area = sum(z.area_m2 for z in layout.zones.values())
    observations = {
        zid: ZoneObservation(
            zone_id=zid,
            visitor_count=int(req.agentCount * (layout.zones[zid].area_m2 / total_area)),
        )
        for zid in zone_ids
    }

    model = MarketDigitalTwin(layout, observations, mode=SimulationMode.SCENARIO, seed=42)
    exit_zone_ids = {zid for zid, spec in layout.zones.items() if spec.is_exit_zone}

    frames: list[list[dict]] = []
    evacuation_seconds: int | None = None
    for step_index in range(req.steps):
        model.step()
        frames.append(_frame_agents(model))

        if evacuation_seconds is None:
            evacuating = [a for a in model.agents if a.state is VisitorState.EVACUATING]
            if evacuating and all(a.zone_id in exit_zone_ids for a in evacuating):
                evacuation_seconds = (step_index + 1) * STEP_DURATION_SECONDS

    # 최종 위험도: 구역 중 가장 높은 점수를 종합 위험도로 삼는다 (snapshot()의 overallRiskScore와 동일 기준).
    # contributingFactors는 그 최고 위험 구역의 세부 지표를 그대로 사용한다.
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