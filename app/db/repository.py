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
    captured_at이 없으면 각 구역의 최신 1건을 가져온다.
    """
    with get_cursor() as cur:
        if captured_at is not None:
            cur.execute(
                "SELECT zone_id, visitor_count, density_score, status_level, captured_at "
                "FROM crddnst01m WHERE market_id = %s AND captured_at = %s",
                (market_id, captured_at),
            )
        else:
            cur.execute(
                """
                SELECT DISTINCT ON (zone_id)
                       zone_id, visitor_count, density_score, status_level, captured_at
                FROM crddnst01m
                WHERE market_id = %s
                ORDER BY zone_id, captured_at DESC
                """,
                (market_id,),
            )
        return cur.fetchall()


def fetch_radar_speed(market_id: int) -> dict[int, float]:
    """구역별 최신 평균 이동 속도(레이더). zone_id -> avg_speed."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (s.zone_id) s.zone_id, r.avg_speed
            FROM senrad01m r
            JOIN sensens01m s ON s.sensor_id = r.sensor_id
            WHERE s.market_id = %s
            ORDER BY s.zone_id, r.updated_at DESC
            """,
            (market_id,),
        )
        return {r["zone_id"]: r["avg_speed"] for r in cur.fetchall() if r["avg_speed"] is not None}


def fetch_acoustic_events(market_id: int, since: datetime | None = None) -> dict[int, dict]:
    """구역별 이상 음향 이벤트 집계. zone_id -> {count, max_confidence}."""
    sql = """
        SELECT s.zone_id, COUNT(*) AS cnt, MAX(a.confidence) AS max_conf
        FROM audevnt01m a
        JOIN sensens01m s ON s.sensor_id = a.sensor_id
        WHERE s.market_id = %s
    """
    params: list = [market_id]
    if since is not None:
        sql += " AND a.detected_at >= %s"
        params.append(since)
    sql += " GROUP BY s.zone_id"

    with get_cursor() as cur:
        cur.execute(sql, tuple(params))
        return {
            r["zone_id"]: {
                "count": r["cnt"],
                "max_confidence": float(r["max_conf"]) if r["max_conf"] is not None else None,
            }
            for r in cur.fetchall()
        }


def insert_risk_results(market_id: int, assessments: list[dict]) -> int:
    """산출된 위험도를 mrkrisk01m에 기록한다."""
    if not assessments:
        return 0
    with get_cursor() as cur:
        cur.executemany(
            "INSERT INTO mrkrisk01m "
            "(market_id, zone_id, risk_score, risk_level, reason_code, detected_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW())",
            [
                (market_id, a["zoneId"], a["riskScore"], a["riskLevel"], a["reason"][:200])
                for a in assessments
            ],
        )
        return len(assessments)
