"""
보행 가능 영역을 격자(그리드)로 표현해 이동 경로를 계산하는 모듈.

2026-07-24 도입 배경: 기존에는 연속 좌표계에서 두 점을 직선으로 이어서 이동시켰는데,
실제 시장 구역 폴리곤은 오목한(concave) 모양이 많아서 두 점이 각각 폴리곤 내부에
있어도 그 사이 직선이 폴리곤 바깥의 오목한 부분을 가로질러 나가는 문제가 있었다.

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
from shapely.prepared import prep

Cell = tuple[int, int]

PREFERRED_RADIUS_M = 1.5
"""통로 중심선으로부터 이 거리(m) 이내 셀은 '선호 경로'로 취급해 이동 비용을 낮춘다."""

PREFERRED_COST = 0.4
"""선호 경로 셀의 이동 비용 배율 (1.0 미만이면 그쪽으로 더 걷고 싶어함)."""


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

    @classmethod
    def build(
        cls,
        walkable_area: BaseGeometry,
        obstacles: list[tuple[float, float, float]],
        preferred_lines: list[list[tuple[float, float]]] | None = None,
        cell_size_m: float = 1.0,
        padding_m: float = 2.0,
    ) -> "WalkableGrid":
        """
        walkable_area: 걸어다닐 수 있는 전체 영역 (보통 모든 구역 폴리곤의 union)
        obstacles: (x, y, radius_m) 목록 - 매대/푸드트럭 등 점유 오브젝트.
            아직 실제 오브젝트 데이터가 없으면 빈 리스트를 넘기면 된다(장애물 없음).
        preferred_lines: 통로 중심선(로컬 좌표 점 목록)들. 없으면 전부 기본 비용(1.0).
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

        for row in range(n_rows):
            cy = miny + (row + 0.5) * cell_size_m
            for col in range(n_cols):
                cx = minx + (col + 0.5) * cell_size_m
                point = Point(cx, cy)
                if not prepared.contains(point):
                    continue
                blocked = any(
                    (cx - ox) ** 2 + (cy - oy) ** 2 <= orad ** 2
                    for ox, oy, orad in obstacles
                )
                if blocked:
                    continue
                mask[row][col] = True
                if preferred_geoms and any(
                    line.distance(point) <= PREFERRED_RADIUS_M for line in preferred_geoms
                ):
                    cost[row][col] = PREFERRED_COST

        return cls(
            cell_size_m=cell_size_m,
            origin_x=minx,
            origin_y=miny,
            n_cols=n_cols,
            n_rows=n_rows,
            mask=mask,
            cost=cost,
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
        """
        row, col = cell
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
                result.append(candidate)
        return result

    def shortest_path(self, start: Cell, goal: Cell) -> list[Cell] | None:
        """다익스트라 기반 최단(가중치 고려) 경로.

        대각선 이동은 sqrt(2)배 거리로, 셀 비용(cost)은 두 셀의 평균으로 반영해서
        통로 중심선 근처(cost가 낮은 셀)를 더 선호하도록 한다.
        시작/목표 셀이 막혀 있으면(오브젝트 위 등) 근처 보행 가능 셀로 자동 보정한다.
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
