"""기준안과 복수 정책 대안의 시뮬레이션 지표 변화량이 올바르게 계산되는지 검증한다."""

from pathlib import Path

from app.db.report_adapter import build_report_request
from app.reporting.analytics import assess_request
from app.schemas.report_db_models import DbReportBundle


ROOT = Path(__file__).resolve().parents[2]


def test_assess_request_compares_all_alternatives():
    mock_path = ROOT / "data" / "db" / "night_market.json"

    bundle = DbReportBundle.model_validate_json(
        mock_path.read_text(encoding="utf-8")
    )
    request = build_report_request(bundle)

    assessments = assess_request(request)

    assert len(assessments) == len(request.alternatives)

    assert [
        item.alternative_id
        for item in assessments
    ] == [
        item.alternative_id
        for item in request.alternatives
    ]

    for assessment in assessments:
        assert assessment.available_metric_count > 0
        assert len(assessment.deltas) > 0
        assert not hasattr(assessment, "score")
        assert not hasattr(assessment, "status")