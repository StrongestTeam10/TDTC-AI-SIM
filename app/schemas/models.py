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
    """
    사용자가 지도(구역) 위에 배치한 오브젝트 하나. BE PlacedObjectDto와 1:1 매칭.

    2026-07-25 추가: 정밀 좌표 데이터가 없으므로 zoneId 단위로 배치하고,
    해당 구역의 대표점(polygon.representative_point())에 물리적 효과를 준다.
    """

    objectType: str = Field(..., description="food_truck | obstacle | event_zone | rest_area")
    zoneId: int
    intensity: float = Field(0.5, ge=0.0, le=1.0)
    latitude: float | None = Field(None, description="지도에서 정밀 배치 시 위도")
    longitude: float | None = Field(None, description="지도에서 정밀 배치 시 경도")


class CorridorPolicy(BaseModel):
    """구역 간 통로에 대한 정책. BE CorridorPolicyDto와 1:1 매칭."""

    fromZoneId: int
    toZoneId: int
    action: str = Field(..., description="close | open | one_way")
    allowedDirection: str | None = Field(
        None, description="one_way일 때만 사용. from_to | to_from"
    )


class EventTrigger(BaseModel):
    """
    2026-07-25 추가: 화재/음향 이상 이벤트를 지도 클릭으로 배치. BE EventTriggerDto와 1:1 매칭.

    fire: 해당 구역의 위험도를 강제로 끌어올려(75 + 25*intensity) 그 구역 사람들이
        계속 대피 상태를 유지하게 한다(지속 효과).
    acoustic_anomaly: 발생 지점 반경(5 + 15*intensity m) 안의 사람들을 그 순간
        한 번만 강제로 대피시킨다(즉발성, 밀집도와 무관).

    2026-07-29 추가: triggerStep. 이 이벤트가 실제로 발동하는 스텝 번호(1부터
    시작). 기본값 1이면 예전처럼 시뮬레이션 시작 시점에 바로 발동한다.
    """

    eventType: str = Field(..., description="fire | acoustic_anomaly")
    zoneId: int
    intensity: float = Field(0.5, ge=0.0, le=1.0)
    latitude: float | None = Field(None, description="지도에서 정밀 배치 시 위도")
    longitude: float | None = Field(None, description="지도에서 정밀 배치 시 경도")
    triggerStep: int = Field(1, ge=1, description="이 이벤트가 발동하는 스텝 번호(1부터 시작)")

class ScenarioRequest(BaseModel):
    """파이프라인 B: 사용자 지정 시나리오 요청.

    2026-07-25: scenarioType/eventZoneId/eventIntensity를 삭제하고 events로 대체했다.
    예전 필드들은 프론트에 입력창만 있었을 뿐 실제로는 어디서도 읽히지 않는
    죽은 필드였다(화재/음향 이벤트가 구현되지 않았었음). 이제 오브젝트 배치와
    같은 방식(지도 클릭 -> zoneId + 위경도 + intensity)으로 실제 효과를 낸다.
    """

    marketId: int
    agentCount: int = Field(..., ge=1, le=100_000)
    steps: int = Field(50, ge=1, le=1000)
    objects: list[PlacedObject] = Field(default_factory=list)
    corridorPolicies: list[CorridorPolicy] = Field(default_factory=list)
    events: list[EventTrigger] = Field(default_factory=list)
    closedGateIds: list[int] = Field(default_factory=list)


class RiskBreakdown(BaseModel):
    """2026-07-23: 레이더(flow)/음향(acoustic) 지표를 완전히 제거해 density/bottleneck만 남김."""

    density: float
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
    agentType: str = "PASS_THROUGH"
    actionState: str = "MOVING"

class PoiState(BaseModel):
    name: str
    zoneId: int
    x: float
    y: float
    latitude: float
    longitude: float


class SnapshotResponse(BaseModel):
    marketId: int
    marketName: str
    mode: str
    step: int
    overallRiskScore: float
    zones: list[ZoneResult]
    agents: list[AgentState] = []
    pois: list[PoiState] = []
    persistedRiskRows: int = 0


class ContributingFactors(BaseModel):
    """BE RiskScoreDto.ContributingFactors와 1:1 매칭.

    2026-07-23: 레이더/음향 센서 완전 제거에 따라 acoustic/flowRate 필드를 삭제하고
    density/bottleneck만 남김. ⚠️ 파이프라인 B(BE ScenarioResultDto.finalRiskScore) 쪽
    Java DTO도 이 변경에 맞춰야 함 - 담당자 공유 필요.
    """

    density: float
    bottleneck: float


class RiskScoreDto(BaseModel):
    """BE RiskScoreDto와 1:1 매칭."""

    timestamp: datetime
    score: float
    level: str
    contributingFactors: ContributingFactors


class ScenarioResult(BaseModel):
    """
    파이프라인 B 응답. BE ScenarioResultDto와 1:1 매칭.
    """

    scenarioId: str
    requestedAt: datetime
    frames: list[list[AgentState]]
    pois: list[PoiState] = []
    evacuationTimeSeconds: int | None
    finalRiskScore: RiskScoreDto
    averageDensity: float
    maxDensity: float
    maxDensityZoneId: int | None = None
    maxDensityZoneName: str | None = None
    evacuatedCount: int = 0


class PredictRequest(BaseModel):
    """
    2026-07-24 추가: 실측 상태에서 출발한 예측 시뮬레이션 요청.

    파이프라인 B(ScenarioRequest)와 달리 화재 등 외부 충격 이벤트를 다루지 않는다.
    실제 관측된 인원 배치를 초기 상태로 삼아, 매대(오브젝트) 매력도 기반 자연스러운
    이동과 게이트를 통한 신규 유입만으로 "인구가 몰렸을 때" 위험도가 어떻게
    전개되는지를 본다.
    """

    marketId: int
    capturedAt: datetime | None = Field(
        None, description="예측의 출발점이 되는 실측 시점. 미지정 시 최신 관측값 사용"
    )
    steps: int = Field(30, ge=1, le=1000)
    totalInflow: int = Field(
        0, ge=0, le=100_000,
        description=(
            "전체 시뮬레이션 동안 게이트로 유입될 총 인원수. 스텝마다 무작위 인원이 "
            "유입되고 합계가 이 값에 맞춰짐(스텝당 고정 인원이 아님). 0이면 신규 유입 없음"
        ),
    )
    # 2026-08-XX 추가: 이벤트(화재/음향이상)만 Before/After 양쪽에 동일하게 배치
    # 가능해야 해서 여기도 받는다. 오브젝트/통로정책/게이트는 여전히 ScenarioRequest 전용.
    events: list[EventTrigger] = Field(default_factory=list)
    seed: int | None = None


class ZoneRiskPoint(BaseModel):
    """예측 결과의 스텝별 구역 위험도 (그래프용, ZoneResult보다 가벼운 요약)."""

    zoneId: int
    riskScore: float
    riskLevel: str


class RiskTrendPoint(BaseModel):
    step: int
    overallRiskScore: float
    zones: list[ZoneRiskPoint]


class PredictResult(BaseModel):
    """예측 시뮬레이션 응답."""

    predictionId: str
    requestedAt: datetime
    frames: list[list[AgentState]]
    pois: list[PoiState] = []
    riskTrend: list[RiskTrendPoint]
    finalOverallRiskScore: float
    agentCount: int
    averageDensity: float
    maxDensity: float
    maxDensityZoneId: int | None = None
    maxDensityZoneName: str | None = None
    evacuatedCount: int = 0