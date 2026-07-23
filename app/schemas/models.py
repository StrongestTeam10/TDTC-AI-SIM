"""API 요청/응답 스키마. Spring Boot DTO와 필드명(camelCase)을 일치시킨다."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SnapshotRequest(BaseModel):
    """파이프라인 A: 실측 기반 관제 스냅샷 요청."""

    marketId: int = Field(..., description="시장 ID")
    capturedAt: datetime | None = Field(
        None, description="조회 시점. 미지정 시 각 구역의 최신 관측값 사용"
    )
    persistRisk: bool = Field(
        False, description="산출된 위험도를 mrkrisk01m에 기록할지 여부"
    )
    includeAgents: bool = Field(
        True, description="개별 에이전트 좌표 포함 여부 (인원이 많으면 응답이 커짐)"
    )


class PlacedObject(BaseModel):
    """지도 위에 사용자가 배치한 오브젝트 하나 (구역 단위)."""

    objectType: str = Field(
        ..., description="food_truck | obstacle | event_zone | rest_area"
    )
    zoneId: int
    intensity: float = Field(
        0.5, ge=0.0, le=1.0, description="효과 강도. 오브젝트 종류별로 의미가 다름"
    )


class CorridorPolicy(BaseModel):
    """통로(구역 간 연결)에 대한 정책. 구역 하나가 아니라 두 구역 사이를 가리킨다."""

    fromZoneId: int
    toZoneId: int
    action: str = Field(..., description="close | open | one_way")
    allowedDirection: str | None = Field(
        None, description="action이 one_way일 때만 사용: from_to | to_from"
    )


class ScenarioRequest(BaseModel):
    """파이프라인 B: 사용자 지정 시나리오 요청."""

    marketId: int
    agentCount: int = Field(..., ge=1, le=100_000)
    scenarioType: str = Field(
        "none", description="none | fire | acoustic_anomaly | corridor_block"
    )
    eventZoneId: int | None = None
    eventIntensity: float = Field(0.5, ge=0.0, le=1.0)
    steps: int = Field(50, ge=1, le=1000)
    objects: list[PlacedObject] = Field(
        default_factory=list, description="배치한 오브젝트(푸드트럭/장애물/행사존/휴게공간) 목록"
    )
    corridorPolicies: list[CorridorPolicy] = Field(
        default_factory=list, description="통로 폐쇄/개방/일방통행 정책 목록"
    )


class RiskBreakdown(BaseModel):
    density: float
    flow: float
    acoustic: float
    bottleneck: float


class ZoneResult(BaseModel):
    zoneId: int
    zoneName: str
    areaM2: float
    pathWidthM: float
    visitorCount: int
    density: float
    personalSpace: float
    riskScore: float
    riskLevel: str
    reason: str
    breakdown: RiskBreakdown


class AgentState(BaseModel):
    agentId: int
    zoneId: int
    x: float
    y: float
    latitude: float
    longitude: float
    state: str


class SnapshotResponse(BaseModel):
    marketId: int
    marketName: str
    mode: str
    step: int
    overallRiskScore: float
    zones: list[ZoneResult]
    agents: list[AgentState] = []
    persistedRiskRows: int = 0


class ContributingFactors(BaseModel):
    """BE RiskScoreDto.ContributingFactors와 1:1 매칭 (필드명 flowRate 주의 - flow 아님)."""

    density: float
    acoustic: float
    flowRate: float


class RiskScoreDto(BaseModel):
    """BE RiskScoreDto와 1:1 매칭."""

    timestamp: datetime
    score: float
    level: str
    contributingFactors: ContributingFactors


class ScenarioResult(BaseModel):
    """
    파이프라인 B 응답. BE ScenarioResultDto와 1:1 매칭.

    frames: 스텝별 전체 에이전트 상태 (프론트 애니메이션 재생용).
    evacuationTimeSeconds: 위험으로 대피를 시작한 에이전트 전원이 출구 구역에
        도달하는 데 걸린 시간. 대피가 발생하지 않았거나 요청한 steps 내에
        완료되지 못하면 None.
    """

    scenarioId: str
    requestedAt: datetime
    frames: list[list[AgentState]]
    evacuationTimeSeconds: int | None
    finalRiskScore: RiskScoreDto
