"""RAG 검색부터 DOCX 생성까지 보고서 파이프라인의 실행 순서를 조율한다."""

from __future__ import annotations

import json
from pathlib import Path

from .analytics import assess_request
from .charting import create_charts
from .docx_renderer import DocxReportRenderer
from .evidence import EvidenceProvider
from .narrative import NarrativeGenerator
from app.schemas.report_models import ReportRequest


class ReportService:
    def __init__(
        self,
        root: Path,
        vector_index_path: Path | None = None,
    ):
        """보고서 생성에 필요한 검색기·생성기·렌더러를 구성한다."""

        self.root = root
        self.evidence_provider = EvidenceProvider(
            vector_index_path
            or root / "knowledge" / "vector_index.json"
        )
        self.narrative_generator = NarrativeGenerator()
        self.docx_renderer = DocxReportRenderer()

    def generate(
        self,
        request: ReportRequest,
        output_dir: Path,
    ) -> dict[str, str]:
        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )
        assets_dir = output_dir / "assets"

        evidence = self.evidence_provider.retrieve(
            request,
            limit=5,
        )
        assessments = assess_request(request)
        narrative = self.narrative_generator.generate(
            request=request,
            assessments=assessments,
            evidence=evidence,
        )
        charts = create_charts(
            request,
            assets_dir,
        )

        docx_path = output_dir / f"{request.report_id}.docx"
        self.docx_renderer.render(
            request=request,
            narrative=narrative,
            evidence=evidence,
            charts=charts,
            output_path=docx_path,
        )

        analysis_path = (
            output_dir
            / f"{request.report_id}_analysis.json"
        )
        analysis_payload = {
            "report_id": request.report_id,
            "change_id": request.change_id,
            "report_title": narrative.report_title,
            "report_title_generated_by_llm": (
                request.generate_report_title
                and self.narrative_generator.last_mode
                == "openai"
            ),
            "metric_comparisons": [
                item.to_dict()
                for item in assessments
            ],
            "evidence": [
                item.model_dump(mode="json")
                for item in evidence
            ],
            "rag": self.evidence_provider.status(),
            "narrative": self.narrative_generator.status(),
            "flow_analysis": [
                {
                    "alternative_id": item.alternative_id,
                    "alternative_name": item.alternative_name,
                    "analysis": item.analysis,
                }
                for item in narrative.flow_analysis
            ],
            "evidence_notes": [
                {
                    "source_id": item.source_id,
                    "title": item.title,
                    "page": item.page,
                    "summary": item.summary,
                }
                for item in narrative.evidence_notes
            ],
            "charts": charts,
        }
        analysis_path.write_text(
            json.dumps(
                analysis_payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "docx": str(docx_path),
            "analysis": str(analysis_path),
        }
