"""PostgreSQL(Supabase) 커넥션 관리."""
from __future__ import annotations

from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

from app.config import settings

_pool: SimpleConnectionPool | None = None


def init_pool(minconn: int = 1, maxconn: int = 5) -> None:
    global _pool
    if _pool is None:
        _pool = SimpleConnectionPool(minconn, maxconn, dsn=settings.dsn)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_cursor():
    """딕셔너리 형태로 결과를 반환하는 커서를 제공한다."""
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)
