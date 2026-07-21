"""
시장 디지털 트윈 Mesa 모델.

두 가지 운영 모드를 지원한다.
  - MIRROR   : 파이프라인 A. 센서 실측값을 그대로 반영해 현재 상태를 재현한다.
  - SCENARIO : 파이프라인 B. 초기 상태만 잡고 이후는 시뮬레이션 규칙으로 전개한다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum

import networkx as nx
from mesa import Model
from shapely.geometry import Point, Polygon

from app.simulation.agents import VisitorAgent
from app.simulation.placement import (
    PlacementStrategy,
    place_visitors,
)
from app.simulation.risk import RiskAssessment, assess_zone
from app.simulation.space import LocalProjection, effective_width_m, parse_polygon


class SimulationMode(str, Enum):
    MIRROR = "mirror"
    SCENARIO = "scenario"


@dataclass
class ZoneSpec:
    """시뮬레이션에 사용되는 구역 정의 (DB에서 로드된 공간 데이터)."""

    zone_id: int
    zone_name: str
    polygon_local: Polygon
    area_m2: float
    path_width_m: float
    is_exit_zone: bool = False
    """출입구가 있는 구역인지 여부. 대피 경로 계산의 목적지가 된다."""


@dataclass
class ZoneObservation:
    """센서에서 관측된 구역별 실측값."""

    zone_id: int
    visitor_count: int = 0
    avg_speed_cm_s: float | None = None
    acoustic_event_count: int = 0
    acoustic_max_confidence: float | None = None


@dataclass
class MarketLayout:
    """시장 전체 공간 구조."""

    market_id: int
    market_name: str
    projection: LocalProjection
    zones: dict[int, ZoneSpec]
    graph: nx.Graph
    gates: list[dict] = field(default_factory=list)

    @classmethod
    def from_db_rows(
        cls,
        market_row: dict,
        zone_rows: list[dict],
        adjacency_rows: list[dict],
        gate_rows: list[dict],
    ) -> "MarketLayout":
        """
        DB 조회 결과로부터 레이아웃을 구성한다.

        market_row  : mrkaddr01m 1행
        zone_rows   : mrkaddr01d 목록 (polygon_coordinates는 GeoJSON 문자열)
        adjacency_rows: mrkadjc01m 목록 (is_active=True인 것만 전달할 것)
        gate_rows   : mrkfcts01m 중 facility_type='GATE' 목록
        """
        projection = LocalProjection(
            origin_lat=float(market_row["latitude"]),
            origin_lon=float(market_row["longitude"]),
        )

        # 출입구가 속한 구역을 판정하기 위해 먼저 폴리곤을 만든다.
        zones: dict[int, ZoneSpec] = {}
        for row in zone_rows:
            poly_wgs = parse_polygon(row["polygon_coordinates"])
            poly_local = projection.polygon_to_local(poly_wgs)
            zones[row["zone_id"]] = ZoneSpec(
                zone_id=row["zone_id"],
                zone_name=row["zone_name"],
                polygon_local=poly_local,
                area_m2=poly_local.area,
                path_width_m=effective_width_m(poly_local),
            )

        gates: list[dict] = []
        for row in gate_rows:
            if row.get("latitude") is None or row.get("longitude") is None:
                continue
            gx, gy = projection.to_local(float(row["latitude"]), float(row["longitude"]))
            # 출입구는 폴리곤 경계 바로 바깥(수 m 이내)에 위치할 수 있으므로
            # 가장 가까운 구역에 귀속시킨다.
            gate_point = Point(gx, gy)
            nearest = min(
                zones.values(),
                key=lambda z: z.polygon_local.distance(gate_point),
                default=None,
            )
            gate = {
                "facility_id": row.get("facility_id"),
                "name": row.get("name"),
                "x": gx,
                "y": gy,
                "zone_id": nearest.zone_id if nearest else None,
            }
            gates.append(gate)
            if nearest is not None:
                nearest.is_exit_zone = True

        graph = nx.Graph()
        for zone_id in zones:
            graph.add_node(zone_id)
        for row in adjacency_rows:
            graph.add_edge(
                row["from_zone_id"],
                row["to_zone_id"],
                weight=float(row.get("distance_m") or 1.0),
                path_width=float(row.get("path_width") or 0.0),
            )

        return cls(
            market_id=market_row["market_id"],
            market_name=market_row["market_name"],
            projection=projection,
            zones=zones,
            graph=graph,
            gates=gates,
        )


class MarketDigitalTwin(Model):
    """시장 디지털 트윈 모델."""

    def __init__(
        self,
        layout: MarketLayout,
        observations: dict[int, ZoneObservation],
        mode: SimulationMode = SimulationMode.MIRROR,
        placement_strategy: PlacementStrategy = PlacementStrategy.CENTERLINE,
        seed: int | None = None,
    ) -> None:
        super().__init__(seed=seed)
        self.layout = layout
        self.observations = observations
        self.mode = mode
        self.placement_strategy = placement_strategy
        self._rng = random.Random(seed)

        self._risk: dict[int, RiskAssessment] = {}
        self._exit_hops: dict[int, int] = self._compute_exit_hops()

        self._spawn_agents()
        self.evaluate_risk()

    # ---------- 초기화 ----------

    def _compute_exit_hops(self) -> dict[int, int]:
        """각 구역에서 가장 가까운 출구 구역까지의 홉 수."""
        exit_zones = [z.zone_id for z in self.layout.zones.values() if z.is_exit_zone]
        if not exit_zones:
            return {zid: 0 for zid in self.layout.zones}

        hops: dict[int, int] = {}
        for zone_id in self.layout.zones:
            best = None
            for exit_id in exit_zones:
                try:
                    d = nx.shortest_path_length(self.layout.graph, zone_id, exit_id)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                best = d if best is None else min(best, d)
            hops[zone_id] = best if best is not None else 0
        return hops

    def _spawn_agents(self) -> None:
        """관측된 인원수만큼 각 구역 폴리곤 내부에 에이전트를 배치한다."""
        for zone_id, spec in self.layout.zones.items():
            obs = self.observations.get(zone_id)
            count = obs.visitor_count if obs else 0
            if count <= 0:
                continue

            points = place_visitors(
                spec.polygon_local,
                count,
                strategy=self.placement_strategy,
                seed=self._rng.randint(0, 2**31 - 1),
            )
            for x, y in points:
                VisitorAgent(self, zone_id=zone_id, x=x, y=y)

    # ---------- 위험도 ----------

    def evaluate_risk(self) -> dict[int, RiskAssessment]:
        """현재 상태 기준으로 구역별 위험도를 재계산한다."""
        counts = self.current_zone_counts()
        self._risk = {}
        for zone_id, spec in self.layout.zones.items():
            obs = self.observations.get(zone_id)
            self._risk[zone_id] = assess_zone(
                zone_id=zone_id,
                visitor_count=counts.get(zone_id, 0),
                area_m2=spec.area_m2,
                avg_speed_cm_s=obs.avg_speed_cm_s if obs else None,
                acoustic_event_count=obs.acoustic_event_count if obs else 0,
                acoustic_max_confidence=obs.acoustic_max_confidence if obs else None,
                path_width_m=spec.path_width_m,
            )
        return self._risk

    def zone_risk_score(self, zone_id: int) -> float:
        assessment = self._risk.get(zone_id)
        return assessment.score if assessment else 0.0

    @property
    def risk(self) -> dict[int, RiskAssessment]:
        return self._risk

    # ---------- 에이전트 지원 ----------

    def current_zone_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {zid: 0 for zid in self.layout.zones}
        for agent in self.agents:
            counts[agent.zone_id] = counts.get(agent.zone_id, 0) + 1
        return counts

    def next_zone_toward_exit(self, zone_id: int) -> int | None:
        """출구에 더 가까운 인접 구역을 반환한다."""
        current_hops = self._exit_hops.get(zone_id)
        if current_hops is None or current_hops == 0:
            return zone_id
        for neighbor in self.layout.graph.neighbors(zone_id):
            if self._exit_hops.get(neighbor, 99) < current_hops:
                return neighbor
        return zone_id

    def random_point_in_zone(self, zone_id: int) -> tuple[float, float]:
        spec = self.layout.zones.get(zone_id)
        if spec is None:
            return 0.0, 0.0
        pts = place_visitors(
            spec.polygon_local,
            1,
            strategy=self.placement_strategy,
            seed=self._rng.randint(0, 2**31 - 1),
        )
        return pts[0] if pts else (0.0, 0.0)

    # ---------- 실행 ----------

    def step(self) -> None:
        """한 타임스텝 진행. MIRROR 모드에서는 위험도만 재평가한다."""
        if self.mode is SimulationMode.SCENARIO:
            self.agents.shuffle_do("step")
        self.evaluate_risk()

    def snapshot(self) -> dict:
        """현재 상태를 API 응답용 dict로 직렬화한다."""
        projection = self.layout.projection
        agents = []
        for agent in self.agents:
            lat, lon = projection.to_latlon(agent.x, agent.y)
            agents.append(
                {
                    **agent.to_dict(),
                    "latitude": round(lat, 8),
                    "longitude": round(lon, 8),
                }
            )

        counts = self.current_zone_counts()
        zones = []
        for zone_id, spec in self.layout.zones.items():
            r = self._risk[zone_id]
            zones.append(
                {
                    "zoneId": zone_id,
                    "zoneName": spec.zone_name,
                    "areaM2": round(spec.area_m2, 2),
                    "pathWidthM": round(spec.path_width_m, 2),
                    "visitorCount": counts.get(zone_id, 0),
                    "density": r.density,
                    "personalSpace": r.personal_space,
                    "riskScore": r.score,
                    "riskLevel": r.level.value,
                    "reason": r.reason,
                    "breakdown": {
                        "density": r.density_score,
                        "flow": r.flow_score,
                        "acoustic": r.acoustic_score,
                        "bottleneck": r.bottleneck_score,
                    },
                }
            )

        overall = max((z["riskScore"] for z in zones), default=0.0)
        return {
            "marketId": self.layout.market_id,
            "marketName": self.layout.market_name,
            "mode": self.mode.value,
            "step": self.steps,
            "overallRiskScore": overall,
            "zones": zones,
            "agents": agents,
        }
