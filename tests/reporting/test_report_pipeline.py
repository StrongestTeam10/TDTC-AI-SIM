"""외부 API 호출 없이 시뮬레이션 결과로 DOCX 보고서와 분석 JSON이 생성되는지 검증한다."""

from pathlib import Path

from app.db.report_adapter import build_report_request
from app.reporting.service import ReportService
from app.schemas.report_db_models import DbReportBundle


class StubEvidenceProvider:
    """외부 RAG 호출 없이 빈 근거 목록을 반환한다."""

    def __init__(self, vector_index_path: Path):
        self.vector_index_path = vector_index_path

    def retrieve(self, request, limit: int = 5):
        return []

    def status(self) -> dict:
        return {
            "configured_mode": "stub",
            "last_mode": "stub",
            "last_error": None,
            "vector_index_exists": False,
            "indexed_chunk_count": 0,
        }


def test_report_pipeline_creates_docx_and_analysis(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv(
        "NARRATIVE_MODE",
        "template",
    )

    monkeypatch.setattr(
        "app.reporting.service.EvidenceProvider",
        StubEvidenceProvider,
    )

    root = Path(__file__).resolve().parents[2]
    mock_path = (
        root
        / "data"
        / "db"
        / "night_market.json"
    )

    bundle = DbReportBundle.model_validate_json(
        mock_path.read_text(encoding="utf-8")
    )
    request = build_report_request(bundle)

    service = ReportService(
        root=root,
        vector_index_path=(
            tmp_path
            / "unused-vector-index.json"
        ),
    )

    paths = service.generate(
        request,
        tmp_path / request.report_id,
    )

    docx_path = Path(paths["docx"])
    analysis_path = Path(paths["analysis"])

    assert docx_path.exists()
    assert docx_path.stat().st_size > 0
    assert analysis_path.exists()
    assert analysis_path.stat().st_size > 0