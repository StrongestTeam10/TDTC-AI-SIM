"""OpenAI Vector 검색이 정책 시나리오와 관련된 공공문서 근거 청크를 반환하는지 검증한다."""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.db.report_adapter import build_report_request
from app.reporting.evidence import EvidenceProvider
from app.schemas.report_db_models import DbReportBundle


ROOT = Path(__file__).resolve().parents[2]
VECTOR_INDEX_PATH = (
    ROOT
    / "knowledge"
    / "vector_index.json"
)

load_dotenv(ROOT / ".env")

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY", "").strip(),
    reason=(
        "OPENAI_API_KEY가 없어 실제 OpenAI RAG "
        "통합 테스트를 건너뜁니다."
    ),
)
def test_openai_vector_retrieval_returns_pdf_chunks():
    """기준안·대안·시계열 데이터가 올바르게 연결되는지 확인한다."""

    assert VECTOR_INDEX_PATH.exists(), (
        "벡터 인덱스가 없습니다. "
        "python scripts/build_rag_index.py를 먼저 실행하세요."
    )

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

    provider = EvidenceProvider(
        VECTOR_INDEX_PATH
    )

    items = provider.retrieve(
        request,
        limit=5,
    )

    assert 1 <= len(items) <= 5

    for item in items:
        assert item.title
        assert item.excerpt
        assert len(item.excerpt) > 20

        # 보고서 내용과 무관한 행정문서가
        # 검색되지 않는지 확인한다.
        assert "행정업무운영" not in item.title
        assert "공문서 쓰기" not in item.title

        # PDF 수식이 깨진 청크가
        # 검색 결과에 포함되지 않는지 확인한다.
        assert "𝑪𝒊" not in item.excerpt

        assert any(
            tag.startswith("vector_score:")
            for tag in item.relevance_tags
        )

    status = provider.status()

    assert status["last_mode"] == "openai_vector"
    assert status["last_error"] is None
    assert status["vector_index_exists"] is True
    assert status["indexed_chunk_count"] > 0