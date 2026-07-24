"""보고서 생성 파이프라인에서 공통으로 사용하는 도메인 모델을 정의한다.

DB 어댑터, 분석, RAG 서술 및 문서 렌더러 사이의 데이터 계약을 검증한다.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ScenarioType(str, Enum):
    NIGHT_MARKET = "night_market"
    PEDESTRIAN_DIRECTION = "pedestrian_direction"
    FACILITY_RELOCATION = "facility_relocation"
    EVENT_OPERATION = "event_operation"
    EMERGENCY_RESPONSE = "emergency_response"
    COMBINED_POLICY = "combined_policy"
    CUSTOM = "custom"


class MarketInfo(BaseModel):
    """MRKADDR01M에서 보고서에 필요한 시장 기본정보."""

    market_id: int | str
    market_name: str
    latitude: float | None = None
    longitude: float | None = None


class SimulationContext(BaseModel):
    analysis_date: date
    simulation_start: datetime
    simulation_end: datetime
    expected_visitors: int = Field(ge=0)
    assumptions: list[str] = Field(default_factory=list)
    model_version: str | None = None
    random_seed: int | None = None
    repetitions: int | None = Field(default=None, ge=1)
    density_risk_threshold_p_m2: float | None = Field(
        default=None,
        ge=0,
    )


class Intervention(BaseModel):
    intervention_type: str
    target_zone: str | None = None
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class MetricSet(BaseModel):
    """SIMRSLT01D에서 직접 전달받는 시뮬레이션 결과 지표."""

    max_density_p_m2: float | None = Field(default=None, ge=0)
    avg_density_p_m2: float | None = Field(default=None, ge=0)
    risk_score: float | None = Field(default=None, ge=0)
    avg_dwell_time_min: float | None = Field(default=None, ge=0)


class DensityTimePoint(BaseModel):
    """시뮬레이션 경과시간별 밀집도 예측값."""

    elapsed_minutes: int = Field(ge=0)
    max_density_p_m2: float | None = Field(default=None, ge=0)
    avg_density_p_m2: float | None = Field(default=None, ge=0)


class AlternativeResult(BaseModel):
    alternative_id: str
    alternative_name: str
    description: str | None = None
    interventions: list[Intervention] = Field(default_factory=list)
    metrics: MetricSet = Field(default_factory=MetricSet)
    density_timeseries: list[DensityTimePoint] = Field(
        default_factory=list
    )
    flow_direction: dict[str, Any] | list[Any] | str | None = None
    economic_effect_analysis: str | None = None
    result_id: int | None = None
    executed_at: datetime | None = None


class EvidenceItem(BaseModel):
    """벡터 검색으로 조회한 근거 문서 청크."""

    source_id: str
    title: str
    page: int | None = None
    excerpt: str
    relevance_tags: list[str] = Field(default_factory=list)


class ReportRequest(BaseModel):
    """DB 행을 보고서 생성에 적합한 구조로 변환한 내부 요청 모델."""

    report_id: str
    change_id: int
    scenario_type: ScenarioType
    report_title: str
    generate_report_title: bool = False
    decision_question: str
    market: MarketInfo
    context: SimulationContext
    baseline: AlternativeResult
    alternatives: list[AlternativeResult] = Field(min_length=1)
    disclaimer: str = (
        "본 문서는 시뮬레이션 예측 결과와 검색 근거자료를 바탕으로 자동 생성한 "
        "정책 사전검토 초안이며, 담당자의 검토와 승인을 대체하지 않습니다."
    )

    @field_validator("alternatives")
    @classmethod
    def unique_alternative_ids(
        cls,
        values: list[AlternativeResult],
    ) -> list[AlternativeResult]:
        ids = [value.alternative_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("alternative_id는 중복될 수 없습니다.")
        return values
