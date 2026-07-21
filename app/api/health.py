"""헬스체크 엔드포인트. Spring Boot의 SimulationEngineClient가 호출한다."""
from __future__ import annotations

from fastapi import APIRouter

from app.db.connection import get_cursor

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {"status": "UP"}


@router.get("/health/db")
def health_db() -> dict:
    """DB 연결까지 확인하는 심층 헬스체크."""
    try:
        with get_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        return {"status": "UP", "database": "UP"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "DOWN", "database": "DOWN", "detail": str(exc)}
