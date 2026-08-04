"""
DB 조회 계층.

Mesa 모델은 SQL을 직접 작성하지 않고 이 계층을 통해서만 데이터를 얻는다.
"""
from __future__ import annotations

import json
from datetime import datetime

from app.db.connection import get_cursor


def fetch_market(market_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute(
            "SELECT market_id, market_name, latitude, longitude "
            "FROM mrkaddr01m WHERE market_id = %s",
            (market_id,),
        )
        return cur.fetchone()


def fetch_zones(market_id: int) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT zone_id, market_id, zone_name, polygon_coordinates "
            "FROM mrkaddr01d WHERE market_id = %s ORDER BY zone_id",
            (market_id,),
        )
        return cur.fetchall()


def fetch_adjacency(market_id: int, active_only: bool = True) -> list[dict]:
    """
    MRKADJC01M에는 market_id가 없으므로(from_zone_id가 속한 구역을 통해 이미 알 수
    있어 중복이었음, BE ZoneAdjacencyRepository와 동일한 방식) MRKADDR01D(구역)를
    from_zone_id 기준으로 조인해 시장 단위로 필터링한다. uq_mrkadjc01m_edge 제약상
    from/to 조합은 항상 같은 시장 내에서만 만들어지므로 from_zone_id 조인만으로 충분하다.
    """
    sql = (
        "SELECT a.adjacency_id, a.from_zone_id, a.to_zone_id, a.path_width, a.distance_m, "
        "a.is_active, a.path_coordinates "
        "FROM mrkadjc01m a "
        "JOIN mrkaddr01d z ON z.zone_id = a.from_zone_id "
        "WHERE z.market_id = %s"
    )
    if active_only:
        sql += " AND a.is_active = TRUE"
    with get_cursor() as cur:
        cur.execute(sql, (market_id,))
        return cur.fetchall()


def fetch_gates(market_id: int) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT facility_id, name, latitude, longitude, weight "
            "FROM mrkfcts01m "
            "WHERE market_id = %s AND facility_type = 'GATE' AND is_active = TRUE",
            (market_id,),
        )
        return cur.fetchall()


def fetch_stalls(market_id: int) -> list[dict]:
    """
    매력도(attraction)를 만드는 오브젝트(매대/적재물 등).
    GATE를 제외한 모든 활성 시설을 매대로 취급한다 (BE SpatialLayoutService의
    "GATE가 아니면 stall" 규칙과 동일).
    footprint_radius_m: 오브젝트가 실제로 차지하는 반경(m). 없으면 model.py의
    DEFAULT_FACILITY_RADIUS_M으로 대체됨 (2026-07-24: 장애물 회피 경로 계산용 추가).
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT facility_id, name, latitude, longitude, weight, footprint_radius_m "
            "FROM mrkfcts01m "
            "WHERE market_id = %s AND facility_type != 'GATE' AND is_active = TRUE",
            (market_id,),
        )
        return cur.fetchall()


def fetch_buildings(market_id: int) -> list[dict]:
    """
    2026-08-XX 추가: 상가/건물 폴리곤(MRKBLDG01M). 원래 지도에 3D 느낌으로
    표시하는 용도로만 BE/FE에 연결돼 있었는데, SIM은 이 데이터를 몰라서
    에이전트가 시장 구역 폴리곤(남측/중앙/북측 등) 전체를 걸어다닐 수 있는
    것으로 취급해 상가 건물 자리까지 그냥 통과할 수 있었다. 이제 이동 경로
    계산(walkable_area)에서 건물이 차지하는 실제 모양을 빼서, 에이전트가
    건물 사이 통로로만 다니게 하는 데 쓴다.
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT pnu_code, polygon_coordinates "
            "FROM mrkbldg01m WHERE market_id = %s",
            (market_id,),
        )
        return cur.fetchall()


def fetch_latest_pedestrian_frames(market_id: int, captured_at: datetime | None = None) -> list[dict]:
    """
    구역별 CCTV 프레임 좌표 1건을 조회한다.

    2026-07-31: 파이프라인 A의 실측 관측값 소스를 센서(CRDDNST01M, DROP됨)에서
    CCTV 보행자 좌표(PEDAGGR01H)로 전환. PEDAGGR01H에는 zone_id가 없으므로
    VDOCLIP01M(zone_id 보유)을 clip_id로, MRKADDR01D(market_id 보유)를 zone_id로
    조인해서 시장 단위로 필터링한다.

    captured_at을 지정하지 않으면 구역별 최신 1건을 가져온다. 같은 클립(영상) 안의
    프레임들은 captured_at이 전부 동일한 값으로 적재되므로(실제 데이터로 확인됨),
    "가장 최신 프레임"을 구분하려면 frame_id를 보조 정렬 기준으로 반드시 같이 써야
    한다 - captured_at만으로 정렬하면 같은 클립 내에서 순서가 임의로 섞인다.
    """
    base_select = (
        "SELECT DISTINCT ON (v.zone_id) "
        "p.coord_id, v.zone_id, p.bev_xyz_json, p.captured_at, p.frame_id "
        "FROM pedaggr01h p "
        "JOIN vdoclip01m v ON v.clip_id = p.clip_id "
        "JOIN mrkaddr01d z ON z.zone_id = v.zone_id "
    )
    with get_cursor() as cur:
        if captured_at is not None:
            cur.execute(
                base_select
                + "WHERE z.market_id = %s AND p.captured_at = %s "
                "ORDER BY v.zone_id, p.frame_id DESC",
                (market_id, captured_at),
            )
        else:
            cur.execute(
                base_select
                + "WHERE z.market_id = %s "
                "ORDER BY v.zone_id, p.captured_at DESC, p.frame_id DESC",
                (market_id,),
            )
        return cur.fetchall()


def count_people_in_frame(bev_xyz_json) -> int:
    """
    bev_xyz_json 1건의 인원수를 센다.

    실제 적재 데이터를 확인한 결과 {"person_1": {"x":..,"y":..}, "person_2": {...}}
    형태의 JSON **객체**다(배열이 아님 - PEDAGGR01H 테이블 정의 주석에 남아있던
    "JSON 배열" 가정은 폐기, person_N은 프레임 간 지속되는 추적 ID). 최상위 키
    개수가 곧 해당 프레임의 인원수다.
    """
    if not bev_xyz_json:
        return 0
    data = bev_xyz_json if isinstance(bev_xyz_json, dict) else json.loads(bev_xyz_json)
    return len(data)


def insert_risk_results(assessments: list[dict]) -> int:
    """
    산출된 위험도를 mrkrisk01m에 기록한다.

    2026-07-31: MRKRISK01M의 그레인이 "구역 단위(zone_id)"에서 "CCTV 프레임
    좌표 1건 단위(coord_id, PEDAGGR01H 참조)"로 바뀜에 따라 zone_id 대신 coord_id로
    INSERT하도록 변경. coord_id는 NOT NULL FK라 호출부(app/api/simulate.py)가
    CCTV 프레임이 있는 구역만 걸러서 넘겨줘야 한다 - 프레임이 없는 구역은 이
    함수에서도 한 번 더 방어적으로 걸러낸다.
    total_count는 해당 프레임의 실측 인원수(선택 컬럼, ERD상 nullable).
    """
    rows = [a for a in assessments if a.get("coordId") is not None]
    if not rows:
        return 0
    with get_cursor() as cur:
        cur.executemany(
            "INSERT INTO mrkrisk01m "
            "(coord_id, risk_score, risk_level, reason_code, detected_at, total_count) "
            "VALUES (%s, %s, %s, %s, NOW(), %s)",
            [
                (
                    a["coordId"],
                    a["riskScore"],
                    a["riskLevel"],
                    a["reason"][:200],
                    a.get("totalCount"),
                )
                for a in rows
            ],
        )
        return len(rows)