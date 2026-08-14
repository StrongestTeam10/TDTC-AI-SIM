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


DEFAULT_FACILITY_RADIUS_M = 0.6
"""시설(매대 등)의 물리적 점유 반경 기본값(m). 실제 크기 데이터
(mrkfcts01m.footprint_radius_m)가 없을 때 쓰는 임시 근사치.

2026-08-XX 변경(1.2 -> 0.6): 실측 시장이 68m x 240m로 좁고 긴 형태인데,
매대 81개를 각각 반경 1.2m 원형 장애물로 뚫으니 폭이 좁은 통로 연결부가
전부 끊겨서 걸을 수 있는 영역이 20개의 고립된 섬으로 쪼개졌다(연결성
분석 결과: 걸을 수 있는 칸의 30%만 최대 섬에 속하고 POI 81개 중 23개,
게이트 6개 중 2개만 도달 가능). 그 결과 에이전트가 목적지로 가는 경로를
91%나 못 찾아(build_path 실패) 대부분 제자리에 멈춰 있었다. 반경을 0.6m로
줄이면 걸을 수 있는 영역이 하나로 온전히 연결되고 모든 POI/게이트에
도달 가능해진다(1m 격자 해상도에서 좁은 통로를 살리기 위한 실측 튜닝값).
매대는 여전히 장애물로서 중심 0.6m는 못 지나간다."""

MAX_STALL_OBSTACLE_RADIUS_M = 0.6
"""매대 장애물이 걸을 수 있는 격자에서 차지하는 반경의 상한(m). DB에
footprint_radius_m가 크게 들어와 있어도, 좁은 통로가 다시 끊기지 않도록
격자용 반경은 이 값으로 제한한다(위 DEFAULT_FACILITY_RADIUS_M 설명 참고).
'실제 매대 크기'와 '보행 격자에서의 점유 반경'을 분리한 값이다."""

DEFAULT_CORRIDOR_WIDTH_M = 4.0
"""2026-08-XX 추가: 통로 폭(mrkadjc01m.path_width)이 비어있을 때 쓰는 기본값(m).
전통시장 골목 실측 폭 참고. 2026-08-XX 재복귀 이후로는 걸을 수 있는 영역 계산이
아니라 close 정책 판정과 preferred_lines 가중치에만 쓰인다."""


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

    _base_walkable_grid: object = field(default=None, repr=False)
    """2026-08-XX 추가: 배치 오브젝트/통로정책이 반영되기 "전"의 기본 격자
    (건물만 뺀 자연 상태). 화재 인지(경보) 전파는 이 격자로 계산한다 - 알람/
    연기/소문 같은 '화재를 알게 되는 것'은 푸드트럭이나 닫힌 통로에 막히지
    않아야 현실적이기 때문. 오브젝트/폐쇄는 대피 '이동'(walkable_grid)만
    방해한다. (이걸 안 하면 오브젝트가 인지 전파까지 막아 대피 인원이
    비현실적으로 줄어 '개입할수록 안전해 보이는' 착시가 생겼다.)"""

    # walkable_grid를 오브젝트 배치 반영해서 다시 만들 때 필요한 재료.
    # from_db_rows()에서 채워지고, apply_scenario_overrides()가 재사용한다.
    _walkable_area: object = field(default=None, repr=False)
    _base_obstacles: list[tuple[float, float, float]] = field(default_factory=list, repr=False)
    _preferred_lines: list[list[tuple[float, float]]] = field(default_factory=list, repr=False)
    _corridor_polys_by_edge: dict[frozenset[int], list[object]] = field(
        default_factory=dict, repr=False
    )
    """2026-08-XX 추가: 구역 쌍(from,to 방향 무관)별로 그 통로의 버퍼 폴리곤을
    모아둔다. apply_scenario_overrides()가 통로정책 close를 실제 걸을 수 있는
    영역에서 빼는 데 쓴다 - 이전에는 close가 layout.graph(논리 그래프)만 바꾸고
    실제 WalkableGrid(에이전트가 걷는 격자)는 전혀 안 바뀌어서, "닫은 통로"를
    에이전트가 그냥 걸어서 지나갈 수 있는 문제가 있었다."""

    @classmethod
    def from_db_rows(
        cls,
        market_row: dict,
        zone_rows: list[dict],
        adjacency_rows: list[dict],
        gate_rows: list[dict],
        stall_rows: list[dict] | None = None,
        building_rows: list[dict] | None = None,
    ) -> "MarketLayout":
        stall_rows = stall_rows or []
        building_rows = building_rows or []
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

        # 2026-08-XX 재복귀: 통로 중심선(mrkadjc01m.path_coordinates) 버퍼를
        # 걸을 수 있는 영역의 "기준"으로 삼는 방식(2026-08-XX 도입)을 완전히
        # 되돌린다. 그 방식은 통로 데이터 품질에 그대로 휘둘렸다 - 중앙구역처럼
        # 통로 조각이 74m 끊겨있거나, 통로 폭이 구역 폴리곤을 다 못 덮는 경우가
        # 실제 데이터에 있어서, 끊긴 통로 잇기(다리)/건물 우회/버퍼 오버플로우
        # 방지/cap_style 조정 같은 방어 코드를 계속 쌓아야 했다. 원래(이 변경
        # 이전) 썼던 "구역 폴리곤 합집합 - 건물" 방식이 실제로는 문제없이
        # 깔끔하게 동작했었다는 걸 재확인해서(사용자 확인), 다시 이 방식을
        # 기준으로 삼는다. 구역 폴리곤은 "북측/중앙/남측 구역"처럼 그 구역에
        # 속한 통로·매대 공간을 이미 포함하는 넓은 지역 단위라서, 통로 데이터가
        # 부실해도 걸을 수 있는 영역 자체는 안정적으로 나온다. WalkableGrid
        # (격자+다익스트라)가 오목한 폴리곤 형태도 알아서 처리해준다.
        #
        # 통로 중심선(mrkadjc01m)은 버리지 않고 preferred_lines로 계속 쓴다 -
        # "그 근처를 더 선호해서 걷는" 이동 비용 가중치 용도로만 남긴다
        # (gridspace.py PREFERRED_COST 참고). 통로정책 close가 특정 구간을
        # 실제로 막을 수 있도록, 구역쌍별 통로 버퍼 폴리곤도 계속 계산해서
        # corridor_polys_by_edge에 보관한다(apply_scenario_overrides가 사용).
        walkable_area = unary_union([z.polygon_local for z in zones.values()])

        building_polys: list[Polygon] = []
        for row in building_rows:
            poly_wgs = row.get("polygon_coordinates")
            if not poly_wgs:
                continue
            try:
                building_polys.append(projection.polygon_to_local(parse_polygon(poly_wgs)))
            except Exception:
                continue
        if building_polys:
            try:
                walkable_area = walkable_area.difference(unary_union(building_polys))
            except Exception:
                pass

        corridor_polys_by_edge: dict[frozenset[int], list[Polygon]] = {}
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
                poly = LineString(line_local_points).buffer(width / 2)
                edge_key = frozenset((row["from_zone_id"], row["to_zone_id"]))
                corridor_polys_by_edge.setdefault(edge_key, []).append(poly)
            except Exception:
                continue

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
            # 좁은 통로가 매대 장애물로 끊기지 않도록 격자용 반경은 상한을 둔다.
            radius = min(radius, MAX_STALL_OBSTACLE_RADIUS_M)
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
        layout._base_walkable_grid = walkable_grid  # 오브젝트/폐쇄 반영 전 격자(화재 인지 전파용)
        layout._walkable_area = walkable_area
        layout._base_obstacles = obstacles
        layout._preferred_lines = preferred_lines
        layout._corridor_polys_by_edge = corridor_polys_by_edge
        return layout


FOOD_TRUCK_RADIUS_M = 2.0     # 1톤트럭(포터/봉고) 전폭1.74m×전장5.2m 실측 근거
OBSTACLE_BASE_RADIUS_M = 0.5  # 소형 적재물/표지판 기준

# 2026-08-XX 변경: 오브젝트를 두 부류로 나눈다.
#  - 장애물류(food_truck, obstacle): 반경만큼 물리적으로 못 지나감(기존 유지).
#  - 유인류(event_zone, rest_area): 장애물이 아니라 "사람을 끌어모으는 곳".
#    그 위치에 가중치 높은 POI를 심어 에이전트가 자주 목적지로 잡고 모여서
#    체류 -> 국소 밀집(군집)이 생긴다. 가중치는 과하지 않게(사용자 요청).
OBSTACLE_OBJECT_TYPES = {"food_truck", "obstacle"}
ATTRACTOR_OBJECT_TYPES = {"event_zone", "rest_area"}

MAX_PLACED_OBSTACLE_RADIUS_M = 1.0
"""2026-08-XX 추가: 배치 오브젝트(푸드트럭/적재물)가 격자에서 차지하는 반경의
상한(m). 푸드트럭 실측 반경(2.4m 안팎)을 그대로 쓰면 좁은 시장 통로를 통째로
막아 걸을 수 있는 영역이 조각나고(=화재 인지 전파·군집·이동이 다 약해짐),
매대 반경 문제(MAX_STALL_OBSTACLE_RADIUS_M)와 같은 증상이 생긴다. 여전히
'못 지나가는 장애물'이되 통로를 완전히는 끊지 않도록 이 값으로 제한한다."""
# 매대가 수십 개라 일반 매대(가중치 ~1.0) 대비 몇 배로는 군집이 안 생긴다.
# 눈에 보이는 군집이 생기되 "전원이 몰리지는 않을" 정도로 잡은 값(임의 튜닝).
EVENT_ZONE_POI_WEIGHT = 12.0  # 행사장 - 뚜렷한 큰 군집
REST_AREA_POI_WEIGHT = 6.0    # 휴게공간 - 중간 군집

CLOSE_BOUNDARY_WALL_WIDTH_M = 3.0
"""2026-08-XX 추가: 통로정책 close가 두 구역 경계선을 막을 때 쓰는 벽 두께(m).
격자 셀 크기(1m)보다 충분히 두꺼워야 대각선 이동으로 살짝 빠져나가는 것도
확실히 막힌다."""

CLOSE_BOUNDARY_WALL_WIDTH_M = 3.0
"""2026-08-XX 추가: 통로정책 close가 두 구역 경계선을 막을 때 쓰는 벽 두께(m).
격자 셀 크기(1m)보다 충분히 두꺼워야 대각선 이동으로 살짝 빠져나가는 것도
확실히 막힌다."""


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

    2026-08-XX 재작성: 통로정책(close/open/one_way)이 layout.graph(구역 간
    "몇 홉이나 걸리는지" 계산에만 쓰이는 논리 그래프)만 바꾸고, 에이전트가
    실제로 걸어다니는 WalkableGrid(격자)는 전혀 안 바뀌는 문제가 있었다.
    그 결과 통로를 "닫아도" 에이전트는 실제로는 그 통로를 그냥 걸어서 지나갈
    수 있었다(피드백으로 확인됨). close는 이제 실제로 그 통로의 버퍼 폴리곤을
    걸을 수 있는 영역에서 빼서 물리적으로도 막는다.

    2026-08-XX 추가: one_way(방향 제한)도 WalkableGrid에 zone_of/
    blocked_zone_edges를 넘겨서 실제 셀 단위 이동에 반영한다 - 구역 A에서
    구역 B로 넘어가는 방향이 막혀 있으면, 그 경계를 넘는 셀 이동 자체를
    차단한다(gridspace.py neighbors8() 참고). B->A 방향은 그대로 열려 있다.

    open은 여전히 논리 그래프만 바꾼다 - DB에 없던 통로를 "새로 뚫는" 것이라
    실제 좌표(어느 경로로 뚫렸는지)가 없어 격자에 반영할 도형 자체가 없기
    때문이다. 다만 두 구역의 폴리곤이 이미 물리적으로 맞닿아 있다면(대부분의
    실제 케이스) close로 벽을 세운 적이 없는 한 원래부터 걸어서 오갈 수
    있으므로, open은 이런 경우 사실상 이미 걸어다닐 수 있는 경로를 논리
    그래프에도 "공식적으로 있다"고 기록하는 것뿐이다. 두 구역이 실제로
    맞닿아 있지 않은데 open으로 완전히 새로운 통로를 만들고 싶다면, 그
    통로의 실제 좌표/폭 데이터가 별도로 필요하다(현재 스키마에는 없음).
    """
    extra_obstacles: list[tuple[float, float, float]] = []

    obstacle_radius_by_type: dict[str, float] = {
        "food_truck": FOOD_TRUCK_RADIUS_M,
        "obstacle": OBSTACLE_BASE_RADIUS_M,
    }
    attractor_weight_by_type: dict[str, float] = {
        "event_zone": EVENT_ZONE_POI_WEIGHT,
        "rest_area": REST_AREA_POI_WEIGHT,
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

        if obj.objectType in obstacle_radius_by_type:
            # 장애물류: 반경만큼 물리적으로 막되, 좁은 통로를 통째로 끊지 않도록
            # 상한을 둔다(그래야 화재 인지·군집·이동이 안정적으로 유지된다).
            radius = obstacle_radius_by_type[obj.objectType] * (0.5 + obj.intensity)
            radius = min(radius, MAX_PLACED_OBSTACLE_RADIUS_M)
            extra_obstacles.append((lx, ly, radius))
        elif obj.objectType in attractor_weight_by_type:
            # 유인류: 그 자리에 가중치 높은 POI를 심어 사람을 끌어모은다(밀집).
            # intensity가 높을수록 조금 더 강하게 끌리되, 과하지 않게 상한을 둔다.
            weight = attractor_weight_by_type[obj.objectType] * (0.75 + 0.5 * obj.intensity)
            layout.pois.append({
                "name": {"event_zone": "행사장", "rest_area": "휴게공간"}[obj.objectType],
                "zone_id": obj.zoneId,
                "x": lx,
                "y": ly,
                "weight": weight,
                "kind": obj.objectType,  # 체류 시간 보정 등에 사용
            })

    closed_edge_keys: set[frozenset[int]] = set()
    for policy in corridor_policies:
        a, b = policy.fromZoneId, policy.toZoneId
        if a not in layout.zones or b not in layout.zones:
            continue

        if policy.action == "close":
            if layout.graph.has_edge(a, b):
                layout.graph.remove_edge(a, b)
            layout.blocked_directions.discard((a, b))
            layout.blocked_directions.discard((b, a))
            closed_edge_keys.add(frozenset((a, b)))
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

    base_area = layout._walkable_area
    if closed_edge_keys:
        # 2026-08-XX 변경: 걸을 수 있는 영역 계산이 다시 "구역 폴리곤 - 건물"
        # 기준으로 바뀌면서, 예전처럼 좁은 통로 버퍼 폴리곤 하나만 빼는 걸로는
        # close가 더 이상 통하지 않는다 - 구역 자체가 넓은 면적이라 그 좁은
        # 조각을 빼도 나머지 넓은 공간으로 그냥 돌아갈 수 있기 때문이다(실제
        # 테스트로 확인됨: 통로 폐쇄해도 경로가 그대로 있었음). 이제 두 구역이
        # 맞닿는 경계선 전체를 CLOSE_BOUNDARY_WALL_WIDTH_M만큼 두껍게 "벽"으로
        # 만들어서 실제로 그 구역 간 이동 자체를 막는다. 맞닿는 경계가 없는
        # 구역쌍(폴리곤이 안 붙어있음)은 기존처럼 통로 버퍼만이라도 뺀다.
        polys_to_remove: list[Polygon] = []
        for key in closed_edge_keys:
            zone_ids = list(key)
            a = zone_ids[0]
            b = zone_ids[1] if len(zone_ids) > 1 else zone_ids[0]
            zone_a = layout.zones.get(a)
            zone_b = layout.zones.get(b)
            wall_added = False
            if zone_a is not None and zone_b is not None:
                try:
                    shared_boundary = zone_a.polygon_local.boundary.intersection(
                        zone_b.polygon_local.boundary
                    )
                    if not shared_boundary.is_empty:
                        polys_to_remove.append(
                            shared_boundary.buffer(CLOSE_BOUNDARY_WALL_WIDTH_M)
                        )
                        wall_added = True
                except Exception:
                    pass
            if not wall_added:
                polys_to_remove.extend(layout._corridor_polys_by_edge.get(key, []))
        if polys_to_remove:
            try:
                base_area = base_area.difference(unary_union(polys_to_remove))
            except Exception:
                pass

    if extra_obstacles or closed_edge_keys or layout.blocked_directions:
        layout.walkable_grid = WalkableGrid.build(
            walkable_area=base_area,
            obstacles=[*layout._base_obstacles, *extra_obstacles],
            preferred_lines=layout._preferred_lines,
            zones={zid: spec.polygon_local for zid, spec in layout.zones.items()}
            if layout.blocked_directions
            else None,
            blocked_zone_edges=layout.blocked_directions or None,
        )


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
"""화재 발생 구역에 부여하는 위험도 = 75 + 25*intensity (최대 100)."""

FIRE_SPREAD_DECAY = 0.5
"""인접 구역으로 한 홉씩 갈수록 위험도가 이전 값의 이 비율만큼만 남는다."""

FIRE_SPREAD_MAX_HOPS = 3
"""화재 위험도를 전파할 최대 홉 수(구역 인접 그래프 기준). 그보다 먼 구역은
전혀 영향을 안 받는다."""

FIRE_SPREAD_MIN_SCORE = 5.0
"""감쇠 결과가 이 값보다 작으면 의미 없는 수치이므로 부여하지 않는다."""

# 2026-08-XX 추가: 화재 "인지(awareness)" 전파. 화재가 나는 순간 전원이 동시에
# 아는 게 아니라, 화재 지점에서 통로를 따라 인지가 바깥으로 퍼진다. 각 에이전트는
# 그 인지 전선이 자기 위치(통로 거리 기준)에 도달해야 화재를 알고 대피를 시작한다.
AWARENESS_SPEED_M_PER_STEP = 10.0
"""인지 전선이 통로를 따라 1스텝에 퍼지는 거리(m). 화재 지점 기준 이 속도로
바깥으로 확산한다(1스텝=10초 기준 사람 걸음보다 빠른, 알람/연기/소문 속도)."""

AWARENESS_INITIAL_RADIUS_M = 8.0
"""화재 발생 즉시 인지하는 초기 반경(m). 화재 바로 근처 사람들은 나자마자 안다."""

AWARE_MIN_DANGER = 35.0
"""화재를 인지한 에이전트의 최소 위험도 표시값. 화재에서 멀어 구역 위험도가
낮아도, 인지해서 대피 중이면 최소 '노랑'으로 보이게 한다(FE 색 구분용)."""

INFLOW_RESUME_GRACE_STEPS = 3
"""화재 진압(연소 종료) 후 이 스텝 수만큼 지나면 유동인구 유입을 재개한다.
전원 대피 완료나 완전 복구를 기다리지 않고, 진압 뒤 자연스럽게 새 방문객이
들어오기 시작한다(사용자 요청). 나가는 사람과 들어오는 사람이 겹치게 된다."""


def apply_event_triggers(model: MarketDigitalTwin, events: list[EventTrigger]) -> None:
    """
    2026-07-25 추가, 2026-08-XX 변경: 화재 이벤트를 이번 시뮬레이션 실행에
    반영한다. 이제는 즉시 위험도를 세팅하는 게 아니라, 모델에 "활성 화재"로
    등록만 하고, 실제 구역별 위험도는 매 스텝 model._update_fires()가 화재
    생애주기(발화->연소->진압->복구)에 따라 계산한다.

    화재 생애주기(각 화재의 발화 시점부터의 경과 스텝 age 기준):
      - 연소(age <= burnSteps): 발화 구역 75~100점, 인접 구역은 홉당 절반씩
        감쇠(FIRE_SPREAD_DECAY). 이 값이 그대로 유지된다.
      - 복구(burnSteps < age <= burnSteps+recoverySteps): 위 위험도가 선형으로
        0까지 감쇠한다(진압되어 서서히 안전해짐).
      - 복구 완료(age > burnSteps+recoverySteps): 화재 제거, 위험도 0으로
        돌아가고 정상 상태(+유동인구 재유입) 재개.
    """
    for event in events:
        if event.eventType != "fire":
            continue
        if event.zoneId not in model.layout.zones:
            continue
        model.register_fire(event)


class MarketDigitalTwin(Model):
    """시장 디지털 트윈 모델."""

    def __init__(
        self,
        layout: MarketLayout,
        observations: dict[int, ZoneObservation],
        mode: SimulationMode = SimulationMode.MIRROR,
        placement_strategy: PlacementStrategy = PlacementStrategy.CENTERLINE,
        seed: int | None = None,
        observed_positions: dict[int, list[tuple[float, float]]] | None = None,
    ) -> None:
        super().__init__(seed=seed)
        self.layout = layout
        self.observations = observations
        self.mode = mode
        self.placement_strategy = placement_strategy
        self._rng = random.Random(seed)

        # 2026-08-12 추가(CCTV 관측 초기배치): 구역별 관측 위치(로컬 x,y). 있으면 그 자리에
        # 먼저 배치하고, 유입 인원(observations.visitor_count)을 그 위에 추가로 채운다.
        # 없으면(기존 동작) 유입만으로 배치.
        self.observed_positions = observed_positions or {}

        self._risk: dict[int, RiskAssessment] = {}

        # 2026-08-XX 추가: nearest_reachable_gate_path()가 쓰는 게이트 도달성
        # 사전 계산 캐시. 첫 호출 때 지연 계산되고(_build_gate_tree), 이후엔
        # 재사용된다 - 게이트 구성은 모델 생명주기 동안 안 바뀌므로 안전하다.
        self._gate_tree: tuple[dict, dict, dict] | None = None
        self._gate_tree_built: bool = False

        # 2026-07-25 추가: 대피가 실제로 완료됐는지(게이트 통과해서 퇴장) 판정하는 데 사용.
        self.ever_evacuating: bool = False

        # 2026-07-27 추가: 시뮬레이션 도중 한 번이라도 대피 상태(EVACUATING)로
        # 전환된 에이전트 수 누적 카운터. 보고서에서 "위험 인원 N명" 서술에 쓴다.
        # 게이트가 닫혀 실제로 못 나갔어도(제자리에 멈춰있어도) 위험 판정 자체는
        # 발생했으므로 카운트에 포함한다.
        self.evacuated_count: int = 0

        # 2026-07-25 추가: 화재 이벤트로 실측 밀집도와 무관하게 강제로 끌어올린
        # 구역별 위험도. _update_fires()가 매 스텝 다시 계산해 채운다.
        self.forced_risk: dict[int, float] = {}

        # 2026-08-XX 추가: 화재 생애주기 관리.
        self.current_step: int = 0
        self.active_fires: list[dict] = []  # {zone_id, intensity, ignite_step, burn_steps, recovery_steps}

        # 2026-08-XX 추가: 평상시(화재 없음) 유동인구 유지용 목표 인원.
        # None이면 유입을 하지 않는다(예: MIRROR 모드). API가 시나리오/예측
        # 실행 시 agentCount(또는 totalInflow)로 설정한다.
        self.target_population: int | None = None
        self.population_tolerance: int = 5

        self.pois: list[dict] = self.layout.pois

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

    def _nearest_reachable_cell(self, cell, reachable, max_radius: int = 60):
        """cell이 게이트 도달 가능 집합(reachable)에 없으면, 주변에서 가장 가까운
        도달 가능 칸을 찾아 반환한다. 없으면 None.

        2026-08-XX 추가: 장애물이 좁은 통로를 막아 생긴 '고립 포켓'에 에이전트가
        스폰되면 어디로도 못 가서 갇히는(끼임) 문제를 막기 위함. 게이트에서
        도달 가능한 칸에만 스폰하도록 스냅한다."""
        grid = self.layout.walkable_grid
        if reachable is None:
            snapped = grid._nearest_walkable(cell)
            return snapped
        if cell in reachable:
            return cell
        row, col = cell
        for r in range(1, max_radius + 1):
            for dr in range(-r, r + 1):
                for dc in range(-r, r + 1):
                    if max(abs(dr), abs(dc)) != r:
                        continue
                    cand = (row + dr, col + dc)
                    if cand in reachable:
                        return cand
        return None

    def _nearest_free_reachable_cell(self, cell, reachable, occupied, max_radius: int = 60):
        """2026-08-12 추가(겹침 회피): 도달 가능하면서 '아직 아무도 안 놓인' 가장 가까운 칸.

        _nearest_reachable_cell과 같은 나선 탐색이되, 이미 찬 칸(occupied)은 건너뛴다.
        여러 사람이 같은 지점(관측 클러스터)으로 와도 각자 옆 빈 칸에 배치되어 겹치지
        않는다. 탐색 순서가 고정이라 결정적(개입 전/후 동일 결과 보장). 없으면 None.
        """
        if cell in reachable and cell not in occupied:
            return cell
        row, col = cell
        for r in range(1, max_radius + 1):
            for dr in range(-r, r + 1):
                for dc in range(-r, r + 1):
                    if max(abs(dr), abs(dc)) != r:
                        continue
                    cand = (row + dr, col + dc)
                    if cand in reachable and cand not in occupied:
                        return cand
        return None

    def _spawn_agents(self) -> None:
        """
        2026-08-XX 수정: 스폰 지점이 걸을 수 없는 칸이면 가장 가까운 걸을 수
        있는 칸으로 스냅한다.

        2026-08-XX 추가: 끼임(갇힘) 방지. 장애물이 통로를 막아 생긴 '고립
        포켓'에 스폰되면 어디로도 못 가므로, 게이트에서 실제로 도달 가능한
        칸에만 스폰한다(게이트 도달성 트리의 도달 칸 집합 사용). 일방통행
        정책 등으로 트리를 못 쓸 때만 일반 walkable 스냅으로 폴백한다.
        """
        grid = self.layout.walkable_grid
        if not self._gate_tree_built:
            self._build_gate_tree()
        reachable = set(self._gate_tree[0].keys()) if self._gate_tree else None

        # 2026-08-12: 이미 사람이 놓인 칸 집합. 여러 명이 같은 지점으로 와도 겹치지
        # 않고 옆 빈 칸에 배치하기 위해(관측 클러스터/유입 공통). 배치 순서가 고정이라
        # 개입 전/후가 같은 결과를 낸다.
        occupied: set = set()

        def spawn_at(zone_id: int, x: float, y: float) -> None:
            # 스폰 지점을 게이트 도달 가능한 '빈' 칸으로 스냅한다(겹침 회피).
            cell = grid.to_cell(x, y)
            if reachable is not None:
                target = self._nearest_free_reachable_cell(cell, reachable, occupied)
            else:
                # 게이트 도달성 트리를 못 쓰는 드문 경우(일방통행 등)는 기존 스냅으로 폴백.
                target = self._nearest_reachable_cell(cell, reachable)
            if target is not None:
                occupied.add(target)
                x, y = grid.to_point(target)
            else:
                x, y = self.random_point_in_zone(zone_id)
            VisitorAgent(self, zone_id=zone_id, x=x, y=y)

        for zone_id, spec in self.layout.zones.items():
            # 1) 관측(CCTV) 위치를 먼저 그 자리에 배치 (기본 배치). 실제로 본 사람이라
            #    유입 상한과 무관하게 전부 놓는다.
            for ox, oy in self.observed_positions.get(zone_id, []):
                spawn_at(zone_id, ox, oy)

            # 2) 유입 인원(면적 비례)을 그 위에 추가로 채운다(랜덤). 관측과 별개(additive) -
            #    유입 0이면 관측된 사람만, 관측이 없으면 유입만(기존 동작).
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
                spawn_at(zone_id, x, y)

    def evaluate_risk(self) -> dict[int, RiskAssessment]:
        """현재 상태 기준으로 구역별 위험도를 재계산한다.

        2026-07-25 추가: forced_risk에 등록된 구역(화재 이벤트)은, 실측 밀집도
        기반 점수보다 강제 점수가 높으면 그 값으로 덮어쓴다.
        """
        counts = self.current_zone_counts()
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

    def register_fire(self, event: "EventTrigger") -> None:
        """2026-08-XX 추가: 화재를 활성 화재 목록에 등록한다. 실제 위험도
        반영은 매 스텝 _update_fires()가 생애주기에 따라 계산한다.

        화재 지점(origin)도 함께 저장한다 - 인지 전파(fire_front_reached)가
        이 지점 근처(도달 가능한 길)에서 바깥으로 퍼지는 데 쓴다.

        2026-08-XX 변경: 현실적으로 화재는 상가 건물에서 나는데, 이 시장의
        매대(mrkfcts01m) 점들은 전부 건물 밖 길 위에 찍혀 있어서 "매대로 스냅"
        하면 화재가 길에서 났다. 이제 화재 지점은 FE가 건물 폴리곤 위로
        스냅해 보내주는 좌표(lat/lon)를 그대로 쓴다. 좌표가 없으면 구역
        대표점을 쓴다. 건물 내부는 걸을 수 없는 칸이지만, 인지 전선은
        _update_fires에서 '가장 가까운 도달 가능한 길 칸'을 시작점으로 잡으므로
        그 상가 앞 길에 있던 사람들부터 대피하게 된다."""
        if event.latitude is not None and event.longitude is not None:
            ox, oy = self.layout.projection.to_local(event.latitude, event.longitude)
        else:
            spec = self.layout.zones.get(event.zoneId)
            rp = spec.polygon_local.representative_point()
            ox, oy = rp.x, rp.y

        self.active_fires.append({
            "zone_id": event.zoneId,
            "intensity": event.intensity,
            "ignite_step": event.triggerStep,
            "burn_steps": event.burnSteps,
            "recovery_steps": event.recoverySteps,
            "origin_x": ox,
            "origin_y": oy,
            "dist_field": None,  # 최초 활성화 시 화재 지점 기준 통로 거리장을 1회 계산해 캐시
        })

    def fire_front_reached(self, x: float, y: float, jitter: float = 0.0) -> bool:
        """2026-08-XX 추가: 활성 화재의 인지 전선이 (x,y) 지점에 도달했는지.

        화재 지점에서 통로를 따라 잰 거리가, 발화 후 경과 스텝만큼 퍼진 인지
        반경(AWARENESS_INITIAL_RADIUS + 속도*경과) 이내이면 True. 연소가 끝나면
        (진압) 인지 전선은 더 이상 확산하지 않는다(불이 꺼졌으니 새로 알 사람 없음).
        jitter는 개인별 반응 지연(거리로 환산)이라 다들 정확히 같은 순간에
        반응하지 않고 자연스럽게 흩어지게 한다.

        인지 거리장을 기본 격자로 계산하므로 에이전트 칸도 같은 기본 격자로 찾는다."""
        grid = self.layout._base_walkable_grid or self.layout.walkable_grid
        cell = grid._nearest_walkable(grid.to_cell(x, y)) or grid.to_cell(x, y)
        for fire in self.active_fires:
            age = self.current_step - fire["ignite_step"]
            if age < 0 or fire.get("dist_field") is None:
                continue
            # 인지는 연소~복구 기간 내내 계속 바깥으로 퍼진다(멀리 있는 사람에게도
            # 결국 소식이 닿게). 복구가 끝난 뒤엔 더 이상 확산하지 않는다.
            spread_steps = min(age, fire["burn_steps"] + fire["recovery_steps"])
            radius = AWARENESS_INITIAL_RADIUS_M + AWARENESS_SPEED_M_PER_STEP * spread_steps
            d = fire["dist_field"].get(cell)
            if d is not None and d + jitter <= radius:
                return True
        return False

    def _update_fires(self) -> None:
        """2026-08-XX 추가: 매 스텝 활성 화재들의 생애주기(연소->진압->복구)에
        따라 구역별 forced_risk를 다시 계산하고, 복구가 끝난 화재는 제거한다.

        각 화재의 발화 후 경과 스텝(age = current_step - ignite_step)으로 단계 판정:
          - age <= burn_steps           : phase=1.0 (연소, 위험도 최대)
          - burn_steps < age <= 끝       : phase가 1.0 -> 0으로 선형 감쇠(복구)
          - age > burn_steps+recovery    : 화재 제거(복구 완료)
        여러 화재가 겹치면 구역별로 더 높은 값을 취한다(max).
        """
        new_forced: dict[int, float] = {}
        still_active: list[dict] = []
        for fire in self.active_fires:
            age = self.current_step - fire["ignite_step"]
            if age < 0:
                still_active.append(fire)
                continue
            burn = fire["burn_steps"]
            recovery = fire["recovery_steps"]
            if age <= burn:
                phase = 1.0
            elif age <= burn + recovery:
                phase = 1.0 - (age - burn) / recovery if recovery > 0 else 0.0
            else:
                continue  # 복구 완료 - 화재 제거
            still_active.append(fire)

            # 화재가 처음 활성화된 시점에 화재 지점 기준 통로 거리장을 1회 계산해
            # 캐시한다(인지 전파 fire_front_reached가 사용). 격자 전체를 한 번만
            # 훑으므로 가볍다(게이트 도달성 계산과 동일한 인프라 재사용).
            #
            # 2026-08-XX 버그수정: 화재는 상가(매대=장애물) 좌표로 스냅되는데,
            # 오브젝트(푸드트럭 등)까지 놓이면 그 상가 옆 칸이 몇 칸짜리 고립
            # 포켓이 되는 경우가 있어, 거기서 편 거리장이 그 포켓에만 갇혀
            # 에이전트 누구에게도 인지가 안 닿는 문제가 있었다(오브젝트 배치 시
            # 화재 대피가 통째로 안 됨). 이제 인지 전선의 시작점을 "상가 옆 아무
            # 칸"이 아니라 사람들이 실제로 있는(게이트 도달 가능) 영역의 가장
            # 가까운 칸으로 잡아, 인지가 사람들 사이로 퍼지게 한다.
            if fire.get("dist_field") is None:
                # 인지(경보) 전파는 오브젝트/폐쇄 반영 "전" 기본 격자로 계산한다.
                # 푸드트럭·닫힌 통로가 화재를 "알게 되는 것"까지 막으면(트럭 뒤
                # 사람이 화재를 못 알아챔) 비현실적이므로, 경보는 자연 통로로
                # 퍼지게 하고 오브젝트/폐쇄는 대피 "이동"(walkable_grid)만 막는다.
                grid = self.layout._base_walkable_grid or self.layout.walkable_grid
                raw_cell = grid.to_cell(fire["origin_x"], fire["origin_y"])
                # 2026-08-XX 버그수정: 화재를 통로에서 멀리 떨어진(도로망과 끊긴)
                # 상가 건물 좌표에 놓으면, _nearest_walkable이 그 건물 옆의 '고립된
                # 도로 조각'을 집어서 거기서 편 거리장이 사람들(도로)에게 전혀 안
                # 닿는 문제가 있었다(먼 건물 화재 시 아무도 인지 못 하고 대피 통째
                # 실패 - 최북단 상가에 불냈을 때 재현됨). 인지 전선의 시작점을 "건물
                # 옆 아무 칸"이 아니라 사람이 실제로 있는(게이트 도달 가능) 영역의
                # 가장 가까운 도로 칸으로 잡아, 어느 위치에 불이 나도 인지가 사람들
                # 사이로 퍼지게 한다. _spawn_agents가 쓰는 것과 동일한 도달 영역.
                if not self._gate_tree_built:
                    self._build_gate_tree()
                reachable = set(self._gate_tree[0].keys()) if self._gate_tree else None
                oc = (
                    self._nearest_reachable_cell(raw_cell, reachable)
                    or grid._nearest_walkable(raw_cell)
                    or raw_cell
                )
                dist_field, _ = grid.multi_source_tree([oc])
                fire["dist_field"] = dist_field

            if phase <= 0.0:
                continue

            base_score = (FIRE_BASE_SCORE + FIRE_INTENSITY_RANGE * fire["intensity"]) * phase
            try:
                hops_from_fire = nx.single_source_shortest_path_length(
                    self.layout.graph, fire["zone_id"], cutoff=FIRE_SPREAD_MAX_HOPS
                )
            except nx.NodeNotFound:
                hops_from_fire = {fire["zone_id"]: 0}
            for zone_id, hops in hops_from_fire.items():
                decayed = base_score * (FIRE_SPREAD_DECAY ** hops)
                if decayed < FIRE_SPREAD_MIN_SCORE:
                    continue
                new_forced[zone_id] = max(new_forced.get(zone_id, 0.0), decayed)

        self.active_fires = still_active
        self.forced_risk = new_forced

    def _maintain_population(self) -> None:
        """2026-08-XX 추가: 평상시(화재 없음) 유동인구를 목표치 근처로 유지한다.

        나가는 사람만큼 게이트로 새 방문객을 유입시켜, 현재 인원이 목표보다
        population_tolerance(기본 ±5)명 넘게 부족하면 부족분을 채운다.

        2026-08-XX 변경: 예전에는 화재의 복구가 완전히 끝날 때까지(has_active_fire)
        유입을 막아, 전원 대피 후에야 갑자기 사람이 들어왔다. 이제 연소~진압
        직후 몇 스텝(fire_blocking_inflow)만 막고, 그 뒤부터는 복구가 끝나기 전
        이라도 새 방문객이 서서히 들어온다 - 나가는 사람과 겹쳐 자연스럽다."""
        if self.target_population is None or self.fire_blocking_inflow:
            return
        current = sum(1 for _ in self.agents)
        deficit = self.target_population - current
        if deficit <= self.population_tolerance:
            return
        # 한 스텝에 몰아서 유입하면(특히 화재 복구 직후 부족분이 목표 전체일 때)
        # 사람이 게이트에 순간적으로 확 튀어나온다. 스텝당 유입량을 제한해
        # 여러 스텝에 걸쳐 자연스럽게 채워지도록 한다(게이트로 서서히 유입).
        per_step_cap = max(5, self.target_population // 10)
        self.inject_inflow(min(deficit, per_step_cap))

    def zone_risk_score(self, zone_id: int) -> float:
        assessment = self._risk.get(zone_id)
        return assessment.score if assessment else 0.0

    def zone_has_forced_risk(self, zone_id: int) -> bool:
        """2026-08-XX 추가: 이 구역에 화재 이벤트로 등록된 강제 위험도가 하나라도
        있는지 여부. agents.py의 evacuation_threshold 계산에서, 화재 영향권
        구역(여기서 True)과 순수 밀집도만으로 위험도가 오른 구역(False)을
        구분하는 데 쓴다 - 화재 영향권은 기존 민감도를 유지하고, 순수 밀집도
        구역은 임계값을 더 높여 덜 민감하게 만든다.
        """
        return zone_id in self.forced_risk

    @property
    def has_active_fire(self) -> bool:
        """2026-08-XX 추가: 시뮬레이션에 활성 화재가 하나라도 있는지 여부.

        forced_risk는 화재(active_fires)의 매 스텝 계산(_update_fires)에서만
        채워지므로, 비어있지 않다는 것은 곧 지금 연소/복구 중인 화재가
        있다는 뜻이다. 복구가 완전히 끝나면 다시 비어서 False가 된다.
        agents.py의 step()에서 색(danger) 판단 등에 쓴다.
        """
        return bool(self.forced_risk)

    @property
    def fire_is_burning(self) -> bool:
        """2026-08-XX 추가: 아직 실제로 연소 중(진압 전)인 화재가 있는지.

        새로 화재를 '인지'하고 대피를 시작하는 판단에 쓴다 - 진압된 뒤에는
        불이 꺼졌으므로 새로 들어오거나 아직 모르던 사람이 대피를 시작하지
        않는다(이미 대피 중이던 사람은 마저 빠져나간다)."""
        for fire in self.active_fires:
            age = self.current_step - fire["ignite_step"]
            if 0 <= age <= fire["burn_steps"]:
                return True
        return False

    @property
    def fire_blocking_inflow(self) -> bool:
        """2026-08-XX 추가: 유동인구 유입을 막아야 하는 상태인지.

        연소 중 + 진압 직후 INFLOW_RESUME_GRACE_STEPS 스텝까지는 유입을 막고,
        그 뒤부터는 복구가 완전히 끝나기 전이라도 새 방문객이 서서히 들어온다.
        '전원 대피 완료'를 기다리지 않고 진압 후 몇 스텝 뒤 자연스럽게 유입
        재개(사용자 요청)."""
        for fire in self.active_fires:
            age = self.current_step - fire["ignite_step"]
            if 0 <= age <= fire["burn_steps"] + INFLOW_RESUME_GRACE_STEPS:
                return True
        return False

    @property
    def risk(self) -> dict[int, RiskAssessment]:
        return self._risk

    def current_zone_counts(self) -> dict[int, int]:
        counts: dict[int, int] = {zid: 0 for zid in self.layout.zones}
        for agent in self.agents:
            counts[agent.zone_id] = counts.get(agent.zone_id, 0) + 1
        return counts

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
        """
        2026-08-XX 수정: 예전에는 구역 폴리곤 전체에서 무작위로 최대 5번만
        찍어보고, 5번 다 걸을 수 없는 지점이었으면(통로 폭이 구역 면적에 비해
        좁을수록 이럴 확률이 높음) 마지막 실패 지점을 그냥 그대로 반환했다.
        그러면 에이전트가 통로 밖(격자상 안 걸을 수 있는 칸)에 스폰되는 문제가
        있었다. 이제 무작위 시도가 실패하면 그 지점 기준으로 실제 걸을 수 있는
        가장 가까운 격자 칸을 찾아 스냅한다(WalkableGrid._nearest_walkable) -
        그래도 없으면 구역 대표점(representative_point) 기준으로 다시 찾는다.
        """
        spec = self.layout.zones.get(zone_id)
        if spec is None:
            return 0.0, 0.0
        grid = self.layout.walkable_grid
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
            cell = grid.to_cell(*point)
            if grid.is_walkable(cell):
                return point
            snapped = grid._nearest_walkable(cell, max_radius=30)
            if snapped is not None:
                return grid.to_point(snapped)

        rp = spec.polygon_local.representative_point()
        snapped = grid._nearest_walkable(grid.to_cell(rp.x, rp.y), max_radius=60)
        if snapped is not None:
            return grid.to_point(snapped)
        return rp.x, rp.y

    def _cell_path_to_waypoints(
        self,
        cell_path: list[tuple[int, int]],
        to_x: float,
        to_y: float,
        arrive_zone: int | None,
    ) -> list[tuple[float, float, int | None]]:
        """cell_path(격자 셀 목록)를 실제 좌표 웨이포인트 목록으로 변환한다.

        build_path()와 nearest_reachable_gate_path()가 공통으로 쓰는 변환
        로직이라 별도 메서드로 뺐다(2026-08-XX, 게이트 탐색을 사전 계산 트리
        기반으로 바꾸면서 build_path()와 결과 형식을 동일하게 맞추기 위함).
        """
        grid = self.layout.walkable_grid
        waypoints = [grid.to_point(c) for c in cell_path[1:-1]]
        path: list[tuple[float, float, int | None]] = [(wx, wy, None) for wx, wy in waypoints]

        last_reached_x, last_reached_y = grid.to_point(cell_path[-1])
        snap_distance = ((last_reached_x - to_x) ** 2 + (last_reached_y - to_y) ** 2) ** 0.5
        if snap_distance <= grid.cell_size_m:
            path.append((to_x, to_y, arrive_zone))
        else:
            path.append((last_reached_x, last_reached_y, arrive_zone))
        return path

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

        return self._cell_path_to_waypoints(cell_path, to_x, to_y, arrive_zone)

    def _build_gate_tree(self) -> None:
        """모든 게이트를 동시에 source로 놓고 격자 전체에 대해 "가장 가까운
        도달 가능 게이트"를 한 번만 계산해서 캐시한다.

        2026-08-XX 추가: 시뮬레이션 도중 게이트 구성은 안 바뀌므로(게이트
        폐쇄는 apply_gate_closures()로 모델 생성 전에 이미 반영됨) 모델
        생명주기 동안 한 번만 계산하면 된다. nearest_reachable_gate_path()의
        docstring 참고 - 이 캐시가 없으면 에이전트마다/스텝마다 게이트
        후보별로 격자 전체를 반복 탐색해야 해서 시뮬레이션 전체 시간의
        대부분을 여기서 소모했다.

        일방통행(one_way) 정책이 걸려 있으면 사용하지 않는다 - 이 트리는
        "게이트에서 각 칸까지 갈 수 있는가"를 계산하는데(neighbors8이
        대칭이라는 전제), 방향 제한이 있으면 "칸에서 게이트로 갈 수 있는가"와
        달라질 수 있어 정확하지 않다. 그런 경우는 기존 방식(후보별 개별
        탐색)으로 대체한다.
        """
        grid = self.layout.walkable_grid
        open_gates = [g for g in self.layout.gates if g.get("zone_id") is not None]
        gate_by_cell: dict[tuple[int, int], dict] = {}
        source_cells: list[tuple[int, int]] = []
        for gate in open_gates:
            raw_cell = grid.to_cell(gate["x"], gate["y"])
            cell = grid._nearest_walkable(raw_cell) or raw_cell
            if grid.is_walkable(cell) and cell not in gate_by_cell:
                gate_by_cell[cell] = gate
                source_cells.append(cell)

        if self.layout.blocked_directions or not source_cells:
            self._gate_tree = None
        else:
            dist, came_from = grid.multi_source_tree(source_cells)
            self._gate_tree = (dist, came_from, gate_by_cell)
        self._gate_tree_built = True

    def nearest_reachable_gate_path(
        self, x: float, y: float
    ) -> tuple[dict, list[tuple[float, float, int | None]]] | None:
        """
        2026-08-XX 추가, 2026-08-XX 재수정: 예전 nearest_open_gate()는 직선거리
        기준으로 가장 가까운 게이트를 무조건 목적지로 잡았는데, 통로 데이터가
        어딘가 끊겨 있거나 걸어다닐 수 있는 영역(walkable_grid)이 여러 조각으로
        나뉘어 있으면 "직선거리로 가장 가까운 게이트"가 실제로는 지금 위치에서
        걸어갈 수 없는 곳일 수 있었다. 그러면 build_path()가 매 스텝 계속
        실패하고, 대피 중인 에이전트가 같은 시도를 영원히 반복하며 제자리에
        멈춰있는 문제가 있었다(빨간색으로 표시되지만 안 움직이는 증상).

        2026-08-XX 재작성: 원래는 직선거리순으로 게이트 후보를 정렬해 하나씩
        실제로 build_path()가 성공하는지 확인하는 방식이었는데, 이게 에이전트
        수만큼, 스텝마다 반복 호출되면서 시뮬레이션의 가장 큰 병목이었다
        (agentCount=200/steps=50 기본 시나리오가 30초 이상 걸려 백엔드 30초
        타임아웃을 넘기는 문제로 확인됨). 이제 _build_gate_tree()로 모든
        게이트로부터의 도달 가능 거리를 모델 생성 시 한 번만 계산해두고,
        여기서는 그 결과를 조회 + 경로 역추적만 한다.
        """
        if not self.layout.gates:
            return None
        if not self._gate_tree_built:
            self._build_gate_tree()

        if self._gate_tree is None:
            # 일방통행 정책이 있어 사전 계산 트리를 못 쓰는 경우 - 기존 방식으로 대체.
            candidates = sorted(
                self.layout.gates,
                key=lambda g: (g["x"] - x) ** 2 + (g["y"] - y) ** 2,
            )
            for gate in candidates:
                path = self.build_path(x, y, gate["x"], gate["y"], None)
                if path:
                    return gate, path
            return None

        grid = self.layout.walkable_grid
        dist, came_from, gate_by_cell = self._gate_tree
        raw_cell = grid.to_cell(x, y)
        start_cell = grid._nearest_walkable(raw_cell) or raw_cell
        if start_cell not in dist:
            return None

        cell_path = [start_cell]
        while cell_path[-1] not in gate_by_cell:
            cell_path.append(came_from[cell_path[-1]])
        gate = gate_by_cell[cell_path[-1]]

        if len(cell_path) < 2:
            return gate, []
        return gate, self._cell_path_to_waypoints(cell_path, gate["x"], gate["y"], None)

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
        self.current_step += 1
        # 화재 생애주기(연소/진압/복구)를 먼저 반영해 이번 스텝의 위험도를 확정.
        self._update_fires()
        if self.mode is SimulationMode.SCENARIO:
            # 화재가 없을 때만 유동인구를 목표치 근처로 유지(나간 만큼 유입).
            self._maintain_population()
            self.agents.shuffle_do("step")
        self.evaluate_risk()

    def snapshot(self) -> dict:
        projection = self.layout.projection
        agents = []
        for agent in self.agents:
            d = agent.to_dict()
            # 2026-08-14 버그수정: FE가 latitude/longitude로 그리므로, lat/lon도 to_dict의
            # 표시용 x/y(레인 분산 반영)에서 변환한다. 예전엔 raw x/y로 변환해 레인 분산이
            # 화면에 안 나타났다(_frame_agents와 동일 수정).
            lat, lon = projection.to_latlon(d["x"], d["y"])
            d["latitude"] = round(lat, 8)
            d["longitude"] = round(lon, 8)
            agents.append(d)

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