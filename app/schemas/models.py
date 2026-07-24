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


class ScenarioRequest(BaseModel):
    """파이프라인 B: 사용자 지정 시나리오 요청."""

    marketId: int
    agentCount: int = Field(..., ge=1, le=100_000)
    scenarioType: str = Field("none", description="none | fire | acoustic_anomaly | corridor_block")
    eventZoneId: int | None = None
    eventIntensity: float = Field(0.5, ge=0.0, le=1.0)
    steps: int = Field(50, ge=1, le=1000)


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
    riskTrend: list[RiskTrendPoint]
    finalOverallRiskScore: float
