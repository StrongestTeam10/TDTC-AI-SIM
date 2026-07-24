"""ERD 테이블 조회 결과와 보고서 생성 메타데이터의 입력 DTO를 정의한다.

Spring Boot가 조회한 시나리오·결과 행을 Python 엔진에 전달하기 전에 검증한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .report_models import MarketInfo


class ReportMeta(BaseModel):
    """DB 결과와 분리해 보고서 생성 요청 시 전달하는 문서 메타데이터."""

    report_id: str
    report_title: str | None = None
    decision_question: str | None = None
    disclaimer: str = (
        "본 문서는 DB 구조 Mock 데이터를 바탕으로 자동 생성한 정책 사전검토 초안입니다."
    )


class ScenarioRow(BaseModel):
    """SIMSCNR01M(시나리오) 조회 결과 한 행."""

    scenario_id: int
    change_id: int
    scenario_name: str
    market_id: int | str
    virtual_config: dict[str, Any] | str = Field(default_factory=dict)
    space_mod_data: dict[str, Any] | list[Any] | str = Field(default_factory=dict)
    reg_datetime: datetime
    agent_count: int = Field(ge=0)
    policy_type_code: str = Field(min_length=1, max_length=5)
    created_at: datetime


class ScenarioResultRow(BaseModel):
    """SIMRSLT01D(시나리오 예측 결과) 조회 결과 한 행."""

    result_id: int
    scenario_id: int
    predicted_max_density: float | None = Field(default=None, ge=0)
    predicted_density: float | None = Field(default=None, ge=0)
    predicted_risk_score: float | None = Field(default=None, ge=0)
    economic_effect_analysis: str | None = None
    generated_report_path: str | None = None
    avg_stay_time: str | float | int | None = None
    flow_direction: dict[str, Any] | list[Any] | str | None = None
    executed_at: datetime


class DensityTimeSeriesRow(BaseModel):
    """시간대별 밀집도 결과 상세 테이블 조회 행."""

    result_id: int
    elapsed_minutes: int = Field(ge=0)
    predicted_max_density: float | None = Field(
        default=None,
        ge=0,
    )
    predicted_density: float | None = Field(
        default=None,
        ge=0,
    )


class DbReportBundle(BaseModel):
    """Spring Boot가 여러 테이블의 조회 결과를 조립해 전달하는 요청 묶음."""

    report_meta: ReportMeta
    change_id: int
    market: MarketInfo
    scenario_rows: list[ScenarioRow] = Field(min_length=2)
    result_rows: list[ScenarioResultRow] = Field(min_length=2)
    density_timeseries_rows: list[DensityTimeSeriesRow] = Field(
        default_factory=list
    )

    @field_validator("scenario_rows")
    @classmethod
    def validate_scenario_ids(
        cls,
        values: list[ScenarioRow],
    ) -> list[ScenarioRow]:
        ids = [row.scenario_id for row in values]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario_rows의 scenario_id는 중복될 수 없습니다.")
        return values

    @field_validator("density_timeseries_rows")
    @classmethod
    def validate_density_timeseries_keys(
        cls,
        values: list[DensityTimeSeriesRow],
    ) -> list[DensityTimeSeriesRow]:
        keys = [
            (
                row.result_id,
                row.elapsed_minutes,
            )
            for row in values
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "density_timeseries_rows의 "
                "(result_id, elapsed_minutes)는 중복될 수 없습니다."
            )
        return values
