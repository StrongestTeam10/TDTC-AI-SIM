"""
시장 디지털 트윈 Mesa 모델.

두 가지 운영 모드를 지원한다.
  - MIRROR   : 파이프라인 A. 센서 실측값을 그대로 반영해 현재 상태를 재현한다.
  - SCENARIO : 파이프라인 B. 초기 상태만 잡고 이후는 시뮬레이션 규칙으로 전개한다.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum

import networkx as nx
from mesa import Model
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from app.schemas.models import CorridorPolicy, EventTrigger, PlacedObject
from app.simulation.agents import VisitorAgent, VisitorState
from app.simulation.gridspace import WalkableGrid
from app.simulation.placement import (
    PlacementStrategy,
    place_visitors,
)
from app.simulation.risk import RiskAssessment, assess_zone, score_to_level
from app.simulation.space import (
    LocalProjection,
    effective_width_m,
    parse_linestring,
    parse_polygon,
)


class SimulationMode(str, Enum):
    MIRROR = "mirror"
    SCENARIO = "scenario"


DEFAULT_FACILITY_RADIUS_M = 1.2
"""시설(매대 등)의 물리적 점유 반경 기본값(m). 실제 크기 데이터
(mrkfcts01m.footprint_radius_m)가 없을 때 쓰는 임시 근사치."""

DEFAULT_CORRIDOR_WIDTH_M = 4.0
"""2026-08-XX 추가: 통로 폭(mrkadjc01m.path_width)이 비어있을 때 쓰는 기본값(m).
전통시장 골목 실측 폭 참고."""


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
    attraction: float = 0.0
    """구역에 속한 매대(오브젝트) weight 합 - VisitorAgent의 정상 보행 이동에 사용."""


@dataclass
class ZoneObservation:
    """센서에서 관측된 구역별 실측값. (레이더/음향 관측값은 2026-07-23부로 완전 제거)"""

    zone_id: int
    visitor_count: int = 0


@dataclass
class MarketLayout:
    """시장 전체 공간 구조."""

    market_id: int
    market_name: str
    projection: LocalProjection
    zones: dict[int, ZoneSpec]
    graph: nx.Graph
    walkable_grid: WalkableGrid
    """보행 가능 영역 격자. 두 점 사이 이동 경로를 이 격자의 BFS로 계산해서
    오목한 폴리곤 형태에서도 폴리곤 밖으로 나가지 않고, 매대/푸드트럭 같은
    오브젝트가 있으면 자동으로 회피한다 (2026-07-24 도입)."""
    gates: list[dict] = field(default_factory=list)
    pois: list[dict] = field(default_factory=list)
    blocked_directions: set[tuple[int, int]] = field(default_factory=set)
    """2026-07-25 추가: 시나리오의 일방통행(one_way) 통로 정책으로 막힌 방향.
    (from_zone_id, to_zone_id) 쌍이 여기 있으면 그 방향으로는 이동하지 않는다."""

    # walkable_grid를 오브젝트 배치 반영해서 다시 만들 때 필요한 재료.
    # from_db_rows()에서 채워지고, apply_scenario_overrides()가 재사용한다.
    _walkable_area: object = field(default=None, repr=False)
    _base_obstacles: list[tuple[float, float, float]] = field(default_factory=list, repr=False)
    _preferred_lines: list[list[tuple[float, float]]] = field(default_factory=list, repr=False)

    def allowed_neighbors(self, zone_id: int) -> list[int]:
        """일방통행으로 막힌 방향을 제외한 인접 구역 목록."""
        if zone_id not in self.graph:
            return []
        return [
            n for n in self.graph.neighbors(zone_id)
            if (zone_id, n) not in self.blocked_directions
        ]

    @classmethod
    def from_db_rows(
        cls,
        market_row: dict,
        zone_rows: list[dict],
        adjacency_rows: list[dict],
        gate_rows: list[dict],
        stall_rows: list[dict] | None = None,
    ) -> "MarketLayout":
        stall_rows = stall_rows or []
        projection = LocalProjection(
            origin_lat=float(market_row["latitude"]),
            origin_lon=float(market_row["longitude"]),
        )

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
                "weight": float(row["weight"]) if row.get("weight") is not None else 1.0,
            }
            gates.append(gate)
            if nearest is not None:
                nearest.is_exit_zone = True

        pois: list[dict] = []
        for row in stall_rows:
            if row.get("latitude") is None or row.get("longitude") is None:
                continue
            sx, sy = projection.to_local(float(row["latitude"]), float(row["longitude"]))
            stall_point = Point(sx, sy)
            nearest_zone = min(
                zones.values(),
                key=lambda z: z.polygon_local.distance(stall_point),
                default=None,
            )
            if nearest_zone is not None:
                weight = float(row["weight"]) if row.get("weight") is not None else 1.0
                nearest_zone.attraction += weight
                pois.append({
                    "name": row.get("name", "Unknown"),
                    "zone_id": nearest_zone.zone_id,
                    "x": sx,
                    "y": sy,
                    "weight": weight
                })

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

        # 2026-08-XX 변경: "구역 폴리곤 전체 - 건물"로 걸어다닐 영역을 역산하던
        # 방식을 버리고, 시장 통로(mrkadjc01m.path_coordinates) 중심선에 실제
        # 통로 폭(path_width)만큼 버퍼를 씌운 모양을 그대로 걸어다닐 수 있는
        # 영역으로 쓴다. 건물 데이터 정확도에 안 휘둘리고, "정말 길인 곳만"
        # 걷게 되므로 더 안정적이다. 통로 데이터가 하나도 없으면(레이아웃이
        # 아직 안 갖춰진 시장 등) 구역 폴리곤 전체로 폴백한다.
        corridor_shapes: list[Polygon] = []
        for row in adjacency_rows:
            path_coordinates = row.get("path_coordinates")
            if not path_coordinates:
                continue
            try:
                line_wgs = parse_linestring(path_coordinates)
                line_local_points = [
                    projection.to_local(lat, lon) for lon, lat in line_wgs.coords
                ]
                width = float(row.get("path_width") or DEFAULT_CORRIDOR_WIDTH_M)
                corridor_shapes.append(
                    LineString(line_local_points).buffer(width / 2, cap_style="flat")
                )
            except Exception:
                continue

        if corridor_shapes:
            walkable_area = unary_union(corridor_shapes)
        else:
            walkable_area = unary_union([z.polygon_local for z in zones.values()])

        obstacles: list[tuple[float, float, float]] = []
        for row in stall_rows:
            if row.get("latitude") is None or row.get("longitude") is None:
                continue
            sx, sy = projection.to_local(float(row["latitude"]), float(row["longitude"]))
            radius = (
                float(row["footprint_radius_m"])
                if row.get("footprint_radius_m") is not None
                else DEFAULT_FACILITY_RADIUS_M
            )
            obstacles.append((sx, sy, radius))

        preferred_lines: list[list[tuple[float, float]]] = []
        for row in adjacency_rows:
            path_coordinates = row.get("path_coordinates")
            if not path_coordinates:
                continue
            try:
                line = parse_linestring(path_coordinates)
                preferred_lines.append(
                    [projection.to_local(lat, lon) for lon, lat in line.coords]
                )
            except Exception:
                continue

        walkable_grid = WalkableGrid.build(
            walkable_area=walkable_area,
            obstacles=obstacles,
            preferred_lines=preferred_lines,
        )

        layout = cls(
            market_id=market_row["market_id"],
            market_name=market_row["market_name"],
            projection=projection,
            zones=zones,
            graph=graph,
            walkable_grid=walkable_grid,
            gates=gates,
            pois=pois,
        )
        layout._walkable_area = walkable_area
        layout._base_obstacles = obstacles
        layout._preferred_lines = preferred_lines
        return layout


FOOD_TRUCK_RADIUS_M = 2.0     # 1톤트럭(포터/봉고) 전폭1.74m×전장5.2m 실측 근거
EVENT_ZONE_RADIUS_M = 1.7     # 표준 부스/몽골텐트 3x3m 실측 근거
REST_AREA_RADIUS_M = 1.3      # 파라솔+테이블+의자 실측 근거
OBSTACLE_BASE_RADIUS_M = 0.5  # 소형 적재물/표지판 기준

CONGESTION_ATTRACTION_DECAY = 3.0
"""2026-07-27 추가: 구역이 혼잡할수록(명/m^2) 매력도가 체감하는 정도를 조절하는
계수. attraction_of()에서 base_attraction / (1 + density * 이 값) 형태로 적용된다.
값이 클수록 조금만 붐벼도 매력도가 빨리 깎인다(임의 튜닝값). 이 값은 시장 구역
자체의 기본 매력도(DB의 zone.attraction)에만 쓰이며, PlacedObject와는 무관하다."""


def apply_scenario_overrides(
    layout: MarketLayout,
    objects: list[PlacedObject],
    corridor_policies: list[CorridorPolicy],
) -> None:
    """
    2026-08-02 변경: PlacedObject의 매력도(attraction) 부여 로직을 전면 제거했다.
    이제 food_truck/event_zone/rest_area/obstacle 네 타입 전부 동일하게
    "사람을 끌어당기지 않고, 그 자리를 물리적으로 못 지나가게만 만드는" 장애물로
    취급한다. 화재/대피 상황에서도 그냥 회피 대상 장애물로 작동한다.
    """
    extra_obstacles: list[tuple[float, float, float]] = []

    radius_by_type: dict[str, float] = {
        "food_truck": FOOD_TRUCK_RADIUS_M,
        "event_zone": EVENT_ZONE_RADIUS_M,
        "rest_area": REST_AREA_RADIUS_M,
        "obstacle": OBSTACLE_BASE_RADIUS_M,
    }

    for obj in objects:
        spec = layout.zones.get(obj.zoneId)
        if spec is None:
            continue

        if obj.latitude is not None and obj.longitude is not None:
            lx, ly = layout.projection.to_local(obj.latitude, obj.longitude)
        else:
            rp = spec.polygon_local.representative_point()
            lx, ly = rp.x, rp.y

        base_radius = radius_by_type.get(obj.objectType)
        if base_radius is None:
            continue

        radius = base_radius * (0.5 + obj.intensity)
        extra_obstacles.append((lx, ly, radius))

    if extra_obstacles:
        layout.walkable_grid = WalkableGrid.build(
            walkable_area=layout._walkable_area,
            obstacles=[*layout._base_obstacles, *extra_obstacles],
            preferred_lines=layout._preferred_lines,
        )

    for policy in corridor_policies:
        a, b = policy.fromZoneId, policy.toZoneId
        if a not in layout.zones or b not in layout.zones:
            continue

        if policy.action == "close":
            if layout.graph.has_edge(a, b):
                layout.graph.remove_edge(a, b)
            layout.blocked_directions.discard((a, b))
            layout.blocked_directions.discard((b, a))
        elif policy.action == "open":
            if not layout.graph.has_edge(a, b):
                layout.graph.add_edge(a, b, weight=1.0, path_width=1.0)
            layout.blocked_directions.discard((a, b))
            layout.blocked_directions.discard((b, a))
        elif policy.action == "one_way":
            if not layout.graph.has_edge(a, b):
                layout.graph.add_edge(a, b, weight=1.0, path_width=1.0)
            if policy.allowedDirection == "to_from":
                layout.blocked_directions.discard((b, a))
                layout.blocked_directions.add((a, b))
            else:
                layout.blocked_directions.discard((a, b))
                layout.blocked_directions.add((b, a))


def apply_gate_closures(layout: MarketLayout, closed_gate_ids: set[int]) -> None:
    if not closed_gate_ids:
        return

    remaining_gates = [
        g for g in layout.gates if g.get("facility_id") not in closed_gate_ids
    ]

    for spec in layout.zones.values():
        spec.is_exit_zone = False
    for gate in remaining_gates:
        zone_id = gate.get("zone_id")
        if zone_id is not None and zone_id in layout.zones:
            layout.zones[zone_id].is_exit_zone = True

    layout.gates = remaining_gates


FIRE_BASE_SCORE = 75.0
FIRE_INTENSITY_RANGE = 25.0
"""화재 이벤트가 구역에 강제로 부여하는 위험도 = 75 + 25*intensity (최대 100)."""

ACOUSTIC_BASE_RADIUS_M = 5.0
ACOUSTIC_INTENSITY_RADIUS_M = 15.0
"""음향 이상 이벤트의 강제 대피 반경 = 5 + 15*intensity (m)."""


def apply_event_triggers(model: MarketDigitalTwin, events: list[EventTrigger]) -> None:
    """
    2026-07-25 추가, 2026-08-XX 변경: 화재/음향 이상 이벤트를 이번 시뮬레이션
    실행에 반영한다.

    2026-08-XX 변경: 화재가 나면 발생 구역만이 아니라 시장 전체가 대피 대상이
    된다(model.forced_evacuation_zones에 전체 구역 등록) - "화재나면 일단
    모두 대피"가 자연스럽다는 판단. 강제 위험도 점수(색상 진하기)는 화재
    발생 구역에만 부여한다.

    model이 이미 만들어져 에이전트가 스폰된 뒤에 호출해야 한다(음향 이상은
    그 시점의 에이전트 좌표를 기준으로 반경 판정을 하기 때문). 좌표 정밀도는
    오브젝트 배치와 동일하게, 위경도가 있으면 그대로 쓰고 없으면 구역 대표점으로
    근사한다.
    """
    layout = model.layout
    for event in events:
        spec = layout.zones.get(event.zoneId)
        if spec is None:
            continue

        if event.latitude is not None and event.longitude is not None:
            ex, ey = layout.projection.to_local(event.latitude, event.longitude)
        else:
            rp = spec.polygon_local.representative_point()
            ex, ey = rp.x, rp.y

        if event.eventType == "fire":
            forced_score = FIRE_BASE_SCORE + FIRE_INTENSITY_RANGE * event.intensity
            model.set_forced_risk(event.zoneId, forced_score)
            model.forced_evacuation_zones.update(layout.zones.keys())
        elif event.eventType == "acoustic_anomaly":
            radius = ACOUSTIC_BASE_RADIUS_M + ACOUSTIC_INTENSITY_RADIUS_M * event.intensity
            model.apply_acoustic_burst(ex, ey, radius)


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

        # 2026-07-25 추가: 대피가 실제로 완료됐는지(게이트 통과해서 퇴장) 판정하는 데 사용.
        self.ever_evacuating: bool = False

        # 2026-07-27 추가: 시뮬레이션 도중 한 번이라도 대피 상태(EVACUATING)로
        # 전환된 에이전트 수 누적 카운터. 보고서에서 "위험 인원 N명" 서술에 쓴다.
        # 게이트가 닫혀 실제로 못 나갔어도(제자리에 멈춰있어도) 위험 판정 자체는
        # 발생했으므로 카운트에 포함한다.
        self.evacuated_count: int = 0

        # 2026-07-25 추가: 화재 이벤트로 실측 밀집도와 무관하게 강제로 끌어올린
        # 구역별 위험도. set_forced_risk()로 채워지고 evaluate_risk()가 반영한다.
        self.forced_risk: dict[int, float] = {}

        # 2026-08-XX 추가: 화재가 나면 개인별 risk_tolerance와 무관하게
        # 무조건 대피해야 하는 구역 집합. apply_event_triggers()가 채우고,
        # VisitorAgent.step()이 확인한다.
        self.forced_evacuation_zones: set[int] = set()


        self.pois: list[dict] = self.layout.pois

        # 2026-07-27 추가: evaluate_risk()에서 한 번 계산한 구역별 인원수를
        # attraction_of()가 재사용하기 위한 캐시(중복 계산 방지).
        self._zone_counts_cache: dict[int, int] = {}

        self._spawn_agents()

        self.evaluate_risk()


    def get_random_pois(self, count: int) -> list[dict]:
        if not self.pois:
            return []
        if count >= len(self.pois):
            return list(self.pois)
        weights = [poi.get("weight", 1.0) for poi in self.pois]
        return self._rng.choices(self.pois, weights=weights, k=count)

    def get_pois_near(self, x: float, y: float, radius: float) -> list[dict]:
        near = []
        for p in self.pois:
            dist = ((p["x"] - x)**2 + (p["y"] - y)**2)**0.5
            if dist <= radius:
                near.append(p)
        return near

    def count_agents_near(self, x: float, y: float, radius_m: float) -> int:
        count = 0
        for agent in self.agents:
            dist = ((agent.x - x)**2 + (agent.y - y)**2)**0.5
            if dist <= radius_m:
                count += 1
        return count

    def _compute_exit_hops(self) -> dict[int, int]:
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
        grid = self.layout.walkable_grid
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
                if not grid.is_walkable(grid.to_cell(x, y)):
                    x, y = self.random_point_in_zone(zone_id)
                VisitorAgent(self, zone_id=zone_id, x=x, y=y)

    def evaluate_risk(self) -> dict[int, RiskAssessment]:
        """현재 상태 기준으로 구역별 위험도를 재계산한다.

        2026-07-25 추가: forced_risk에 등록된 구역(화재 이벤트)은, 실측 밀집도
        기반 점수보다 강제 점수가 높으면 그 값으로 덮어쓴다.
        """
        counts = self.current_zone_counts()
        self._zone_counts_cache = counts
        self._risk = {}
        for zone_id, spec in self.layout.zones.items():
            assessment = assess_zone(
                zone_id=zone_id,
                visitor_count=counts.get(zone_id, 0),
                area_m2=spec.area_m2,
                path_width_m=spec.path_width_m,
            )
            forced = self.forced_risk.get(zone_id)
            if forced is not None and forced > assessment.score:
                assessment = RiskAssessment(
                    zone_id=zone_id,
                    score=forced,
                    level=score_to_level(forced),
                    density=assessment.density,
                    personal_space=assessment.personal_space,
                    density_score=assessment.density_score,
                    bottleneck_score=assessment.bottleneck_score,
                    reason="화재 이벤트로 강제 위험도 부여",
                )
            self._risk[zone_id] = assessment
        return self._risk

    def set_forced_risk(self, zone_id: int, score: float) -> None:
        """2026-07-25 추가: 화재 이벤트로 해당 구역의 위험도를 강제로 끌어올린다.

        실측 밀집도 기반 점수와 강제 점수 중 더 높은 쪽을 쓰므로, 이미 위험한
        구역이면 실측값을 그대로 유지한다. 등록 즉시 위험도를 재계산해서 바로
        반영되게 한다.
        """
        self.forced_risk[zone_id] = max(self.forced_risk.get(zone_id, 0.0), score)
        self.evaluate_risk()

    def apply_acoustic_burst(self, x: float, y: float, radius_m: float) -> None:
        """2026-07-25 추가: 음향 이상(비명/충돌음) 이벤트.

        화재와 달리 지속 효과가 아니라, 호출 시점에 발생 지점 반경 안에 있는
        방문객만 그 순간 한 번 강제로 대피 상태로 전환한다(밀집도 무관 즉발성).
        이후에는 다른 방문객과 동일하게 밀집도 기반 위험도 판단을 따른다.
        """
        for agent in list(self.agents):
            dx, dy = agent.x - x, agent.y - y
            if (dx * dx + dy * dy) ** 0.5 <= radius_m:
                if agent.state is not VisitorState.EVACUATING:
                    self.evacuated_count += 1
                agent.state = VisitorState.EVACUATING
                self.ever_evacuating = True

    def zone_risk_score(self, zone_id: int) -> float:
        assessment = self._risk.get(zone_id)
        return assessment.score if assessment else 0.0

    @property
    def risk(self) -> dict[int, RiskAssessment]:
        return self._risk

    def current_zone_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {zid: 0 for zid in self.layout.zones}
        for agent in self.agents:
            counts[agent.zone_id] = counts.get(agent.zone_id, 0) + 1
        return counts

    def next_zone_toward_exit(self, zone_id: int) -> int | None:
        current_hops = self._exit_hops.get(zone_id)
        if current_hops is None or current_hops == 0:
            return zone_id
        for neighbor in self.layout.allowed_neighbors(zone_id):
            if self._exit_hops.get(neighbor, 99) < current_hops:
                return neighbor
        return zone_id

    @property
    def movement_graph(self) -> nx.Graph:
        return self.layout.graph

    def neighbors_of(self, zone_id: int) -> list[int]:
        return self.layout.allowed_neighbors(zone_id)

    def attraction_of(self, zone_id: int) -> float:
        """
        해당 구역에 배치된 매대(오브젝트)들의 weight 합에, 현재 혼잡도에 따른
        체감 할인을 적용한 값.

        2026-07-27 추가: 이미 붐비는 구역은 매력도가 그대로여도 사람들이 덜
        끌리게(체감 매력 감소) 만든다 - "이미 사람이 많으면 오히려 덜 매력적으로
        느껴진다"는 실제 경향을 반영. 밀집도(명/m^2)가 높을수록
        1/(1+density*CONGESTION_ATTRACTION_DECAY) 배로 할인된다(임의 튜닝값).
        """
        spec = self.layout.zones.get(zone_id)
        if spec is None or spec.attraction <= 0:
            return 0.0
        count = self._zone_counts_cache.get(zone_id, 0)
        density = count / spec.area_m2 if spec.area_m2 > 0 else 0.0
        congestion_discount = 1.0 / (1.0 + density * CONGESTION_ATTRACTION_DECAY)
        return spec.attraction * congestion_discount

    def gate_in_zone(self, zone_id: int) -> dict | None:
        """
        2026-07-25 추가: 해당 구역에 있는(닫히지 않은) 게이트 하나를 반환한다.

        layout.gates는 apply_gate_closures()가 닫힌 게이트를 이미 제거한 상태이므로,
        여기서 찾아지는 게이트는 항상 "열려있는" 게이트다. 같은 구역에 게이트가
        여러 개면 첫 번째 것을 쓴다(정밀 선택은 추후 개선 가능).
        """
        for gate in self.layout.gates:
            if gate.get("zone_id") == zone_id:
                return gate
        return None

    def random_point_in_zone(self, zone_id: int) -> tuple[float, float]:
        spec = self.layout.zones.get(zone_id)
        if spec is None:
            return 0.0, 0.0
        point = (0.0, 0.0)
        for _ in range(5):
            pts = place_visitors(
                spec.polygon_local,
                1,
                strategy=self.placement_strategy,
                seed=self._rng.randint(0, 2**31 - 1),
            )
            if not pts:
                break
            point = pts[0]
            if self.layout.walkable_grid.is_walkable(self.layout.walkable_grid.to_cell(*point)):
                break
        return point

    def build_path(
        self, from_x: float, from_y: float, to_x: float, to_y: float, arrive_zone: int | None
    ) -> list[tuple[float, float, int | None]]:
        """
        2026-08-XX: 경로를 못 찾으면(막다른 곳 등) 목적지까지 직선으로 뚫고
        가지 않는다 - 이동 가능 영역이 이제 통로 모양 그대로라 더 자주 걸릴 수
        있어서, 실패 시 목적지 근처에서 갈 수 있는 가장 가까운 지점까지만
        경로를 잡는 폴백을 둔다(완전히 멈추지도, 벽을 뚫지도 않음).

        또한 shortest_path()는 목적지 셀이 막혀 있으면 내부적으로 조용히
        근처 셀로 스냅해서 성공 처리하는데, 그 경우에도 원래 요청 좌표가
        아니라 실제 도달한 지점을 마지막 웨이포인트로 쓴다.
        """
        grid = self.layout.walkable_grid
        start_cell = grid.to_cell(from_x, from_y)
        goal_cell = grid.to_cell(to_x, to_y)

        cell_path = grid.shortest_path(start_cell, goal_cell)
        if not cell_path or len(cell_path) < 2:
            nearest = grid._nearest_walkable(goal_cell, max_radius=15)
            if nearest is None or nearest == start_cell:
                return []
            fallback_path = grid.shortest_path(start_cell, nearest)
            if not fallback_path or len(fallback_path) < 2:
                return []
            waypoints = [grid.to_point(c) for c in fallback_path[1:]]
            return [(wx, wy, None) for wx, wy in waypoints]

        waypoints = [grid.to_point(c) for c in cell_path[1:-1]]
        path: list[tuple[float, float, int | None]] = [(wx, wy, None) for wx, wy in waypoints]

        last_reached_x, last_reached_y = grid.to_point(cell_path[-1])
        snap_distance = ((last_reached_x - to_x) ** 2 + (last_reached_y - to_y) ** 2) ** 0.5
        if snap_distance <= grid.cell_size_m:
            path.append((to_x, to_y, arrive_zone))
        else:
            path.append((last_reached_x, last_reached_y, arrive_zone))
        return path

    def nearest_open_gate(self, x: float, y: float) -> dict | None:
        """
        2026-08-XX 추가: 대피 시 "구역을 한 칸씩 거쳐서 출구 구역까지 간 다음
        그 구역의 게이트로" 가던 방식 대신, 지금 위치에서 직선거리 기준으로
        가장 가까운 열린 게이트를 바로 목적지로 잡는다. 실제 걷는 경로는
        build_path()가 통로망을 따라 알아서 찾아준다(직선거리는 "어느 게이트가
        가까운지" 후보를 고르는 용도일 뿐, 실제 이동 경로가 직선이라는 뜻은
        아니다). layout.gates는 apply_gate_closures()가 닫힌 게이트를 이미
        제거한 상태라 여기 있는 건 전부 열려있는 게이트다.
        """
        if not self.layout.gates:
            return None
        return min(
            self.layout.gates,
            key=lambda g: (g["x"] - x) ** 2 + (g["y"] - y) ** 2,
        )

    def inject_inflow(self, count: int) -> None:
        gates = [g for g in self.layout.gates if g.get("zone_id") is not None]
        if not gates or count <= 0:
            return

        weights = [max(g.get("weight") or 1.0, 0.0) for g in gates]
        total_weight = sum(weights) or 1.0
        for gate, w in zip(gates, weights):
            n = round(count * (w / total_weight))
            for _ in range(n):
                VisitorAgent(self, zone_id=gate["zone_id"], x=gate["x"], y=gate["y"])

    def step(self) -> None:
        if self.mode is SimulationMode.SCENARIO:
            self.agents.shuffle_do("step")
        self.evaluate_risk()

    def snapshot(self) -> dict:
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