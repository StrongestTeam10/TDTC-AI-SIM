"""ERD 테이블 조회 결과와 보고서 생성 메타데이터의 입력 DTO를 정의한다.

Spring Boot가 조회한 시나리오·결과 행을 Python 엔진에 전달하기 전에 검증한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .report_models import MarketInfo


class ReportMeta(BaseModel):
    """DB 결과와 분리해 보고서 생성 요청 시 전달하는 문서 메타데이터."""

    # report_id는 출력 폴더명·파일명으로 그대로 쓰이므로 경로 문자를 허용하면 안 된다.
    # ".."는 상위 폴더로 빠져나가고, 절대경로("/tmp/x")는 Path 결합 시 앞부분을 통째로
    # 덮어써 REPORT_OUTPUT_DIR 밖에 파일을 쓰거나 읽게 된다.
    # 영문·숫자·하이픈·밑줄만 허용해 입구에서 차단한다.
    # (BE가 만드는 "RPT-MKT1-20260731-161044-9536" 형식은 이 규칙을 만족한다)
    report_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    report_title: str | None = None
    decision_question: str | None = None


class ZoneInfo(BaseModel):
    """MRKADDR01D(시장 구역) 한 행.

    보고서 문장에 "1구역" 대신 "남측 구역"처럼 실제 이름을 쓰기 위해 받는다.
    SIM은 보고서 생성 시 DB를 조회하지 않으므로 BE가 함께 실어 보낸다.
    """

    zone_id: int
    zone_name: str | None = None


class ScenarioRow(BaseModel):
    """시나리오 한 행.

    SIMSCNR01M(대안)과 SIMBSLN01M(현행안) 모두 이 형태로 전달된다.
    현행안은 BE가 SIMBSLN01M을 읽어 같은 모양으로 맞춰 보낸다.

    개입 목록(interventions)은 virtual_config에서 파생하므로 별도의
    space_mod_data는 받지 않는다. created_at은 보고서에 쓰이지 않아
    선택 항목이다(현행안 테이블에는 대응 컬럼이 없을 수 있다).
    """

    scenario_id: int
    scenario_name: str
    market_id: int | str
    virtual_config: dict[str, Any] | str = Field(default_factory=dict)
    reg_datetime: datetime
    agent_count: int = Field(ge=0)
    policy_type_code: str = Field(min_length=1, max_length=5)
    created_at: datetime | None = None


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
    market: MarketInfo

    # 구역 번호를 이름으로 바꿔 쓰기 위한 대조표. 비어 있으면 "N구역"으로 표기한다.
    zones: list[ZoneInfo] = Field(default_factory=list)

    # 기준안(현행안)은 SIMBSLN01M/01D에서, 대안은 SIMSCNR01M/SIMRSLT01D에서 온다.
    # 두 테이블은 ID 공간이 달라(둘 다 1부터 시작) 한 리스트에 섞으면 충돌하므로
    # 기준안을 별도 필드로 받는다.
    baseline_scenario: ScenarioRow
    baseline_result: ScenarioResultRow

    scenario_rows: list[ScenarioRow] = Field(min_length=1)
    result_rows: list[ScenarioResultRow] = Field(min_length=1)
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

    @model_validator(mode="after")
    def validate_baseline_result_matches(self) -> "DbReportBundle":
        """기준안 결과가 기준안 시나리오의 것인지 확인한다."""

        if self.baseline_result.scenario_id != self.baseline_scenario.scenario_id:
            raise ValueError(
                "baseline_result.scenario_id"
                f"({self.baseline_result.scenario_id})가 "
                "baseline_scenario.scenario_id"
                f"({self.baseline_scenario.scenario_id})와 다릅니다."
            )
        return self

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
