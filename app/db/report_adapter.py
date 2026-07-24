"""ERD 기반 조회 DTO를 보고서 생성용 ReportRequest로 변환한다.

동일 change_id의 기준안·대안을 묶고 시나리오별 최신 실행 결과를 연결한다.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from app.schemas.report_db_models import (
    DbReportBundle,
    DensityTimeSeriesRow,
    ScenarioResultRow,
    ScenarioRow,
)
from app.schemas.report_models import (
    AlternativeResult,
    DensityTimePoint,
    Intervention,
    MetricSet,
    ReportRequest,
    ScenarioType,
    SimulationContext,
)


POLICY_CODE_TO_TYPE = {
    "NIGHT": ScenarioType.NIGHT_MARKET,
    "WALK": ScenarioType.PEDESTRIAN_DIRECTION,
    "FACIL": ScenarioType.FACILITY_RELOCATION,
    "EVENT": ScenarioType.EVENT_OPERATION,
    "EMERG": ScenarioType.EMERGENCY_RESPONSE,
    "COMBO": ScenarioType.COMBINED_POLICY,
    "CUSTM": ScenarioType.CUSTOM,
}

POLICY_CODE_TO_TITLE = {
    "NIGHT": "야시장 운영",
    "WALK": "보행동선 변경",
    "FACIL": "편의시설 배치 변경",
    "EVENT": "행사 운영",
    "EMERG": "비상 대응",
    "COMBO": "복합 정책변경",
    "CUSTM": "정책변경",
}


def _as_json(value: Any, *, field_name: str) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name}는 유효한 JSON 문자열이어야 합니다.") from exc
    return value


def _duration_to_minutes(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = value.strip().upper()
    iso = re.fullmatch(
        r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?",
        text,
    )
    if iso:
        hours = float(iso.group(1) or 0)
        minutes = float(iso.group(2) or 0)
        seconds = float(iso.group(3) or 0)
        return round(hours * 60 + minutes + seconds / 60, 2)

    hms = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", text)
    if hms:
        return round(
            float(hms.group(1)) * 60
            + float(hms.group(2))
            + float(hms.group(3)) / 60,
            2,
        )
    raise ValueError(f"avg_stay_time 형식을 해석할 수 없습니다: {value}")


def _latest_result_by_scenario(
    rows: list[ScenarioResultRow],
) -> dict[int, ScenarioResultRow]:
    latest: dict[int, ScenarioResultRow] = {}
    for row in sorted(
        rows,
        key=lambda item: (
            item.scenario_id,
            item.executed_at,
            item.result_id,
        ),
    ):
        latest[row.scenario_id] = row
    return latest


def _simulation_context(
    config: dict[str, Any],
    baseline: ScenarioRow,
) -> SimulationContext:
    raw = config.get("simulation_context", {})
    start = raw.get("simulation_start") or baseline.reg_datetime
    end = raw.get("simulation_end") or baseline.reg_datetime
    analysis_date = raw.get("analysis_date") or baseline.reg_datetime.date()

    if isinstance(analysis_date, datetime):
        analysis_date = analysis_date.date()
    elif isinstance(analysis_date, str):
        analysis_date = date.fromisoformat(analysis_date)

    return SimulationContext(
        analysis_date=analysis_date,
        simulation_start=start,
        simulation_end=end,
        expected_visitors=int(
            raw.get("expected_visitors", baseline.agent_count)
        ),
        weather=raw.get("weather"),
        temperature_c=raw.get("temperature_c"),
        rain_probability_pct=raw.get("rain_probability_pct"),
        assumptions=list(raw.get("assumptions", [])),
        model_version=raw.get("model_version"),
        random_seed=raw.get("random_seed"),
        repetitions=raw.get("repetitions"),
        density_risk_threshold_p_m2=raw.get(
            "density_risk_threshold_p_m2"
        ),
    )


def _to_interventions(space_mod_data: Any) -> list[Intervention]:
    data = _as_json(
        space_mod_data,
        field_name="space_mod_data",
    )
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("interventions", [])
    else:
        raise ValueError("space_mod_data는 JSON 객체 또는 배열이어야 합니다.")

    return [
        Intervention.model_validate(item)
        for item in raw_items
    ]


def _scenario_description(
    interventions: list[Intervention],
    *,
    is_baseline: bool,
) -> str:
    """실제 공간 변경 설정을 이용해 사실 기반 설명을 만든다."""

    descriptions = [
        item.description.strip()
        for item in interventions
        if item.description.strip()
    ]
    if descriptions:
        return "; ".join(descriptions)
    if is_baseline:
        return "정책 변경을 적용하지 않은 기준 시나리오"
    return "세부 정책 변경사항 미입력"


def _to_alternative(
    scenario: ScenarioRow,
    result: ScenarioResultRow,
    density_rows: list[DensityTimeSeriesRow],
    *,
    is_baseline: bool,
) -> AlternativeResult:
    interventions = _to_interventions(scenario.space_mod_data)
    return AlternativeResult(
        alternative_id=str(scenario.scenario_id),
        alternative_name=scenario.scenario_name,
        description=_scenario_description(
            interventions,
            is_baseline=is_baseline,
        ),
        interventions=interventions,
        metrics=MetricSet(
            max_density_p_m2=result.predicted_max_density,
            avg_density_p_m2=result.predicted_density,
            risk_score=result.predicted_risk_score,
            avg_dwell_time_min=_duration_to_minutes(result.avg_stay_time),
        ),
        density_timeseries=[
            DensityTimePoint(
                elapsed_minutes=row.elapsed_minutes,
                max_density_p_m2=row.predicted_max_density,
                avg_density_p_m2=row.predicted_density,
            )
            for row in sorted(
                density_rows,
                key=lambda item: item.elapsed_minutes,
            )
            if row.result_id == result.result_id
        ],
        flow_direction=(
            _as_json(
                result.flow_direction,
                field_name="flow_direction",
            )
            if result.flow_direction is not None
            else None
        ),
        economic_effect_analysis=result.economic_effect_analysis,
        result_id=result.result_id,
        executed_at=result.executed_at,
    )


def _scenario_type(
    policy_code: str,
    baseline_config: dict[str, Any],
) -> ScenarioType:
    """정책 코드가 우선이며, 미등록 코드일 때만 설정값을 보조로 사용한다."""

    mapped = POLICY_CODE_TO_TYPE.get(policy_code)
    if mapped is not None:
        return mapped

    raw_type = str(
        baseline_config.get(
            "scenario_type",
            ScenarioType.CUSTOM.value,
        )
    ).lower()
    try:
        return ScenarioType(raw_type)
    except ValueError:
        return ScenarioType.CUSTOM


def _report_title(
    bundle: DbReportBundle,
    policy_code: str,
) -> str:
    if bundle.report_meta.report_title:
        return bundle.report_meta.report_title

    policy_title = POLICY_CODE_TO_TITLE.get(
        policy_code,
        "정책변경",
    )
    return (
        f"{bundle.market.market_name} {policy_title} "
        "디지털 트윈 시뮬레이션 결과 보고서"
    )


def build_report_request(bundle: DbReportBundle) -> ReportRequest:
    scenarios = [
        row
        for row in bundle.scenario_rows
        if row.change_id == bundle.change_id
    ]
    if len(scenarios) < 2:
        raise ValueError(
            "동일 change_id에 기준안과 최소 1개의 대안 시나리오가 필요합니다."
        )

    scenario_ids = {
        row.scenario_id
        for row in scenarios
    }
    results = _latest_result_by_scenario(
        [
            row
            for row in bundle.result_rows
            if row.scenario_id in scenario_ids
        ]
    )
    missing = sorted(scenario_ids - set(results))
    if missing:
        raise ValueError(
            f"결과가 없는 scenario_id가 있습니다: {missing}"
        )

    configs = {
        row.scenario_id: _as_json(
            row.virtual_config,
            field_name="virtual_config",
        )
        for row in scenarios
    }
    baseline_rows = [
        row
        for row in scenarios
        if bool(configs[row.scenario_id].get("is_baseline"))
    ]
    if len(baseline_rows) != 1:
        raise ValueError(
            "virtual_config.is_baseline=true인 기준안이 정확히 1개 필요합니다."
        )

    baseline_row = baseline_rows[0]
    baseline_config = configs[baseline_row.scenario_id]
    policy_code = baseline_row.policy_type_code.upper()

    alternatives = [
        _to_alternative(
            row,
            results[row.scenario_id],
            bundle.density_timeseries_rows,
            is_baseline=False,
        )
        for row in sorted(
            scenarios,
            key=lambda item: item.scenario_id,
        )
        if row.scenario_id != baseline_row.scenario_id
    ]

    return ReportRequest(
        report_id=bundle.report_meta.report_id,
        change_id=bundle.change_id,
        scenario_type=_scenario_type(
            policy_code,
            baseline_config,
        ),
        report_title=_report_title(
            bundle,
            policy_code,
        ),
        generate_report_title=(
            bundle.report_meta.report_title is None
        ),
        decision_question=(
            bundle.report_meta.decision_question
            or "정책 변경 전후 시뮬레이션 결과는 어떻게 달라지는가?"
        ),
        market=bundle.market,
        context=_simulation_context(
            baseline_config,
            baseline_row,
        ),
        baseline=_to_alternative(
            baseline_row,
            results[baseline_row.scenario_id],
            bundle.density_timeseries_rows,
            is_baseline=True,
        ),
        alternatives=alternatives,
        disclaimer=bundle.report_meta.disclaimer,
    )
