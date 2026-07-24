"""현재 ERD가 제공하는 지표를 이용해 기준안과 대안의 변화량을 계산한다."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.schemas.report_models import MetricSet, ReportRequest


# 보고서에서 표시할 시뮬레이션 결과 지표명
METRIC_LABELS = {
    "max_density_p_m2": "최대 밀집도(명/㎡)",
    "avg_density_p_m2": "평균 밀집도(명/㎡)",
    "risk_score": "예측 위험점수(점)",
    "avg_dwell_time_min": "평균 체류시간(분)",
}


# 값이 낮아졌을 때 안전 측면에서 개선으로 해석할 수 있는 지표
# 평균 체류시간은 정책 목적에 따라 의미가 달라지므로 자동 평가하지 않는다.
LOWER_IS_BETTER = {
    "max_density_p_m2": True,
    "avg_density_p_m2": True,
    "risk_score": True,
    "avg_dwell_time_min": None,
}


@dataclass
class MetricDelta:
    """기준안과 대안 사이의 지표 변화량."""

    key: str
    label: str
    baseline: float | int | None
    candidate: float | int | None
    absolute: float | None
    percent: float | None
    improved: bool | None


@dataclass
class AlternativeAssessment:
    """대안별 지표 비교 결과.

    별도의 점수나 검토 상태를 산출하지 않고
    전달받은 시뮬레이션 결과의 변화량만 보관한다.
    """

    alternative_id: str
    alternative_name: str
    deltas: list[MetricDelta]
    available_metric_count: int

    def to_dict(self) -> dict:
        return asdict(self)


def _percent(
    baseline: float | int | None,
    candidate: float | int | None,
) -> float | None:
    """기준안 대비 대안의 변화율을 계산한다."""

    if baseline is None or candidate is None or baseline == 0:
        return None

    return round(
        (float(candidate) - float(baseline))
        / float(baseline)
        * 100,
        1,
    )


def compare_metrics(
    baseline: MetricSet,
    candidate: MetricSet,
) -> list[MetricDelta]:
    """기준안과 대안의 지표별 절대 변화량과 변화율을 계산한다."""

    baseline_values = baseline.model_dump()
    candidate_values = candidate.model_dump()

    rows: list[MetricDelta] = []

    for key, label in METRIC_LABELS.items():
        baseline_value = baseline_values.get(key)
        candidate_value = candidate_values.get(key)

        if baseline_value is None or candidate_value is None:
            absolute = None
        else:
            absolute = round(
                float(candidate_value) - float(baseline_value),
                2,
            )

        percent = _percent(
            baseline_value,
            candidate_value,
        )

        direction = LOWER_IS_BETTER.get(key)

        if absolute is None or direction is None:
            improved = None
        elif direction:
            improved = absolute < 0
        else:
            improved = absolute > 0

        rows.append(
            MetricDelta(
                key=key,
                label=label,
                baseline=baseline_value,
                candidate=candidate_value,
                absolute=absolute,
                percent=percent,
                improved=improved,
            )
        )

    return rows


def assess_request(
    request: ReportRequest,
) -> list[AlternativeAssessment]:
    """모든 대안과 기준안의 지표 변화량을 계산한다."""

    assessments: list[AlternativeAssessment] = []

    for alternative in request.alternatives:
        deltas = compare_metrics(
            request.baseline.metrics,
            alternative.metrics,
        )

        available_metric_count = sum(
            1
            for delta in deltas
            if delta.baseline is not None
            and delta.candidate is not None
        )

        assessments.append(
            AlternativeAssessment(
                alternative_id=alternative.alternative_id,
                alternative_name=alternative.alternative_name,
                deltas=deltas,
                available_metric_count=available_metric_count,
            )
        )

    # 보고서 엔진이 우선순위를 결정하지 않으므로
    # 입력받은 대안 순서를 그대로 유지한다.
    return assessments
