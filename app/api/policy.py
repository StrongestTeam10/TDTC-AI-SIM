from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import logging

from app.schemas.policy_schema import PolicyExtractionResult
from app.services.policy_service import analyze_policy_text

router = APIRouter()
logger = logging.getLogger(__name__)

class PolicyAnalyzeRequest(BaseModel):
    policyText: str

@router.post("/analyze", response_model=PolicyExtractionResult)
async def analyze_policy(request: PolicyAnalyzeRequest):
    """
    정부 공문(텍스트)을 분석하여 시뮬레이션 설정(Pydantic 스키마)으로 자동 변환합니다.
    """
    if not request.policyText.strip():
        raise HTTPException(status_code=400, detail="정책안 텍스트가 비어 있습니다.")
        
    try:
        result = await analyze_policy_text(request.policyText)

        #변환결과 확인
        logger.info(f"최종 변환 결과: {result.model_dump_json(indent=2)}")
        
        return result
    except Exception as e:
        logger.error(f"LLM Policy Analysis Error: {e}")
        raise HTTPException(status_code=500, detail=f"LLM 분석 중 오류가 발생했습니다: {str(e)}")
