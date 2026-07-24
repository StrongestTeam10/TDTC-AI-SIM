"""TDTC-AI-SIM: 전통시장 디지털 트윈 시뮬레이션 엔진 (FastAPI + Mesa)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, simulate, reports
from app.db import connection

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB가 일시적으로 불안정해도 앱은 기동되어야 한다.
    # 커넥션 풀은 지연 초기화되므로, 여기서 실패해도 첫 요청 시 재시도된다.
    try:
        connection.init_pool()
    except Exception as exc:  # noqa: BLE001
        logger.warning("기동 시 DB 커넥션 풀 초기화 실패, 지연 초기화로 전환합니다: %s", exc)
    yield
    try:
        connection.close_pool()
    except Exception:  # noqa: BLE001
        logger.warning("DB 커넥션 풀 종료 중 오류", exc_info=True)


app = FastAPI(
    title="TDTC-AI-SIM",
    description="전통시장 안전탐지 디지털 트윈 시뮬레이션 엔진",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(simulate.router)
app.include_router(reports.router)