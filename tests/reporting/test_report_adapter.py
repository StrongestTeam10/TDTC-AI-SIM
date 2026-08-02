"""ERD 기반 Mock 조회 데이터가 보고서 생성용 내부 모델로 올바르게 변환되는지 검증한다."""

from pathlib import Path

from app.db.report_adapter import build_report_request
from app.schemas.report_db_models import DbReportBundle


ROOT = Path(__file__).resolve().parents[2]


def test_build_report_request_from_erd_mock():
    mock_path = (
        ROOT
        / "data"
        / "db"
        / "night_market.json"
    )

    bundle = DbReportBundle.model_validate_json(
        mock_path.read_text(encoding="utf-8")
    )

    request = build_report_request(bundle)

    assert request.baseline is not None
    assert request.baseline.alternative_id == "110001"
    assert len(request.alternatives) >= 1

    # 기준안은 대안 목록에 섞이지 않는다.
    assert all(
        alternative.alternative_id != "110001"
        for alternative in request.alternatives
    )

    # 기준안은 비교의 출발점이라 변경사항이 비어 있어야 한다.
    assert request.baseline.interventions == []

    # 대안의 변경사항은 virtual_config에서 파생된다.
    assert all(
        alternative.interventions
        for alternative in request.alternatives
    )

    # 기준안의 시간대별 밀집도 결과가 변환됐는지 확인한다.
    assert len(
        request.baseline.density_timeseries
    ) >= 1

    # 모든 대안의 시간대별 밀집도 결과가 변환됐는지 확인한다.
    assert all(
        len(alternative.density_timeseries) >= 1
        for alternative in request.alternatives
)