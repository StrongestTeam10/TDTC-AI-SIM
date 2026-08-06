import os
import json
from google import genai
from google.genai import types
from app.schemas.policy_schema import PolicyExtractionResult

async def analyze_policy_text(policy_text: str) -> PolicyExtractionResult:
    """
    공문 텍스트를 LLM으로 분석하여 시뮬레이션 설정(Pydantic 스키마)으로 변환합니다.
    """
    # API 키 설정
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    # Pydantic 스키마를 JSON Schema 형태로 추출
    schema_json = PolicyExtractionResult.model_json_schema()
    
    prompt = f"""
    너는 재난 대피 시뮬레이션 파라미터 변환기야.
    아래에 제공된 '공문 텍스트'를 읽고, 시뮬레이션에 적용할 제약 조건들을 추출해줘.
    반드시 아래 제공된 JSON Schema 구조에 맞게 완벽한 JSON 형식으로만 응답해야 해.
    
    [JSON Schema]
    {json.dumps(schema_json, ensure_ascii=False, indent=2)}
    
    [추출 가이드]
    - agentCount: 텍스트에 "수용 인원 ~명으로 제한" 등의 문구가 있으면 그 숫자(정수)를 넣을 것.
    - objectsToRemove: 매대, 가판대, 적치물 등을 "철거", "삭제" 하라는 지시가 있으면 추가할 것.
        - 구역 ID(zoneId)는 텍스트 맥락에 따라 유추 (예: A구역/중앙교차로=1, B구역=2)
    - corridorPolicies: "일방통행" 등의 통로 통제가 있으면 추가할 것.
    - closedGateIds: "폐쇄"하라는 게이트 번호가 있으면 정수 리스트로 추출.
    
    [공문 텍스트]
    {policy_text}
    """
    
    response = await client.aio.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json"
        )
    )
    
    # LLM이 뱉은 JSON 텍스트를 dict로 파싱
    result_dict = json.loads(response.text)
    
    # Pydantic 객체로 변환하여 리턴 (검증 포함)
    return PolicyExtractionResult(**result_dict)
