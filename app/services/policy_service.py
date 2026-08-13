import os
import json
from google import genai
from google.genai import types
from app.schemas.policy_schema import PolicyExtractionResult
from fastapi import UploadFile, HTTPException

async def analyze_policy_multimodal(policy_text: str, file: UploadFile | None) -> PolicyExtractionResult:
    """
    공문 텍스트 및 파일을 LLM으로 분석하여 시뮬레이션 설정(Pydantic 스키마)으로 변환합니다.
    """
    # API 키 설정
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    # Pydantic 스키마를 JSON Schema 형태로 추출
    schema_json = PolicyExtractionResult.model_json_schema()
    
    prompt = f"""
    너는 재난 대피 시뮬레이션 파라미터 변환기야.
    아래에 제공된 '공문 텍스트'와 '첨부 문서'를 읽고, 시뮬레이션에 적용할 제약 조건들을 추출해줘.
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
    
    contents = [prompt]
    
    if file:
        file_bytes = await file.read()
        filename = file.filename.lower()
        
        if filename.endswith(".pdf"):
            contents.append(types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"))
        elif filename.endswith((".png", ".jpg", ".jpeg")):
            mime_type = "image/png" if filename.endswith(".png") else "image/jpeg"
            contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))
        elif filename.endswith(".docx"):
            try:
                import docx
                import io
                doc = docx.Document(io.BytesIO(file_bytes))
                docx_text = "\n".join([para.text for para in doc.paragraphs])
                contents.append(f"\n[첨부문서(DOCX) 내용]\n{docx_text}")
            except Exception as e:
                raise HTTPException(status_code=400, detail="DOCX 문서를 읽을 수 없거나 손상되었습니다.")
        else:
            raise HTTPException(status_code=400, detail="지원하지 않는 파일 형식입니다. (지원: PDF, DOCX, PNG, JPG)")
            
    response = await client.aio.models.generate_content(
        model=os.getenv("GEMINI_MODEL"),
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json"
        )
    )
    
    # LLM이 뱉은 JSON 텍스트를 dict로 파싱
    result_dict = json.loads(response.text)
    
    # Pydantic 객체로 변환하여 리턴 (검증 포함)
    return PolicyExtractionResult(**result_dict)
