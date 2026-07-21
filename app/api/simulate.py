"""시뮬레이션 엔드포인트."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.db import repository as repo
from app.schemas.models import ScenarioRequest, SnapshotRequest, SnapshotResponse
from app.simulation.model import (
    MarketDigitalTwin,
    MarketLayout,
    SimulationMode,
    ZoneObservation,
)

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

    CCTV(인구 밀집도) + 레이더(이동 속도) + 음향(이상 이벤트)을 종합한다.
    """
    layout = _load_layout(req.marketId)

    densities = repo.fetch_crowd_density(req.marketId, req.capturedAt)
    speeds = repo.fetch_radar_speed(req.marketId)
    acoustics = repo.fetch_acoustic_events(req.marketId)

    observations: dict[int, ZoneObservation] = {}
    for row in densities:
        zid = row["zone_id"]
        ac = acoustics.get(zid, {})
        observations[zid] = ZoneObservation(
            zone_id=zid,
            visitor_count=row["visitor_count"] or 0,
            avg_speed_cm_s=speeds.get(zid),
            acoustic_event_count=ac.get("count", 0),
            acoustic_max_confidence=ac.get("max_confidence"),
        )

    model = MarketDigitalTwin(layout, observations, mode=SimulationMode.MIRROR)
    snap = model.snapshot()

    persisted = 0
    if req.persistRisk:
        persisted = repo.insert_risk_results(req.marketId, snap["zones"])

    if not req.includeAgents:
        snap["agents"] = []

    return SnapshotResponse(**snap, persistedRiskRows=persisted)


@router.post("/scenario")
def simulate_scenario(req: ScenarioRequest) -> dict:
    """
    파이프라인 B: 사용자 지정 What-if 시나리오.

    현재는 레이아웃 로드와 초기 배치까지만 구현되어 있으며,
    화재 확산/음향 전파 등 이벤트 모델은 후속 작업으로 남아 있다.
    """
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

    frames = []
    for _ in range(req.steps):
        model.step()
        frames.append(model.snapshot())

    return {
        "scenarioType": req.scenarioType,
        "steps": req.steps,
        "finalSnapshot": frames[-1] if frames else None,
        "note": "이벤트 모델(화재/음향 전파) 미구현 - 현재는 기본 이동/위험도 평가만 수행",
    }
