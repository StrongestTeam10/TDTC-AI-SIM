"""
보행 가능 영역을 격자(그리드)로 표현해 이동 경로를 계산하는 모듈.

도입 배경: 연속 좌표계에서 두 점을 직선으로 이어서 이동시키면,
실제 시장 구역 폴리곤은 오목한(concave) 모양이 많아서 두 점이 각각 폴리곤 내부에
있어도 그 사이 직선이 폴리곤 바깥의 오목한 부분을 가로질러 나가는 문제가 있다.

격자 기반으로 바꾸면:
  - 각 셀이 실제로 "보행 가능 영역 내부인지"를 정확히 판정할 수 있어 오목한 형태에도 안전하고
  - 매대/푸드트럭 같은 오브젝트를 "막힌 셀"로 표시하면 자연스럽게 회피 경로가 나오고
  - 통로 중심선(mrkadjc01m.path_coordinates) 데이터가 있으면 그 근처 셀의 이동 비용을
    낮춰서(선호 경로) 실제 통로를 따라 걷는 것처럼 보이게 할 수 있다.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep, PreparedGeometry

Cell = tuple[int, int]

PREFERRED_RADIUS_M = 1.5
"""통로 중심선으로부터 이 거리(m) 이내 셀은 '선호 경로'로 취급해 이동 비용을 낮춘다."""

PREFERRED_COST = 0.4
"""선호 경로 셀의 이동 비용 배율 (1.0 미만이면 그쪽으로 더 걷고 싶어함)."""


def _circle_overlaps_cell(
    ox: float, oy: float, orad: float,
    cell_minx: float, cell_maxx: float, cell_miny: float, cell_maxy: float,
) -> bool:
    """
    원(오브젝트)이 사각형(격자 셀)과 조금이라도 겹치는지 판정.

    "셀 중심점이 원 안에 있는가"만 보면, 격자 셀 크기(기본 1m)보다 작은
    반경(예: 기본 장애물 0.5m)의 오브젝트는 중심이 칸과 칸 사이에 걸칠 때
    어느 칸의 중심점도 원 안에 안 들어가 통째로 안 막힌다(에이전트가
    오브젝트 정중앙을 그냥 관통해서 지나감). 원-사각형 충돌 판정의 표준
    방법(원 중심에서 사각형까지의 최단거리)을 써서, 작은 오브젝트도
    자기가 걸친 칸은 확실히 막히게 한다.
    """
    closest_x = min(max(ox, cell_minx), cell_maxx)
    closest_y = min(max(oy, cell_miny), cell_maxy)
    dx = ox - closest_x
    dy = oy - closest_y
    return (dx * dx + dy * dy) <= orad * orad


@dataclass
class WalkableGrid:
    """보행 가능 영역을 나타내는 격자. 로컬 미터 좌표계 기준."""

    cell_size_m: float
    origin_x: float
    origin_y: float
    n_cols: int
    n_rows: int
    mask: list[list[bool]]  # mask[row][col] == True 이면 보행 가능
    cost: list[list[float]]  # cost[row][col] - 낮을수록 선호되는 경로 (기본 1.0)
    zone_of: list[list[int | None]] | None = None
    """zone_of[row][col] - 해당 셀이 속한 구역 id. blocked_zone_edges가 있을
    때만 계산된다(계산 비용 때문에 평소엔 건너뜀). 일방통행(one_way)
    통로정책을 실제 셀 단위 이동에도 반영하기 위함."""
    blocked_zone_edges: set[tuple[int, int]] | None = None
    """(from_zone_id, to_zone_id) 쌍이 여기 있으면 그 방향으로의 구역 경계
    이동을 막는다. neighbors8()에서 소비한다."""

    @classmethod
    def build(
        cls,
        walkable_area: BaseGeometry,
        obstacles: list[tuple[float, float, float]],
        preferred_lines: list[list[tuple[float, float]]] | None = None,
        cell_size_m: float = 1.0,
        padding_m: float = 2.0,
        zones: dict[int, BaseGeometry] | None = None,
        blocked_zone_edges: set[tuple[int, int]] | None = None,
    ) -> "WalkableGrid":
        """
        walkable_area: 걸어다닐 수 있는 전체 영역 (보통 모든 구역 폴리곤의 union)
        obstacles: (x, y, radius_m) 목록 - 매대/푸드트럭 등 점유 오브젝트.
            아직 실제 오브젝트 데이터가 없으면 빈 리스트를 넘기면 된다(장애물 없음).
        preferred_lines: 통로 중심선(로컬 좌표 점 목록)들. 없으면 전부 기본 비용(1.0).
        zones / blocked_zone_edges: 일방통행 정책이 있을 때만 넘긴다. zones는
            구역별 폴리곤(zone_id -> geometry), blocked_zone_edges는 막힌
            방향 쌍. 둘 다 있어야 셀별 구역 판정을 계산해 실제로 막는다.
        """
        preferred_lines = preferred_lines or []
        preferred_geoms = [
            LineString(pts) for pts in preferred_lines if len(pts) >= 2
        ]

        minx, miny, maxx, maxy = walkable_area.bounds
        minx -= padding_m
        miny -= padding_m
        maxx += padding_m
        maxy += padding_m

        n_cols = max(1, math.ceil((maxx - minx) / cell_size_m))
        n_rows = max(1, math.ceil((maxy - miny) / cell_size_m))

        prepared = prep(walkable_area)
        mask = [[False] * n_cols for _ in range(n_rows)]
        cost = [[1.0] * n_cols for _ in range(n_rows)]
        half = cell_size_m / 2

        for row in range(n_rows):
            cy = miny + (row + 0.5) * cell_size_m
            for col in range(n_cols):
                cx = minx + (col + 0.5) * cell_size_m
                point = Point(cx, cy)
                if not prepared.contains(point):
                    continue
                blocked = any(
                    _circle_overlaps_cell(
                        ox, oy, orad,
                        cx - half, cx + half, cy - half, cy + half,
                    )
                    for ox, oy, orad in obstacles
                )
                if blocked:
                    continue
                mask[row][col] = True
                if preferred_geoms and any(
                    line.distance(point) <= PREFERRED_RADIUS_M for line in preferred_geoms
                ):
                    cost[row][col] = PREFERRED_COST

        zone_of: list[list[int | None]] | None = None
        if zones and blocked_zone_edges:
            zone_preps: list[tuple[int, PreparedGeometry]] = [
                (zid, prep(poly)) for zid, poly in zones.items()
            ]
            zone_of = [[None] * n_cols for _ in range(n_rows)]
            for row in range(n_rows):
                cy = miny + (row + 0.5) * cell_size_m
                for col in range(n_cols):
                    if not mask[row][col]:
                        continue
                    cx = minx + (col + 0.5) * cell_size_m
                    point = Point(cx, cy)
                    for zid, zprep in zone_preps:
                        if zprep.contains(point):
                            zone_of[row][col] = zid
                            break

        return cls(
            cell_size_m=cell_size_m,
            origin_x=minx,
            origin_y=miny,
            n_cols=n_cols,
            n_rows=n_rows,
            mask=mask,
            cost=cost,
            zone_of=zone_of,
            blocked_zone_edges=blocked_zone_edges if zone_of else None,
        )

    def to_cell(self, x: float, y: float) -> Cell:
        col = min(max(int((x - self.origin_x) / self.cell_size_m), 0), self.n_cols - 1)
        row = min(max(int((y - self.origin_y) / self.cell_size_m), 0), self.n_rows - 1)
        return row, col

    def to_point(self, cell: Cell) -> tuple[float, float]:
        row, col = cell
        x = self.origin_x + (col + 0.5) * self.cell_size_m
        y = self.origin_y + (row + 0.5) * self.cell_size_m
        return x, y

    def is_walkable(self, cell: Cell) -> bool:
        row, col = cell
        if row < 0 or row >= self.n_rows or col < 0 or col >= self.n_cols:
            return False
        return self.mask[row][col]

    def _nearest_walkable(self, cell: Cell, max_radius: int = 5) -> Cell | None:
        """해당 셀이 막혀 있으면 주변에서 가장 가까운 보행 가능 셀을 찾는다.

        실측 좌표가 폴리곤 경계에 아주 가깝거나, 오브젝트 반경에 살짝 걸쳐서
        격자 해상도상 막힌 셀로 판정되는 경우를 보정하기 위함.
        """
        if self.is_walkable(cell):
            return cell
        row, col = cell
        for r in range(1, max_radius + 1):
            for dr in range(-r, r + 1):
                for dc in range(-r, r + 1):
                    if max(abs(dr), abs(dc)) != r:
                        continue
                    candidate = (row + dr, col + dc)
                    if self.is_walkable(candidate):
                        return candidate
        return None

    def neighbors8(self, cell: Cell) -> list[Cell]:
        """8방향(Moore) 이웃 중 보행 가능한 셀만 반환.

        대각선 이동 시 양옆 두 칸이 모두 막혀있으면(벽/오브젝트 모서리를 스쳐
        지나가는 것) 제외한다.

        zone_of/blocked_zone_edges가 설정돼 있으면(일방통행
        정책 적용 시) 현재 셀의 구역에서 후보 셀의 구역으로 넘어가는 방향이
        막혀 있는 경우도 제외한다 - one_way가 논리 그래프뿐 아니라 실제 셀
        단위 이동에도 반영되게 하기 위함.
        """
        row, col = cell
        from_zone = self.zone_of[row][col] if self.zone_of else None
        result: list[Cell] = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                candidate = (row + dr, col + dc)
                if not self.is_walkable(candidate):
                    continue
                if dr != 0 and dc != 0:
                    if not self.is_walkable((row + dr, col)) and not self.is_walkable((row, col + dc)):
                        continue  # 모서리를 스쳐 지나가는 대각선 이동 금지
                if self.zone_of is not None and self.blocked_zone_edges:
                    cr, cc = candidate
                    to_zone = self.zone_of[cr][cc]
                    if (
                        from_zone is not None
                        and to_zone is not None
                        and from_zone != to_zone
                        and (from_zone, to_zone) in self.blocked_zone_edges
                    ):
                        continue
                result.append(candidate)
        return result

    def shortest_path(self, start: Cell, goal: Cell) -> list[Cell] | None:
        """다익스트라 기반 최단(가중치 고려) 경로.

        대각선 이동은 sqrt(2)배 거리로, 셀 비용(cost)은 두 셀의 평균으로 반영해서
        통로 중심선 근처(cost가 낮은 셀)를 더 선호하도록 한다.
        시작/목표 셀이 막혀 있으면(오브젝트 위 등) 근처 보행 가능 셀로 자동 보정한다.

        A*(옥타일 거리 휴리스틱)도 시도했으나, 실제 시장
        데이터로 같은 랜덤 시드를 고정해 A/B 측정한 결과 휴리스틱 계산
        자체의 오버헤드가 탐색 절감분을 상쇄해 다익스트라보다 딱히 빠르지
        않았다(오히려 근소하게 느린 경우도 있었음). 실제 병목은 이 함수 자체의 알고리즘이 아니라
        model.nearest_reachable_gate_path()가 게이트 후보마다 이 함수를
        반복 호출하는 빈도였다(해당 함수의 docstring 참고).
        """
        start = self._nearest_walkable(start) or start
        goal = self._nearest_walkable(goal) or goal

        if start == goal:
            return [start]
        if not self.is_walkable(start) or not self.is_walkable(goal):
            return None

        dist: dict[Cell, float] = {start: 0.0}
        came_from: dict[Cell, Cell] = {}
        heap: list[tuple[float, Cell]] = [(0.0, start)]
        visited: set[Cell] = set()

        while heap:
            d, current = heapq.heappop(heap)
            if current in visited:
                continue
            visited.add(current)
            if current == goal:
                break

            cr, cc = current
            for nxt in self.neighbors8(current):
                nr, nc = nxt
                step = math.sqrt(2) if (nr != cr and nc != cc) else 1.0
                avg_cost = (self.cost[cr][cc] + self.cost[nr][nc]) / 2
                nd = d + step * avg_cost
                if nxt not in dist or nd < dist[nxt]:
                    dist[nxt] = nd
                    came_from[nxt] = current
                    heapq.heappush(heap, (nd, nxt))

        if goal not in dist:
            return None

        path = [goal]
        while path[-1] != start:
            path.append(came_from[path[-1]])
        path.reverse()
        return path

    def multi_source_tree(self, sources: list[Cell]) -> tuple[dict[Cell, float], dict[Cell, Cell]]:
        """여러 시작점(source)에서 동시에 퍼져나가는 다익스트라 - "가장 가까운
        source까지의 거리"와 "그 source 쪽으로 한 걸음 다가가는 이웃"을 격자
        전체에 대해 한 번에 계산한다.

        model.nearest_reachable_gate_path()가 에이전트마다,
        스텝마다 "직선거리순 게이트 후보 하나씩 전체 격자를 다익스트라로
        탐색"을 반복해서 시뮬레이션의 압도적 병목이었다(agentCount=200/
        steps=50 기본 시나리오가 30초 이상 걸려 백엔드 타임아웃을 넘김).
        게이트 위치는 시나리오 도중 안 바뀌므로(게이트 폐쇄는 스텝 시작 전에
        한 번만 반영됨), 모든 게이트를 동시에 source로 놓고 격자 전체에 대해
        "가장 가까운 도달 가능 게이트까지 거리"를 딱 한 번만 계산해두면,
        이후 각 에이전트는 자기 위치의 값을 조회만 하면 된다 - 격자를 여러 번
        훑을 필요가 없다. 걸을 수 없는 영역(격리된 섬)에 있는 셀은 결과에
        아예 안 나타난다(어떤 게이트로도 도달 불가능하다는 뜻).
        """
        dist: dict[Cell, float] = {}
        came_from: dict[Cell, Cell] = {}
        heap: list[tuple[float, Cell]] = []
        for src in sources:
            if self.is_walkable(src) and (src not in dist or 0.0 < dist[src]):
                dist[src] = 0.0
                heapq.heappush(heap, (0.0, src))

        visited: set[Cell] = set()
        while heap:
            d, current = heapq.heappop(heap)
            if current in visited:
                continue
            visited.add(current)

            cr, cc = current
            for nxt in self.neighbors8(current):
                nr, nc = nxt
                step = math.sqrt(2) if (nr != cr and nc != cc) else 1.0
                avg_cost = (self.cost[cr][cc] + self.cost[nr][nc]) / 2
                nd = d + step * avg_cost
                if nxt not in dist or nd < dist[nxt]:
                    dist[nxt] = nd
                    came_from[nxt] = current
                    heapq.heappush(heap, (nd, nxt))

        return dist, came_from