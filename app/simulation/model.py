"""
시장 디지털 트윈 Mesa 모델.

두 가지 운영 모드를 지원한다.
  - MIRROR   : 파이프라인 A. 센서 실측값을 그대로 반영해 현재 상태를 재현한다.
  - SCENARIO : 파이프라인 B. 초기 상태만 잡고 이후는 시뮬레이션 규칙으로 전개한다.
              오브젝트(푸드트럭/장애물/행사존/휴게공간)와 통로 정책(폐쇄/개방/일방통행)을
              반영해 결과가 배치에 따라 달라지도록 한다.
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
class ZoneModifier:
    """PlacedObject(오브젝트 배치)가 구역에 미치는 누적 효과."""

    path_width_multiplier: float = 1.0
    """장애물로 인한 통로 폭 감소 배율 (1.0 = 영향 없음)."""
    attraction: float = 0.0
    """0~1. 높을수록 에이전트가 이 구역으로 이동하려는 경향이 커짐 (푸드트럭/행사존)."""
    occupancy_boost: float = 1.0
    """행사존 체류 효과. 실제 인원보다 밀집도 계산 시 부풀리는 배율."""
    density_relief: float = 1.0
    """휴게공간 분산 효과. 1보다 작으면 밀집도를 낮춰서 계산."""


@dataclass
class MarketLayout:
    """시장 전체 공간 구조."""

    market_id: int
    market_name: str
    projection: LocalProjection
    zones: dict[int, ZoneSpec]
    graph: nx.Graph
    gates: list[dict] = field(default_factory=list)
    raw_adjacency: list[dict] = field(default_factory=list)
    """활성/비활성 여부와 무관하게 원본 인접 행을 그대로 보관.
    통로 정책(폐쇄된 통로 개방 등)을 적용하려면 비활성 통로 정보도 필요하다."""

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
        adjacency_rows: mrkadjc01m 목록 (통로 정책 적용을 위해 활성/비활성 전부 전달할 것)
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
            if not row.get("is_active", True):
                continue
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
            raw_adjacency=adjacency_rows,
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
        objects: list | None = None,
        corridor_policies: list | None = None,
    ) -> None:
        super().__init__(seed=seed)
        self.layout = layout
        self.observations = observations
        self.mode = mode
        self.placement_strategy = placement_strategy
        self._rng = random.Random(seed)

        # 통로 정책(폐쇄/개방/일방통행)을 반영한 실제 이동 가능 그래프.
        # SCENARIO 모드가 아니거나 정책이 없으면 기존 layout.graph와 동일하게 동작한다.
        self.movement_graph = self._build_movement_graph(corridor_policies or [])
        self._modifiers: dict[int, ZoneModifier] = self._build_modifiers(objects or [])

        self._risk: dict[int, RiskAssessment] = {}
        self._exit_hops: dict[int, int] = self._compute_exit_hops()

        self._spawn_agents()
        self.evaluate_risk()

    # ---------- 초기화 ----------

    def _build_movement_graph(self, policies: list) -> nx.DiGraph:
        """
        통로 정책(폐쇄/개방/일방통행)을 반영한 실제 이동 가능 그래프.

        기본은 DB의 is_active 값을 따르되, 정책이 있으면 그걸로 덮어쓴다.
        일방통행이 있을 수 있으므로 방향 그래프(DiGraph)로 구성한다.
        """
        policy_map = {}
        for p in policies:
            key = tuple(sorted((p.fromZoneId, p.toZoneId)))
            policy_map[key] = p

        working = nx.DiGraph()
        working.add_nodes_from(self.layout.zones.keys())

        for row in self.layout.raw_adjacency:
            a, b = row["from_zone_id"], row["to_zone_id"]
            key = tuple(sorted((a, b)))
            policy = policy_map.get(key)
            base_open = bool(row.get("is_active", True))
            weight = float(row.get("distance_m") or 1.0)

            if policy is None:
                if base_open:
                    working.add_edge(a, b, weight=weight)
                    working.add_edge(b, a, weight=weight)
                continue

            if policy.action == "close":
                continue  # 양방향 다 막음
            if policy.action == "open":
                working.add_edge(a, b, weight=weight)
                working.add_edge(b, a, weight=weight)
            elif policy.action == "one_way":
                if policy.allowedDirection == "to_from":
                    working.add_edge(b, a, weight=weight)
                else:  # 기본값: from_to
                    working.add_edge(a, b, weight=weight)

        return working

    def _build_modifiers(self, objects: list) -> dict[int, ZoneModifier]:
        """PlacedObject 목록을 구역별 누적 효과로 변환한다."""
        mods: dict[int, ZoneModifier] = {
            zid: ZoneModifier() for zid in self.layout.zones
        }
        for obj in objects:
            mod = mods.get(obj.zoneId)
            if mod is None:
                continue  # 존재하지 않는 구역은 무시
            if obj.objectType == "obstacle":
                # 강도만큼 통로 폭을 줄임. 최소 15%는 남겨서 완전 봉쇄는 방지.
                mod.path_width_multiplier = min(
                    mod.path_width_multiplier, max(0.15, 1.0 - obj.intensity)
                )
            elif obj.objectType == "food_truck":
                mod.attraction = max(mod.attraction, obj.intensity * 0.6)
            elif obj.objectType == "event_zone":
                mod.attraction = max(mod.attraction, obj.intensity * 0.9)
                mod.occupancy_boost = max(mod.occupancy_boost, 1.0 + obj.intensity * 0.8)
            elif obj.objectType == "rest_area":
                mod.density_relief = min(mod.density_relief, max(0.4, 1.0 - obj.intensity * 0.6))
        return mods

    def attraction_of(self, zone_id: int) -> float:
        mod = self._modifiers.get(zone_id)
        return mod.attraction if mod else 0.0

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
                    d = nx.shortest_path_length(self.movement_graph, zone_id, exit_id)
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
        """현재 상태 기준으로 구역별 위험도를 재계산한다. 오브젝트 효과를 반영한다."""
        counts = self.current_zone_counts()
        self._risk = {}
        for zone_id, spec in self.layout.zones.items():
            obs = self.observations.get(zone_id)
            mod = self._modifiers.get(zone_id, ZoneModifier())
            effective_count = counts.get(zone_id, 0) * mod.occupancy_boost * mod.density_relief
            self._risk[zone_id] = assess_zone(
                zone_id=zone_id,
                visitor_count=round(effective_count),
                area_m2=spec.area_m2,
                avg_speed_cm_s=obs.avg_speed_cm_s if obs else None,
                acoustic_event_count=obs.acoustic_event_count if obs else 0,
                acoustic_max_confidence=obs.acoustic_max_confidence if obs else None,
                path_width_m=spec.path_width_m * mod.path_width_multiplier,
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
        for neighbor in self.movement_graph.neighbors(zone_id):
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