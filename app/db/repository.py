"""
DB 조회 계층.

Mesa 모델은 SQL을 직접 작성하지 않고 이 계층을 통해서만 데이터를 얻는다.
"""
from __future__ import annotations

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
    sql = (
        "SELECT adjacency_id, from_zone_id, to_zone_id, path_width, distance_m, is_active "
        "FROM mrkadjc01m WHERE market_id = %s"
    )
    if active_only:
        sql += " AND is_active = TRUE"
    with get_cursor() as cur:
        cur.execute(sql, (market_id,))
        return cur.fetchall()


def fetch_gates(market_id: int) -> list[dict]:
    with get_cursor() as cur:
        cur.execute(
            "SELECT facility_id, name, latitude, longitude "
            "FROM mrkfcts01m "
            "WHERE market_id = %s AND facility_type = 'GATE' AND is_active = TRUE",
            (market_id,),
        )
        return cur.fetchall()


def fetch_crowd_density(market_id: int, captured_at: datetime | None = None) -> list[dict]:
    """
    구역별 인구 밀집도 관측값.

    CRDDNST01M에는 market_id가 없으므로 MRKADDR01D(구역)를 조인해 시장 단위로 필터링한다.
    captured_at이 없으면 각 구역의 최신 1건을 가져온다.
    """
    with get_cursor() as cur:
        if captured_at is not None:
            cur.execute(
                """
                SELECT c.zone_id, c.visitor_count, c.density_score,
                       c.status_level, c.captured_at
                FROM crddnst01m c
                JOIN mrkaddr01d z ON z.zone_id = c.zone_id
                WHERE z.market_id = %s AND c.captured_at = %s
                """,
                (market_id, captured_at),
            )
        else:
            cur.execute(
                """
                SELECT DISTINCT ON (c.zone_id)
                       c.zone_id, c.visitor_count, c.density_score,
                       c.status_level, c.captured_at
                FROM crddnst01m c
                JOIN mrkaddr01d z ON z.zone_id = c.zone_id
                WHERE z.market_id = %s
                ORDER BY c.zone_id, c.captured_at DESC
                """,
                (market_id,),
            )
        return cur.fetchall()


def insert_risk_results(assessments: list[dict]) -> int:
    """
    산출된 위험도를 mrkrisk01m에 기록한다.
    MRKRISK01M에는 market_id가 없고 zone_id로만 시장을 식별한다.
    """
    if not assessments:
        return 0
    with get_cursor() as cur:
        cur.executemany(
            "INSERT INTO mrkrisk01m "
            "(zone_id, risk_score, risk_level, reason_code, detected_at) "
            "VALUES (%s, %s, %s, %s, NOW())",
            [
                (a["zoneId"], a["riskScore"], a["riskLevel"], a["reason"][:200])
                for a in assessments
            ],
        )
        return len(assessments)