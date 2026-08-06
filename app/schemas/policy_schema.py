from pydantic import BaseModel, Field
from typing import List, Optional

class PolicyPlacedObject(BaseModel):
    objectType: str = Field(description="장애물/가판대 철거 대상인 경우 'obstacle' 또는 'food_truck'으로 지정")
    zoneId: int = Field(description="철거할 구역의 zoneId. (예: 망원시장 중앙/A구역=1, B구역=2, C구역=3, D구역=4)")
    action: str = Field("remove", description="항상 'remove'로 고정 (시스템에서 해당 구역의 객체 삭제)")

class PolicyCorridorPolicy(BaseModel):
    fromZoneId: int = Field(description="시작 구역 ID")
    toZoneId: int = Field(description="도착 구역 ID")
    action: str = Field(description="close | open | one_way")
    allowedDirection: Optional[str] = Field(None, description="one_way일 때 from_to | to_from")

class PolicyExtractionResult(BaseModel):
    agentCount: Optional[int] = Field(None, description="제한 수용 인원수")
    objectsToRemove: List[PolicyPlacedObject] = Field(default_factory=list, description="철거할 오브젝트/구역 정보")
    corridorPolicies: List[PolicyCorridorPolicy] = Field(default_factory=list, description="일방통행 등 통로 제어 정책")
    closedGateIds: List[int] = Field(default_factory=list, description="폐쇄할 게이트 ID 목록 (예: Gate 1, Gate 2)")
