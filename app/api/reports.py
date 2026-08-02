"""Spring Boot가 호출할 보고서 생성·조회 API를 제공한다."""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.db.report_adapter import build_report_request
from app.reporting.service import ReportService
from app.schemas.report_db_models import DbReportBundle


router = APIRouter(prefix="/simulation/reports", tags=["simulation-reports"])

# ReportMeta.report_id의 제약과 동일하게 유지할 것. 한쪽만 느슨해지면 그 경로로 뚫린다.
REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
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
    """보고서를 생성한 직후 DOCX 파일로 응답한다.

    본문이 파일이라 제목을 실을 곳이 없어 헤더로 함께 내려준다. 호출자(BE)가
    목록 표시용으로 저장하며, 이래야 목록에 뜨는 제목과 문서 표지 제목이 일치한다.
    HTTP 헤더는 ASCII만 담을 수 있으므로 퍼센트 인코딩한다(수신 측에서 UTF-8로 디코딩).
    """

    try:
        report_id, paths = _generate(bundle)
        return FileResponse(
            paths["docx"],
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            filename=f"{report_id}.docx",
            headers={
                "X-Report-Id": report_id,
                "X-Report-Title": quote(paths["title"]),
            },
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


def _safe_report_id(report_id: str) -> str:
    """경로에 쓰기 안전한 report_id인지 확인한다.

    이 값이 폴더명·파일명으로 그대로 들어가므로, ".."나 절대경로가 섞이면
    REPORT_OUTPUT_DIR 밖의 파일을 읽게 된다(Path 결합에서 절대경로는 앞부분을 덮어쓴다).
    생성 경로는 ReportMeta.report_id의 pattern이 막지만, 이 조회 경로는 스키마를
    거치지 않으므로 여기서 따로 검사한다.
    """

    if not REPORT_ID_PATTERN.fullmatch(report_id):
        raise HTTPException(status_code=400, detail="report_id 형식이 올바르지 않습니다.")
    return report_id


@router.get("/{report_id}/docx")
def download_docx(report_id: str) -> FileResponse:
    """생성된 DOCX 보고서를 내려준다."""

    report_id = _safe_report_id(report_id)
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

    report_id = _safe_report_id(report_id)
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
