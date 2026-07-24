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
from shapely.ops import unary_union

from app.simulation.agents import VisitorAgent
from app.simulation.gridspace import WalkableGrid
from app.simulation.placement import (
    PlacementStrategy,
    place_visitors,
)
from app.simulation.risk import RiskAssessment, assess_zone
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

    @classmethod
    def from_db_rows(
        cls,
        market_row: dict,
        zone_rows: list[dict],
        adjacency_rows: list[dict],
        gate_rows: list[dict],
        stall_rows: list[dict] | None = None,
    ) -> "MarketLayout":
        """
        DB 조회 결과로부터 레이아웃을 구성한다.

        market_row  : mrkaddr01m 1행
        zone_rows   : mrkaddr01d 목록 (polygon_coordinates는 GeoJSON 문자열)
        adjacency_rows: mrkadjc01m 목록 (is_active=True인 것만 전달할 것)
        gate_rows   : mrkfcts01m 중 facility_type='GATE' 목록 (weight = 유입 가중치)
        stall_rows  : mrkfcts01m 중 facility_type!='GATE' 목록 (weight = 매력도 가중치).
            2026-07-24: 예측 시뮬레이션(신규 유입 이후 자연스러운 이동 예측)을 위해 추가.
        """
        stall_rows = stall_rows or []
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
                "weight": float(row["weight"]) if row.get("weight") is not None else 1.0,
            }
            gates.append(gate)
            if nearest is not None:
                nearest.is_exit_zone = True

        # 매대(오브젝트) - 가장 가까운 구역에 매력도(attraction)를 누적한다.
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

        # 보행 가능 영역 격자 구축: 모든 구역 폴리곤의 union이 걸어다닐 수 있는
        # 전체 영역. 매대(오브젝트)는 장애물로, 통로 중심선(path_coordinates)이
        # 있으면 선호 경로로 반영한다.
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

        return cls(
            market_id=market_row["market_id"],
            market_name=market_row["market_name"],
            projection=projection,
            zones=zones,
            graph=graph,
            walkable_grid=walkable_grid,
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
        """관측된 인원수만큼 각 구역 폴리곤 내부에 에이전트를 배치한다.

        2026-07-24: 매대(오브젝트) 위에 배치되지 않도록, 오브젝트 반경 안에 떨어진
        점은 random_point_in_zone()(재시도 로직 포함)으로 다시 뽑는다.
        """
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

    # ---------- 위험도 ----------

    def evaluate_risk(self) -> dict[int, RiskAssessment]:
        """현재 상태 기준으로 구역별 위험도를 재계산한다."""
        counts = self.current_zone_counts()
        self._risk = {}
        for zone_id, spec in self.layout.zones.items():
            self._risk[zone_id] = assess_zone(
                zone_id=zone_id,
                visitor_count=counts.get(zone_id, 0),
                area_m2=spec.area_m2,
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

    @property
    def movement_graph(self) -> nx.Graph:
        """VisitorAgent가 인접 구역 탐색에 쓰는 그래프. 구역 인접 그래프를 그대로 재사용한다.

        2026-07-24: VisitorAgent._maybe_move_toward_attraction()이 참조하는데
        실제로는 정의돼 있지 않던 버그를 수정하며 추가함.
        """
        return self.layout.graph

    def attraction_of(self, zone_id: int) -> float:
        """해당 구역에 배치된 매대(오브젝트)들의 weight 합.

        값이 클수록 정상 보행(NORMAL) 상태의 방문객이 이 구역으로 이끌릴 확률이
        높아진다. 2026-07-24: VisitorAgent._maybe_move_toward_attraction()이
        참조하는데 실제로는 정의돼 있지 않던 버그를 수정하며 추가함.
        """
        spec = self.layout.zones.get(zone_id)
        return spec.attraction if spec else 0.0

    def random_point_in_zone(self, zone_id: int) -> tuple[float, float]:
        spec = self.layout.zones.get(zone_id)
        if spec is None:
            return 0.0, 0.0
        # 오브젝트(매대 등) 위에 떨어지면 최대 몇 번 다시 뽑는다. 그래도 안 되면
        # (매대가 구역 대부분을 덮는 극단적인 경우) 마지막으로 뽑은 점을 그대로 쓴다 -
        # walkable_grid.bfs가 어차피 가장 가까운 보행 가능 셀로 보정해준다.
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
        """두 점 사이를 격자 기반 최단 경로(WalkableGrid.shortest_path)로 잇는다.

        2026-07-24: 직선 보간 대신 격자 경로로 바꿔서 (1) 오목한 폴리곤 형태에서도
        보행 가능 영역 밖으로 나가지 않고, (2) 매대/푸드트럭 같은 오브젝트를 자동으로
        회피하고, (3) 통로 중심선 데이터가 있으면 그쪽을 선호해서 걷는다.
        arrive_zone: 도착 시 설정할 zone_id. 같은 구역 안에서의 이동(배회)이면 None.
        경로를 못 찾은 경우(격자 밖 좌표 등 예외적 상황)는 안전을 위해 목적지로
        바로 이동하는 것으로 폴백한다.
        """
        grid = self.layout.walkable_grid
        cell_path = grid.shortest_path(grid.to_cell(from_x, from_y), grid.to_cell(to_x, to_y))
        if not cell_path or len(cell_path) < 2:
            return [(to_x, to_y, arrive_zone)]

        # 시작 셀은 현재 위치와 사실상 같으니 제외하고, 중간 경유점만 웨이포인트로 쓴다.
        # 마지막 지점은 격자 중심 좌표 대신 실제 목적지 좌표를 그대로 써서 정확하게 도착시킨다.
        waypoints = [grid.to_point(c) for c in cell_path[1:-1]]
        path: list[tuple[float, float, int | None]] = [(wx, wy, None) for wx, wy in waypoints]
        path.append((to_x, to_y, arrive_zone))
        return path

    # ---------- 실행 ----------

    def inject_inflow(self, count: int) -> None:
        """
        게이트 weight에 비례해 신규 방문객을 유입시킨다 (예측 시뮬레이션 전용).

        step() 호출 전에 불러야 그 스텝부터 이동 로직에 참여한다. 반올림 오차로
        실제 유입 인원이 count와 정확히 일치하지 않을 수 있다. 게이트의 실제
        물리적 좌표(x, y)에서 스폰시켜서 "문으로 들어오는" 것처럼 보이게 한다.
        """
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
