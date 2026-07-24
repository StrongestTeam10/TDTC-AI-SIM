"""
공간 데이터 처리 유틸.

DB의 GeoJSON 폴리곤(WGS84 위경도)을 시뮬레이션용 로컬 미터 좌표계로 변환한다.
전통시장 규모(수백 m)에서는 정밀한 투영(UTM 등) 없이
기준점 기반 등거리 근사로 충분하며, 계산 비용이 훨씬 낮다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from shapely.geometry import Polygon, Point, LineString, shape

# 위도 1도당 미터 (WGS84 평균)
METERS_PER_DEG_LAT = 111_132.0


def meters_per_deg_lon(latitude: float) -> float:
    """해당 위도에서 경도 1도당 미터."""
    return 111_320.0 * math.cos(math.radians(latitude))


@dataclass
class LocalProjection:
    """
    위경도 <-> 로컬 미터 좌표 변환기.

    origin(기준점)을 (0, 0)으로 두고, 동쪽(+x) / 북쪽(+y) 미터 단위로 변환한다.
    """

    origin_lat: float
    origin_lon: float
    _m_per_lon: float = field(init=False)

    def __post_init__(self) -> None:
        self._m_per_lon = meters_per_deg_lon(self.origin_lat)

    def to_local(self, lat: float, lon: float) -> tuple[float, float]:
        x = (lon - self.origin_lon) * self._m_per_lon
        y = (lat - self.origin_lat) * METERS_PER_DEG_LAT
        return x, y

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        lat = self.origin_lat + y / METERS_PER_DEG_LAT
        lon = self.origin_lon + x / self._m_per_lon
        return lat, lon

    def polygon_to_local(self, poly: Polygon) -> Polygon:
        """GeoJSON 폴리곤(경도, 위도 순)을 로컬 미터 좌표 폴리곤으로 변환."""
        return Polygon([self.to_local(lat, lon) for lon, lat in poly.exterior.coords])


def parse_polygon(geojson_text: str | dict) -> Polygon:
    """
    DB의 polygon_coordinates(GeoJSON 문자열)를 shapely Polygon으로 변환.
    GeoJSON 좌표 순서는 [경도, 위도]임에 주의.
    """
    data = json.loads(geojson_text) if isinstance(geojson_text, str) else geojson_text
    geom = shape(data)
    if not isinstance(geom, Polygon):
        raise ValueError(f"Polygon이 아닌 지오메트리: {geom.geom_type}")
    return geom


def parse_linestring(geojson_text: str | dict) -> LineString:
    """
    통로 중심선(mrkadjc01m.path_coordinates, GeoJSON LineString 문자열)을
    shapely LineString으로 변환. 2026-07-24 추가.
    GeoJSON 좌표 순서는 [경도, 위도]임에 주의.
    """
    data = json.loads(geojson_text) if isinstance(geojson_text, str) else geojson_text
    geom = shape(data)
    if not isinstance(geom, LineString):
        raise ValueError(f"LineString이 아닌 지오메트리: {geom.geom_type}")
    return geom


def polygon_area_m2(poly_wgs84: Polygon, projection: LocalProjection) -> float:
    """위경도 폴리곤의 실제 면적(m^2)."""
    return projection.polygon_to_local(poly_wgs84).area


def effective_width_m(poly_local: Polygon) -> float:
    """
    골목형 시장의 유효 통로 폭 추정.

    골목이 대각선 방향이면 수평/수직 단면 폭은 과대평가되므로,
    면적 / 대각 연장 길이로 근사한다.
    """
    minx, miny, maxx, maxy = poly_local.bounds
    diagonal = math.hypot(maxx - minx, maxy - miny)
    if diagonal <= 0:
        return 0.0
    return poly_local.area / diagonal
