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
from shapely.geometry import Point, Polygon
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



SHOP_DATA = {
  "A_west": [
    "망원수제고로케", "고손수제군만두", "알뜰살림센타", "큐스 2호점",
    "진양수산", "망원시장 못난이만두", "아침엔바게트", "망원유통(13번과자가게)", "수리상점 곰손",
    "올포유(15번)", "싱싱나라 야채", "무침프로젝트",
    "범골커피(2F)", "고향집", "BYC 망원상회",
    "강정선생", "민영활어공장 망원시장점", "티각태각(1F)", "송이네", "모든발"
  ],
  "A_east": [
    "무궁화어묵 망원점", "프레쉬트레일 망원지점", "엄마손마트", "트라이",
    "장수한방족발", "원당수제고로케(1층 3호)", "목포홍어무침각종전", "하나로축산",
    "대게특별시 망원시장점(24호)", "마포축산", "이삭토스트(1F)", "전통맛죽",
    "369활어회", "연만두 본점"
  ],
  "B_west": [
    "훈이네빈대떡", "깜놀이네야채과일", "성미건어물(37번)", "우이락",
    "엄마손왕두부", "부산대원어묵", "장충동한방족발", "홍보사", "엄마손반찬", "맛있는집",
    "새나래수산", "마당쇠", "옹기마을", "참맛닭곰탕"
  ],
  "B_east": [
    "진영농산물직판장", "망원닭강정", "털보네야채", "솔나무떡집",
    "큐스닭강정", "오공찬(1F)", "망원튀맥집", "해피네바삭치킨", "홍두깨손칼국수",
    "오지개", "뷰티크레딧", "고려왕족발", "틈새전", "망원떡갈비", "훈훈호떡",
    "수경아채", "남도건어물"
  ],
  "C_west": [
    "부자상회", "전국일 김치삼겹", "바다를 사랑하는 형제들", "포트캔커피 망원점", "철길떡볶이 망원시장점(101호)"
  ],
  "C_east": [
    "대박수산", "형제건어물", "석규네수제한과(74호)", "교동왕족발",
    "서울축산", "서민구판장", "망원축산", "바삭마차", "이포인트", "풍년기름", "와이레스 망원점"
  ]
}

class SimulationMode(str, Enum):
    MIRROR = "mirror"
    SCENARIO = "scenario"


DEFAULT_FACILITY_RADIUS_M = 1.2
"""시설(매대 등)의 물리적 점유 반경 기본값(m). 실제 크기 데이터
(mrkfcts01m.footprint_radius_m)가 없을 때 쓰는 임시 근사치."""


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
        )
        layout._walkable_area = walkable_area
        layout._base_obstacles = obstacles
        layout._preferred_lines = preferred_lines
        return layout


FOOD_TRUCK_ATTRACTION = 6.0
EVENT_ZONE_ATTRACTION = 9.0
REST_AREA_ATTRACTION = 2.5
OBSTACLE_BASE_RADIUS_M = 1.5

CONGESTION_ATTRACTION_DECAY = 3.0
"""2026-07-27 추가: 구역이 혼잡할수록(명/m^2) 매력도가 체감하는 정도를 조절하는
계수. attraction_of()에서 base_attraction / (1 + density * 이 값) 형태로 적용된다.
값이 클수록 조금만 붐벼도 매력도가 빨리 깎인다(임의 튜닝값)."""


def _saturating_attraction(base_weight: float, intensity: float) -> float:
    """
    2026-07-27 변경: 오브젝트 강도(intensity)에 따른 매력도 증가를 선형이 아니라
    포화 곡선(제곱근)으로 바꿨다. 이전엔 intensity가 늘어난 만큼 매력도가 그대로
    비례해서 늘었는데, 실제로는 "이미 충분히 눈에 띄는 오브젝트"가 강도를 더
    올린다고 그만큼 더 매력적이지는 않다는 점을 반영한 것 - 강도 0.5에서 1.0으로
    올려도 효과 증가폭이 처음보다 완만해진다(임의 튜닝, sqrt 곡선).
    """
    return base_weight * math.sqrt(max(intensity, 0.0))


def apply_scenario_overrides(
    layout: MarketLayout,
    objects: list[PlacedObject],
    corridor_policies: list[CorridorPolicy],
) -> None:
    extra_obstacles: list[tuple[float, float, float]] = []

    for obj in objects:
        spec = layout.zones.get(obj.zoneId)
        if spec is None:
            continue

        if obj.latitude is not None and obj.longitude is not None:
            lx, ly = layout.projection.to_local(obj.latitude, obj.longitude)
        else:
            rp = spec.polygon_local.representative_point()
            lx, ly = rp.x, rp.y

        if obj.objectType == "food_truck":
            spec.attraction += _saturating_attraction(FOOD_TRUCK_ATTRACTION, obj.intensity)
        elif obj.objectType == "event_zone":
            spec.attraction += _saturating_attraction(EVENT_ZONE_ATTRACTION, obj.intensity)
        elif obj.objectType == "rest_area":
            spec.attraction += _saturating_attraction(REST_AREA_ATTRACTION, obj.intensity)
        elif obj.objectType == "obstacle":
            radius = OBSTACLE_BASE_RADIUS_M * (0.5 + obj.intensity)
            extra_obstacles.append((lx, ly, radius))
            spec.attraction = max(0.0, spec.attraction - EVENT_ZONE_ATTRACTION * obj.intensity * 0.3)

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
    2026-07-25 추가: 화재/음향 이상 이벤트를 이번 시뮬레이션 실행에 반영한다.

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


        self.pois: list[dict] = []
        self._init_pois()

        # 2026-07-27 추가: evaluate_risk()에서 한 번 계산한 구역별 인원수를
        # attraction_of()가 재사용하기 위한 캐시(중복 계산 방지).
        self._zone_counts_cache: dict[int, int] = {}

        self._spawn_agents()

        self.evaluate_risk()


    def _init_pois(self) -> None:
        from shapely.geometry import box, LineString
        W2_LAT, W2_LON = 37.55654668407929, 126.9060619136959
        W1_LAT, W1_LON = 37.555882365686884, 126.90627211946713

        w2_x, w2_y = self.layout.projection.to_local(W2_LAT, W2_LON)
        w1_x, w1_y = self.layout.projection.to_local(W1_LAT, W1_LON)
        
        exc_box1 = box(w2_x - 14, w2_y - 6, w2_x + 14, w2_y + 6)
        exc_box2 = box(w1_x - 14, w1_y - 6, w1_x + 14, w1_y + 6)

        zone_groups = {"A": [], "B": [], "C": []}
        
        for zone_id, spec in self.layout.zones.items():
            cx, cy = spec.polygon_local.centroid.x, spec.polygon_local.centroid.y
            lat, lon = self.layout.projection.to_latlon(cx, cy)
            
            if lat > W2_LAT:
                zone_groups["A"].append(spec.polygon_local)
            elif lat > W1_LAT:
                zone_groups["B"].append(spec.polygon_local)
            else:
                zone_groups["C"].append(spec.polygon_local)
                
        for group_name, poly_list in zone_groups.items():
            if not poly_list:
                continue
                
            group_poly = unary_union(poly_list)
            boundary = group_poly.exterior
            minx, miny, maxx, maxy = group_poly.bounds
            
            west_shops = SHOP_DATA.get(f"{group_name}_west", [])
            east_shops = SHOP_DATA.get(f"{group_name}_east", [])
            
            def place_shops(shops: list[str], side: str):
                if not shops:
                    return
                
                num_points = max(200, len(shops) * 3)
                step = (maxy - 2 - (miny + 2)) / max(1, num_points - 1)
                ys = [miny + 2 + step * i for i in range(num_points)]
                
                valid_points = []
                for y_val in ys:
                    horiz_line = LineString([(minx - 10, y_val), (maxx + 10, y_val)])
                    inter = boundary.intersection(horiz_line)
                    
                    if inter.is_empty:
                        continue
                        
                    pts = []
                    if inter.geom_type == 'Point':
                        pts = [inter]
                    elif inter.geom_type == 'MultiPoint':
                        pts = list(inter.geoms)
                    
                    if not pts:
                        continue
                        
                    if side == 'west':
                        target_pt = min(pts, key=lambda p: p.x)
                        offset = 1.0
                    else:
                        target_pt = max(pts, key=lambda p: p.x)
                        offset = -1.0
                        
                    final_pt = Point(target_pt.x + offset, target_pt.y)
                    
                    if exc_box1.contains(final_pt) or exc_box2.contains(final_pt):
                        continue
                        
                    valid_points.append(final_pt)
                
                if not valid_points:
                    target_zones = list(self.layout.zones.keys())
                    for s_name in shops:
                        z_id = self._rng.choice(target_zones)
                        x, y = self.random_point_in_zone(z_id)
                        self.pois.append({"name": s_name, "zone_id": z_id, "x": x, "y": y, "weight": 1.0})
                    return
                    
                step_idx = (len(valid_points) - 1) / max(1, len(shops) - 1)
                indices = [int(round(step_idx * i)) for i in range(len(shops))]
                
                for idx, s_name in zip(indices, shops):
                    pt = valid_points[idx]
                    assigned_zone = None
                    for z_id, spec in self.layout.zones.items():
                        if spec.polygon_local.buffer(2.0).contains(pt):
                            assigned_zone = z_id
                            break
                    if assigned_zone is None:
                        assigned_zone = list(self.layout.zones.keys())[0]
                        
                    base_weight = 1.0
                    spec = self.layout.zones.get(assigned_zone)
                    if spec and spec.attraction > 0:
                        base_weight += spec.attraction * 0.1
                        
                    self.pois.append({"name": s_name, "zone_id": assigned_zone, "x": pt.x, "y": pt.y, "weight": base_weight})

            place_shops(west_shops, 'west')
            place_shops(east_shops, 'east')

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
        grid = self.layout.walkable_grid
        cell_path = grid.shortest_path(grid.to_cell(from_x, from_y), grid.to_cell(to_x, to_y))
        if not cell_path or len(cell_path) < 2:
            return [(to_x, to_y, arrive_zone)]

        waypoints = [grid.to_point(c) for c in cell_path[1:-1]]
        path: list[tuple[float, float, int | None]] = [(wx, wy, None) for wx, wy in waypoints]
        path.append((to_x, to_y, arrive_zone))
        return path

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