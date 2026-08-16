"""
건물 폴리곤을 걸을 수 있는 영역에서 빼는 계산을 확인한다.

이 계산이 조용히 실패하면 증상이 안 보인다. 지도(FE)는 BE에서 건물을 따로 받아
그리므로 화면에는 건물이 멀쩡히 나오는데, 시뮬레이션만 건물이 없는 빈 골목으로
돌아 밀집도·대피 수치가 틀린다. 외부 공간정보 API로 건물을 자동 적재하기
시작하면 도형 품질을 우리가 통제할 수 없어 더 자주 터지므로 자동 검증이 필요하다.
"""

import json

from shapely.geometry import Polygon

from app.simulation.model import _repair_polygon, _subtract_buildings


class IdentityProjection:
    """위경도 -> 로컬 미터 변환을 건너뛴다. 여기서 검증할 것은 도형 처리뿐이다."""

    def polygon_to_local(self, poly: Polygon) -> Polygon:
        return poly


def building_row(coords: list[tuple[float, float]]) -> dict:
    ring = [[x, y] for x, y in coords]
    ring.append(ring[0])
    return {"polygon_coordinates": json.dumps({"type": "Polygon", "coordinates": [ring]})}


# 100 x 100 짜리 시장 영역
MARKET_AREA = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])


def test_정상_건물은_이동_영역에서_빠진다():
    rows = [building_row([(10, 10), (30, 10), (30, 30), (10, 30)])]

    result = _subtract_buildings(MARKET_AREA, rows, IdentityProjection())

    assert result.area == MARKET_AREA.area - 400


def test_자기교차_건물도_복구해서_반영한다():
    # 나비넥타이 모양. shapely에서 is_valid가 False라 예전 구현은 여기서 통째로 포기했다.
    bowtie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    assert not bowtie.is_valid
    assert _repair_polygon(bowtie) is not None

    rows = [building_row([(10, 10), (30, 30), (30, 10), (10, 30)])]

    result = _subtract_buildings(MARKET_AREA, rows, IdentityProjection())

    assert result.area < MARKET_AREA.area


def test_깨진_건물이_섞여도_나머지는_반영된다():
    rows = [
        {"polygon_coordinates": "이건 JSON이 아니다"},
        {"polygon_coordinates": None},
        building_row([(10, 10), (30, 10), (30, 30), (10, 30)]),
    ]

    result = _subtract_buildings(MARKET_AREA, rows, IdentityProjection())

    assert result.area == MARKET_AREA.area - 400


def test_쓸_수_있는_건물이_하나도_없으면_원본을_그대로_둔다():
    rows = [{"polygon_coordinates": "깨진 값"}]

    result = _subtract_buildings(MARKET_AREA, rows, IdentityProjection())

    assert result.area == MARKET_AREA.area


def test_건물이_영역을_통째로_덮으면_반영을_취소한다():
    # 좌표계가 어긋나거나 영역을 너무 좁게 그리면 실제로 일어난다. 그대로 두면
    # 에이전트가 설 자리가 없어 "결과가 비어 있다"는 증상만 남는다.
    rows = [building_row([(-10, -10), (110, -10), (110, 110), (-10, 110)])]

    result = _subtract_buildings(MARKET_AREA, rows, IdentityProjection())

    assert not result.is_empty
    assert result.area == MARKET_AREA.area


def test_건물이_없으면_영역이_그대로다():
    result = _subtract_buildings(MARKET_AREA, [], IdentityProjection())

    assert result.area == MARKET_AREA.area
