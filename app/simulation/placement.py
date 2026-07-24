"""
오브젝트(유동인구) 배치 로직.

CCTV/LiDAR 센서는 구역 단위 집계값(인원수, 밀집도)만 제공하므로
개별 보행자의 실제 좌표는 복원할 수 없다.
따라서 구역 폴리곤 내부에 통계적으로 타당한 분포로 배치하여 근사한다.
"""

from __future__ import annotations

import math
import random
from enum import Enum

from shapely.geometry import Polygon, Point


class PlacementStrategy(str, Enum):
    """배치 전략."""

    UNIFORM = "uniform"
    """구역 내 균등 분포. 별도 사전 정보가 없을 때의 기본값."""

    CENTERLINE = "centerline"
    """
    통로 중심선 쪽에 가중치를 둔 분포.
    점포가 양옆에 늘어선 골목형 시장에서 보행자가 중앙 통로에 몰리는 현실을 반영.
    """


def _random_point_in_polygon(
    poly: Polygon,
    rng: random.Random,
    max_attempts: int = 200,
) -> Point | None:
    """
    폴리곤 내부의 무작위 점 하나를 생성(rejection sampling).

    2026-07-24: 축 정렬 경계 상자(poly.bounds) 대신 최소 회전 사각형
    (minimum_rotated_rectangle) 안에서 후보를 뽑도록 변경. 대각선으로 길게 뻗은
    좁은 폴리곤(실제 시장 골목 모양)은 축 정렬 경계 상자가 실제 면적보다 훨씬
    커서 거부율이 매우 높았고, 실패 시 같은 대표점에 계속 몰리는 문제가 있었음.

    폴리곤이 매우 가늘면(회전 사각형으로도) 실패 확률이 높아지므로, 실패 시
    None을 반환하고 호출부에서 대표점(representative_point)으로 대체하도록 한다.
    """
    mrr = poly.minimum_rotated_rectangle
    coords = list(mrr.exterior.coords)[:4]
    if len(coords) < 4:
        # 극단적으로 퇴화한(선/점) 폴리곤 - 기존 방식으로 폴백
        minx, miny, maxx, maxy = poly.bounds
        for _ in range(max_attempts):
            p = Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
            if poly.contains(p):
                return p
        return None

    origin = coords[0]
    v1 = (coords[1][0] - origin[0], coords[1][1] - origin[1])
    v2 = (coords[3][0] - origin[0], coords[3][1] - origin[1])

    for _ in range(max_attempts):
        u, v = rng.uniform(0.0, 1.0), rng.uniform(0.0, 1.0)
        x = origin[0] + v1[0] * u + v2[0] * v
        y = origin[1] + v1[1] * u + v2[1] * v
        p = Point(x, y)
        if poly.contains(p):
            return p
    return None


def _pull_toward_centerline(
    point: Point,
    poly: Polygon,
    strength: float,
    rng: random.Random,
) -> Point:
    """
    점을 폴리곤 경계에서 안쪽으로 끌어당겨 중심선 쪽에 몰리게 한다.

    strength: 0.0(효과 없음) ~ 1.0(최대). 경계 근처의 점일수록 크게 이동한다.
    """
    if strength <= 0:
        return point

    boundary_dist = poly.exterior.distance(point)
    # 폴리곤 내부에서 가장 안쪽까지의 거리(근사)
    inner = poly.representative_point()
    max_dist = max(poly.exterior.distance(inner), 1e-6)

    # 경계에 가까울수록(ratio가 작을수록) 강하게 당김
    ratio = min(boundary_dist / max_dist, 1.0)
    pull = strength * (1.0 - ratio) * rng.uniform(0.3, 1.0)

    moved = Point(
        point.x + (inner.x - point.x) * pull,
        point.y + (inner.y - point.y) * pull,
    )
    return moved if poly.contains(moved) else point


def place_visitors(
    poly_local: Polygon,
    visitor_count: int,
    strategy: PlacementStrategy = PlacementStrategy.CENTERLINE,
    centerline_strength: float = 0.5,
    seed: int | None = None,
) -> list[tuple[float, float]]:
    """
    구역 폴리곤(로컬 미터 좌표) 내부에 visitor_count 명을 배치한다.

    Returns:
        (x, y) 로컬 미터 좌표 리스트.
    """
    if visitor_count <= 0:
        return []

    rng = random.Random(seed)
    fallback = poly_local.representative_point()
    points: list[tuple[float, float]] = []

    for _ in range(visitor_count):
        p = _random_point_in_polygon(poly_local, rng)
        if p is None:
            p = fallback
        elif strategy is PlacementStrategy.CENTERLINE:
            p = _pull_toward_centerline(p, poly_local, centerline_strength, rng)
        points.append((p.x, p.y))

    return points


def local_density(visitor_count: int, area_m2: float) -> float:
    """구역 밀집도(명/m^2)."""
    if area_m2 <= 0:
        return 0.0
    return visitor_count / area_m2


def personal_space_m2(visitor_count: int, area_m2: float) -> float:
    """1인당 점유 면적(m^2/명). 압사 위험 판단의 국제 표준 지표."""
    if visitor_count <= 0:
        return float("inf")
    return area_m2 / visitor_count
