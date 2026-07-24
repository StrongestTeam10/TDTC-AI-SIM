"""Spring Boot가 호출할 보고서 생성·조회 API를 제공한다."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.db.report_adapter import build_report_request
from app.reporting.service import ReportService
from app.schemas.report_db_models import DbReportBundle


router = APIRouter(prefix="/simulation/reports", tags=["simulation-reports"])
service = ReportService(
    root=settings.project_root,
    vector_index_path=settings.vector_index_path,
)


def _generate(bundle: DbReportBundle) -> tuple[str, dict[str, str]]:
    """ERD 조회 묶음을 내부 모델로 바꾼 뒤 보고서를 생성한다."""

    request = build_report_request(bundle)
    paths = service.generate(
        request,
        settings.output_dir / request.report_id,
    )
    return request.report_id, paths


@router.post("")
def generate_report(bundle: DbReportBundle) -> dict[str, str]:
    """보고서를 생성하고 다운로드 가능한 API 경로를 반환한다."""

    try:
        report_id, _ = _generate(bundle)
        return {
            "report_id": report_id,
            "status": "COMPLETED",
            "download_url": f"/simulation/reports/{report_id}/docx",
            "analysis_url": f"/simulation/reports/{report_id}/analysis",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/file")
def generate_report_file(bundle: DbReportBundle) -> FileResponse:
    """보고서를 생성한 직후 DOCX 파일로 응답한다."""

    try:
        report_id, paths = _generate(bundle)
        return FileResponse(
            paths["docx"],
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            filename=f"{report_id}.docx",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/mock/{mock_name}")
def generate_mock_report(mock_name: str) -> dict[str, str]:
    """로컬 ERD Mock JSON으로 보고서 생성 흐름을 실행한다."""

    path = settings.project_root / "data" / "db" / f"{mock_name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Mock 파일이 없습니다.")
    bundle = DbReportBundle.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    return generate_report(bundle)


@router.get("/{report_id}/docx")
def download_docx(report_id: str) -> FileResponse:
    """생성된 DOCX 보고서를 내려준다."""

    path = settings.output_dir / report_id / f"{report_id}.docx"
    if not path.exists():
        raise HTTPException(status_code=404, detail="보고서가 없습니다.")
    return FileResponse(
        path,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        filename=path.name,
    )


@router.get("/{report_id}/analysis")
def download_analysis(report_id: str) -> FileResponse:
    """보고서 생성 근거와 지표 비교가 담긴 JSON을 내려준다."""

    path = settings.output_dir / report_id / f"{report_id}_analysis.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="분석 JSON이 없습니다.")
    return FileResponse(
        path,
        media_type="application/json",
        filename=path.name,
    )


@router.get("/status")
def report_status() -> dict:
    """보고서 검색기와 본문 생성기의 실행 상태를 반환한다."""

    return {
        "retrieval": service.evidence_provider.status(),
        "generation": service.narrative_generator.status(),
    }
