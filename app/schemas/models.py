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
    2026-07-25 추가: 화재 이벤트를 지도 클릭으로 배치. BE EventTriggerDto와 1:1 매칭.

    fire: 해당 구역의 위험도를 강제로 끌어올려(75 + 25*intensity) 그 구역 사람들이
        계속 대피 상태를 유지하게 한다(지속 효과).

    2026-07-29 추가: triggerStep. 이 이벤트가 실제로 발동하는 스텝 번호(1부터
    시작). 기본값 1이면 예전처럼 시뮬레이션 시작 시점에 바로 발동한다.

    2026-08-XX 변경: 음향 이상(acoustic_anomaly) 이벤트 완전 제거. 이제
    eventType은 "fire" 하나뿐이다.
    """

    eventType: str = Field(..., description="fire")
    zoneId: int
    intensity: float = Field(0.5, ge=0.0, le=1.0)
    latitude: float | None = Field(None, description="지도에서 정밀 배치 시 위도")
    longitude: float | None = Field(None, description="지도에서 정밀 배치 시 경도")
    triggerStep: int = Field(1, ge=1, description="이 이벤트가 발동(발화)하는 스텝 번호(1부터 시작)")
    # 2026-08-XX 추가: 화재 생애주기. triggerStep에 발화 -> burnSteps 동안 연소
    # (위험도 최대 유지) -> 진압 -> recoverySteps 동안 위험도가 서서히 0으로
    # 감쇠(복구) -> 복구 완료 후 정상 상태로 돌아가고 유동인구 재유입이 재개된다.
    burnSteps: int = Field(
        18, ge=1, description="발화 후 이 스텝 수만큼 연소(위험도 최대 유지). 기본 18(약 3분)"
    )
    recoverySteps: int = Field(
        12, ge=0, description="진압 후 위험도가 0으로 감쇠하며 복구되는 스텝 수. 기본 12(약 2분)"
    )

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
    # 2026-08-XX 추가: 이 에이전트가 현재 있는 구역의 위험도 점수(0~100).
    # FE에서 위험도에 따라 에이전트 색을 다르게(빨강/노랑/파랑) 칠하는 데 쓴다.
    # 화재 발생 구역이 가장 높고, 확산 감쇠에 따라 주변이 낮아진다.
    dangerLevel: float = 0.0

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
    # 2026-08-XX 추가: 개입 후 스텝별 위험도 추이(개입 전과 겹쳐 비교용).
    riskTrend: list[RiskTrendPoint] = []
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

    2026-08-XX 변경: 이제 ScenarioRequest(After)와 같은 방식으로 초기
    인원을 구역 면적 비례로 배치한다(자세한 이유는 simulate.py
    simulate_predict()의 docstring 참고).

    2026-08-12 변경: 오브젝트 배치/통로정책도 Before가 받는다. 시장 구조 등록에서
    저장한 "현행"을 개입 전(Before)에도 반영해, Before가 실제 시장 모습이 되도록
    한다. Before/After 차이는 사용자가 After에서 삭제/추가한 개입분에서만 나온다.
    (게이트 폐쇄는 이전부터 Before가 받고 있었다.)
    """

    marketId: int
    capturedAt: datetime | None = Field(
        None,
        description=(
            "(더 이상 초기 배치에 쓰이지 않음 - 하위 호환을 위해 필드만 남겨둠. "
            "2026-08-XX 이전에는 이 시점의 실측 CCTV 관측값을 예측 출발점으로 썼다.)"
        ),
    )
    steps: int = Field(30, ge=1, le=1000)
    totalInflow: int = Field(
        0, ge=0, le=100_000,
        description=(
            "2026-08-XX 변경: 시뮬레이션 시작 시점에 구역 면적 비례로 한 번에 배치할 "
            "총 인원수(ScenarioRequest.agentCount와 동일한 의미). 예전처럼 스텝마다 "
            "게이트로 서서히 유입되지 않는다 - After와 동일한 기준선으로 비교하기 위함."
        ),
    )
    # 2026-08-XX 추가: 이벤트(화재)를 Before/After 양쪽에 동일하게 배치.
    events: list[EventTrigger] = Field(default_factory=list)
    # 2026-08-XX 추가: 게이트 개폐는 개입 전/후 독립적으로 조절 가능해야 해서
    # Before(predict)도 닫힌 게이트 목록을 받는다(FE에서 양쪽 지도 각각 클릭 토글).
    closedGateIds: list[int] = Field(default_factory=list)
    # 2026-08-12 추가: 현행 오브젝트/통로정책(시장 구조 등록에서 저장한 것).
    # ScenarioRequest와 동일하게 apply_scenario_overrides로 반영된다.
    objects: list[PlacedObject] = Field(default_factory=list)
    corridorPolicies: list[CorridorPolicy] = Field(default_factory=list)
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
    # 2026-08-XX 추가: 개입 전 대피 완료 시간(개입 후와 비교용). 대피 없으면 None.
    evacuationTimeSeconds: int | None = None