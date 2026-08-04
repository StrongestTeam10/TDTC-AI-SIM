"""ERD 기반 조회 DTO를 보고서 생성용 ReportRequest로 변환한다.

동일 change_id의 기준안·대안을 묶고 시나리오별 최신 실행 결과를 연결한다.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
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
    # 정책 유형
    # BE 공통코드(POL 그룹) - 사고·이벤트 유형
    "POLNO": ScenarioType.CUSTOM,
    "POLFR": ScenarioType.EMERGENCY_RESPONSE,
    "POLAC": ScenarioType.EMERGENCY_RESPONSE,
    "POLCB": ScenarioType.PEDESTRIAN_DIRECTION,
}

# 작성일은 한국 기준 날짜로 적는다(docx_renderer의 표기와 동일한 기준).
KST = timezone(timedelta(hours=9))

POLICY_CODE_TO_TITLE = {
    "POLNO": "정책변경",
    "POLFR": "화재 대응",
    "POLAC": "음향 이상 대응",
    "POLCB": "통로 폐쇄",
}

# 정책 유형을 특정하지 못하는 코드
NEUTRAL_POLICY_CODES = {"POLNO", "CUSTM"}

# virtual_config의 코드 값을 보고서 문장용 한국어 라벨로 변환한다.
# 미등록 값이 들어오면 원본 코드를 그대로 노출해 정보 손실을 막는다.
OBJECT_TYPE_LABELS = {
    "food_truck": "푸드트럭",
    "obstacle": "장애물",
    "event_zone": "행사 구역",
    "rest_area": "휴게 공간",
}
EVENT_TYPE_LABELS = {
    "fire": "화재",
    "acoustic_anomaly": "음향 이상",
}
CORRIDOR_ACTION_LABELS = {
    "close": "폐쇄",
    "open": "개방",
    "one_way": "일방통행",
}
CORRIDOR_DIRECTION_LABELS = {
    "from_to": "정방향",
    "to_from": "역방향",
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


def _population_assumptions(
    baseline: ScenarioRow,
    alternative_rows: list[ScenarioRow],
) -> list[str]:
    """시나리오 간 투입 인구가 다르면 분석 가정에 그 사실을 명시한다.

    야시장 개장처럼 정책이 방문객 수 자체를 늘리는 경우에는 인구수가 다른 것이
    정상이다. 다만 그때 밀집도 차이에는 정책 효과와 인구 변화 효과가 함께 섞이므로,
    독자가 이를 정책 효과만으로 오해하지 않도록 문서가 스스로 밝혀야 한다.
    """

    counts = {
        row.agent_count
        for row in [baseline, *alternative_rows]
        if row.agent_count is not None
    }
    if len(counts) <= 1:
        return []

    detail = ", ".join(
        f"{row.scenario_name} {row.agent_count:,}명"
        for row in [baseline, *alternative_rows]
        if row.agent_count is not None
    )
    return [
        "시나리오 간 투입 인구가 서로 다르다"
        f"({detail}). 따라서 밀집도·위험도 차이에는 정책 변경 효과와 "
        "방문객 수 변화 효과가 함께 반영되어 있으며, 이를 정책 효과만으로 "
        "해석하지 않도록 유의해야 한다."
    ]


def _simulation_context(
    config: dict[str, Any],
    baseline: ScenarioRow,
    extra_assumptions: list[str] | None = None,
    executed_times: list[datetime] | None = None,
) -> SimulationContext:
    """보고서 머리말에 들어갈 검토 개요를 만든다.

    두 시각을 구분해서 담는다.
      - analysis_date(작성일): 이 문서를 만든 날. RAG 근거와 LLM 본문도 이 시점 산출물이다.
      - simulation_start/end(시뮬레이션 기준 시점): 검토 대상 시나리오가 실행된 시각.
    둘을 나눠야 "언제 돌린 결과로 언제 쓴 보고서인지"를 독자가 판단할 수 있다.
    (예전에는 셋 다 현행안 등록 시각을 써서 두 정보가 모두 사라졌다)
    """

    raw = config.get("simulation_context", {})

    # 실행 시각이 여러 건이면 가장 이른 때~늦은 때를 구간으로 보여준다.
    stamps = sorted(executed_times or []) or [baseline.reg_datetime]
    start = raw.get("simulation_start") or stamps[0]
    end = raw.get("simulation_end") or stamps[-1]
    analysis_date = raw.get("analysis_date") or datetime.now(KST).date()

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
        assumptions=[
            *raw.get("assumptions", []),
            *(extra_assumptions or []),
        ],
        model_version=raw.get("model_version"),
        random_seed=raw.get("random_seed"),
        repetitions=raw.get("repetitions"),
        density_risk_threshold_p_m2=raw.get(
            "density_risk_threshold_p_m2"
        ),
    )


def _zone_label(zone_id: Any, zone_names: dict[int, str] | None) -> str:
    """구역을 문장에 쓸 이름으로 바꾼다.

    "1구역"보다 "남측 구역"이 정책 담당자에게 바로 읽히므로 실제 이름을 우선한다.
    이름을 못 찾으면(구역 정보 미전달, 신규 구역 등) 번호 표기로 돌아간다.
    """

    if zone_id is None:
        return ""
    name = (zone_names or {}).get(zone_id)
    if name and name.strip():
        return name.strip()
    return f"{zone_id}구역"


def _to_interventions(
    virtual_config: Any,
    zone_names: dict[int, str] | None = None,
) -> list[Intervention]:
    """virtual_config의 배치·이벤트·통로정책·게이트 폐쇄를 개입 목록으로 변환한다.

    별도의 space_mod_data 없이 실제 시뮬레이션 입력(virtual_config)에서 직접 파생한다.
    보고서의 '시나리오 구성' 표와 RAG 검색 질의, 동선 분석 서술이 이 목록을 사용한다.
    """

    config = _as_json(virtual_config, field_name="virtual_config")
    if not isinstance(config, dict):
        return []

    interventions: list[Intervention] = []

    for obj in config.get("objects", []):
        if not isinstance(obj, dict):
            continue
        code = str(obj.get("objectType", "")).strip()
        label = OBJECT_TYPE_LABELS.get(code, code or "오브젝트")
        zone_id = obj.get("zoneId")
        intensity = obj.get("intensity")
        if zone_id is not None:
            description = f"{_zone_label(zone_id, zone_names)}에 {label} 배치"
        else:
            description = f"{label} 배치"
        if intensity is not None:
            description += f" (강도 {intensity})"
        interventions.append(
            Intervention(
                intervention_type="object_placement",
                target_zone=None if zone_id is None else str(zone_id),
                description=description,
                parameters=dict(obj),
            )
        )

    for event in config.get("events", []):
        if not isinstance(event, dict):
            continue
        code = str(event.get("eventType", "")).strip()
        label = EVENT_TYPE_LABELS.get(code, code or "이벤트")
        zone_id = event.get("zoneId")
        intensity = event.get("intensity")
        if zone_id is not None:
            description = f"{_zone_label(zone_id, zone_names)} {label} 발생 상정"
        else:
            description = f"{label} 발생 상정"
        if intensity is not None:
            description += f" (강도 {intensity})"
        interventions.append(
            Intervention(
                intervention_type="event_trigger",
                target_zone=None if zone_id is None else str(zone_id),
                description=description,
                parameters=dict(event),
            )
        )

    for policy in config.get("corridorPolicies", []):
        if not isinstance(policy, dict):
            continue
        from_zone = policy.get("fromZoneId")
        to_zone = policy.get("toZoneId")
        action = str(policy.get("action", "")).strip()
        action_label = CORRIDOR_ACTION_LABELS.get(action, action or "정책")
        description = (
            f"{_zone_label(from_zone, zone_names)}↔"
            f"{_zone_label(to_zone, zone_names)} 통로 {action_label}"
        )
        direction = policy.get("allowedDirection")
        if direction:
            description += (
                f" ({CORRIDOR_DIRECTION_LABELS.get(direction, direction)})"
            )
        interventions.append(
            Intervention(
                intervention_type="corridor_policy",
                target_zone=None if from_zone is None else str(from_zone),
                description=description,
                parameters=dict(policy),
            )
        )

    closed_gates = config.get("closedGateIds") or []
    if closed_gates:
        gate_list = ", ".join(str(gate) for gate in closed_gates)
        interventions.append(
            Intervention(
                intervention_type="gate_closure",
                target_zone=None,
                description=f"게이트 폐쇄: {gate_list}",
                parameters={"closedGateIds": list(closed_gates)},
            )
        )

    return _merge_duplicates(interventions)


def _merge_duplicates(interventions: list[Intervention]) -> list[Intervention]:
    """설명이 같은 개입을 한 줄로 합치고 건수를 덧붙인다.

    지도에서 같은 구역에 같은 종류·강도의 이벤트를 여러 번 찍는 경우가 있다.
    그대로 나열하면 "2구역 화재 발생 상정 (강도 0.5)"가 표에 세 번 반복돼
    읽기 어렵고 지면만 차지하므로, "…(강도 0.5) 3건"으로 묶는다.
    개별 좌표는 parameters에 목록으로 보존해 정보가 사라지지 않게 한다.
    """

    merged: dict[str, Intervention] = {}
    counts: dict[str, int] = {}
    grouped_params: dict[str, list[Any]] = {}

    for item in interventions:
        key = f"{item.intervention_type}|{item.description}"
        counts[key] = counts.get(key, 0) + 1
        grouped_params.setdefault(key, []).append(item.parameters)
        merged.setdefault(key, item)

    result: list[Intervention] = []
    for key, item in merged.items():
        count = counts[key]
        if count == 1:
            result.append(item)
            continue
        result.append(
            Intervention(
                intervention_type=item.intervention_type,
                target_zone=item.target_zone,
                description=f"{item.description} {count}건",
                parameters={"items": grouped_params[key]},
            )
        )
    return result


INTERVENTION_TYPE_LABELS = {
    "object_placement": "오브젝트 배치",
    "event_trigger": "이벤트 발생",
    "corridor_policy": "통로 정책",
    "gate_closure": "게이트 폐쇄",
}


def _scenario_description(
    interventions: list[Intervention],
    *,
    is_baseline: bool,
) -> str:
    """시나리오가 무엇을 바꿨는지 한 줄로 요약한다.

    개별 변경 내역은 보고서 표의 "주요 변경사항" 열이 따로 보여주므로 여기서 다시
    나열하지 않는다. 예전에는 같은 문장을 이어붙여 두 열이 완전히 겹쳤다.
    유형별 건수만 알려주어 표를 훑을 때 성격을 빠르게 파악하게 한다.
    """

    if is_baseline:
        return "정책 변경을 적용하지 않은 현행 시나리오"
    if not interventions:
        return "세부 정책 변경사항 미입력"

    counts: dict[str, int] = {}
    for item in interventions:
        label = INTERVENTION_TYPE_LABELS.get(
            item.intervention_type, item.intervention_type
        )
        counts[label] = counts.get(label, 0) + 1

    return ", ".join(f"{label} {count}건" for label, count in counts.items())


def _to_alternative(
    scenario: ScenarioRow,
    result: ScenarioResultRow,
    density_rows: list[DensityTimeSeriesRow],
    *,
    is_baseline: bool,
    zone_names: dict[int, str] | None = None,
) -> AlternativeResult:
    """ 개입 목록: 기준안 대비 '변경량'.
    기준안은 비교의 출발점이라 변경량이 0이므로 비워 둔다.
    """
    interventions = (
        []
        if is_baseline
        else _to_interventions(scenario.virtual_config, zone_names)
    )
    return AlternativeResult(
        alternative_id=str(scenario.scenario_id),
        alternative_name=scenario.scenario_name,
        description=_scenario_description(
            interventions,
            is_baseline=is_baseline,
        ),
        agent_count=scenario.agent_count,
        interventions=interventions,
        metrics=MetricSet(
            max_density_p_m2=result.predicted_max_density,
            avg_density_p_m2=result.predicted_density,
            risk_score=result.predicted_risk_score,
            avg_dwell_time_min=_duration_to_minutes(result.avg_stay_time),
            evacuated_count=result.evacuated_count,
        ),
        max_density_zone_id=result.max_density_zone_id,
        # 구역명은 결과 행에 있으면 그대로 쓰고, 없으면 zone_names로 보완한다.
        # 보고서 기능 도입 전에 저장된 행은 이름 없이 id만 들고 있을 수 있다.
        max_density_zone_name=(
            result.max_density_zone_name
            or (zone_names or {}).get(result.max_density_zone_id)
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

def _resolve_policy_code(
    baseline_row: ScenarioRow,
    alternative_rows: list[ScenarioRow],
) -> str:
    """보고서 제목·RAG 검색에 쓸 정책 유형 코드를 고른다.

    현행안은 보통 POLNO(정책 미적용)라 유형을 특정하지 못한다.
    그 값으로 제목을 만들면 "정책변경"이라는 말만 남으므로, 이때는 대안 쪽 코드를 대신 사용한다.

    alternative_rows에는 기준안이 포함되지 않는다(별도 필드로 전달됨).
    현행안과 대안은 ID 공간이 달라 값이 겹칠 수 있으므로 scenario_id로 걸러내면 안 된다.
    """

    code = (baseline_row.policy_type_code or "").upper()
    if code and code not in NEUTRAL_POLICY_CODES:
        return code

    for row in sorted(alternative_rows, key=lambda item: item.scenario_id):
        candidate = (row.policy_type_code or "").upper()
        if candidate and candidate not in NEUTRAL_POLICY_CODES:
            return candidate

    return code


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


# 검토 질문은 표지에만 쓰이는 문구가 아니다. RAG 검색어(evidence.py)와 LLM 프롬프트
# (narrative.py의 facts.request)에 함께 들어가 보고서 전체의 논조를 결정한다.
# "어떻게 달라지는가" 같은 서술형 질문은 현상 나열로 끝나므로, 담당자가 실제로
# 답해야 하는 판단(채택할 것인가 / 무엇을 보완할 것인가)을 묻는 형태로 둔다.
POLICY_CODE_TO_DECISION_QUESTION = {
    "POLFR": (
        "화재 발생 시 현행 대응 체계로 인명 안전을 확보할 수 있는가? "
        "부족하다면 어느 구역에 어떤 보완이 필요한가?"
    ),
    "POLAC": (
        "음향 이상 상황에서 현행 대응만으로 군중을 안전하게 분산시킬 수 있는가? "
        "추가로 필요한 조치는 무엇인가?"
    ),
    "POLCB": (
        "해당 통로를 폐쇄해도 안전 수준을 유지할 수 있는가? "
        "폐쇄로 혼잡이 가중되는 구역은 어디이며 어떤 보완이 필요한가?"
    ),
}

DEFAULT_DECISION_QUESTION = (
    "이 변경안을 현행 대비 채택할 만한가? "
    "채택한다면 어느 구역에 어떤 보완 조치가 필요한가?"
)


def _decision_question(policy_code: str) -> str:
    """정책 유형에 맞는 의사결정 질문을 고른다."""

    return POLICY_CODE_TO_DECISION_QUESTION.get(
        (policy_code or "").upper(),
        DEFAULT_DECISION_QUESTION,
    )


def _policy_titles(
    baseline_row: ScenarioRow,
    alternative_rows: list[ScenarioRow],
) -> list[str]:
    """대안들이 다루는 정책 유형의 한국어 표기를 중복 없이 모은다.

    대안이 여러 개면 서로 다른 정책을 볼 수 있으므로 하나만 골라 제목에 쓰면
    나머지가 문서에서 사라진다. 등장 순서를 유지한 채 모두 모은다.
    """

    titles: list[str] = []
    rows = [*sorted(alternative_rows, key=lambda item: item.scenario_id), baseline_row]
    for row in rows:
        code = (row.policy_type_code or "").upper()
        if not code or code in NEUTRAL_POLICY_CODES:
            continue
        title = POLICY_CODE_TO_TITLE.get(code)
        if title and title not in titles:
            titles.append(title)
    return titles


def _report_title(
    bundle: DbReportBundle,
    policy_titles: list[str],
) -> str:
    if bundle.report_meta.report_title:
        return bundle.report_meta.report_title

    # 정책 유형이 3종을 넘으면 제목이 길어져 오히려 읽기 어려우므로 통칭한다.
    if not policy_titles:
        policy_title = "정책변경"
    elif len(policy_titles) > 3:
        policy_title = "정책변경 종합"
    else:
        policy_title = "·".join(policy_titles)

    return (
        f"{bundle.market.market_name} {policy_title} "
        "디지털 트윈 시뮬레이션 결과 보고서"
    )


def build_report_request(bundle: DbReportBundle) -> ReportRequest:
    # 기준안(현행안)은 별도 필드로 온다. 대안만 scenario_rows에 담긴다.
    # 두 테이블(SIMBSLN01M / SIMSCNR01M)의 ID가 각각 1부터 시작해 겹칠 수 있어,
    # 한 리스트에 섞지 않고 분리해서 받는다.
    baseline_row = bundle.baseline_scenario
    alternative_rows = list(bundle.scenario_rows)

    # 구역 번호 -> 이름. 비어 있으면 개입 문장이 "N구역" 표기로 돌아간다.
    zone_names = {
        zone.zone_id: zone.zone_name
        for zone in bundle.zones
        if zone.zone_name
    }

    alternative_ids = {
        row.scenario_id
        for row in alternative_rows
    }
    results = _latest_result_by_scenario(
        [
            row
            for row in bundle.result_rows
            if row.scenario_id in alternative_ids
        ]
    )
    missing = sorted(alternative_ids - set(results))
    if missing:
        raise ValueError(
            f"결과가 없는 scenario_id가 있습니다: {missing}"
        )

    baseline_config = _as_json(
        baseline_row.virtual_config,
        field_name="virtual_config",
    )
    policy_code = _resolve_policy_code(baseline_row, alternative_rows)

    alternatives = [
        _to_alternative(
            row,
            results[row.scenario_id],
            bundle.density_timeseries_rows,
            is_baseline=False,
            zone_names=zone_names,
        )
        for row in sorted(
            alternative_rows,
            key=lambda item: item.scenario_id,
        )
    ]

    return ReportRequest(
        report_id=bundle.report_meta.report_id,
        scenario_type=_scenario_type(
            policy_code,
            baseline_config,
        ),
        report_title=_report_title(
            bundle,
            _policy_titles(baseline_row, alternative_rows),
        ),
        generate_report_title=(
            bundle.report_meta.report_title is None
        ),
        decision_question=(
            bundle.report_meta.decision_question
            or _decision_question(policy_code)
        ),
        market=bundle.market,
        context=_simulation_context(
            baseline_config,
            baseline_row,
            _population_assumptions(baseline_row, alternative_rows),
            # 검토 대상은 대안이므로 대안의 실행 시각을 기준 시점으로 삼는다.
            [
                results[row.scenario_id].executed_at
                for row in alternative_rows
                if results[row.scenario_id].executed_at is not None
            ],
        ),
        baseline=_to_alternative(
            baseline_row,
            bundle.baseline_result,
            bundle.density_timeseries_rows,
            is_baseline=True,
            # 현행안도 최대 밀집 구역명을 보완하려면 zone_names가 필요하다. 개입 목록은
            # 현행안에서 비워 두므로 예전에는 넘길 이유가 없었다.
            zone_names=zone_names,
        ),
        alternatives=alternatives,
        # disclaimer는 ReportMeta에서 받지 않고 ReportRequest의 기본 문구를 쓴다.
    )
