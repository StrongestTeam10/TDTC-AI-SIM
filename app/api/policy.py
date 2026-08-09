from fastapi import APIRouter, HTTPException, Form, File, UploadFile
from typing import Optional
import logging

from app.schemas.policy_schema import PolicyExtractionResult
from app.services.policy_service import analyze_policy_multimodal

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/analyze", response_model=PolicyExtractionResult)
async def analyze_policy(
    policyText: str = Form(default=""),
    file: Optional[UploadFile] = File(None)
):
    """
    정부 공문(텍스트) 및 파일(PDF/이미지/DOCX)을 분석하여 시뮬레이션 설정으로 변환합니다.
    """
    if not policyText.strip() and file is None:
        raise HTTPException(status_code=400, detail="텍스트나 파일 중 하나는 반드시 제공되어야 합니다.")
        
    try:
        result = await analyze_policy_multimodal(policyText, file)

        #변환결과 확인
        logger.info(f"최종 변환 결과: {result.model_dump_json(indent=2)}")
        
        return result
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"LLM Policy Analysis Error: {e}")
        raise HTTPException(status_code=500, detail=f"LLM 분석 중 오류가 발생했습니다: {str(e)}")
