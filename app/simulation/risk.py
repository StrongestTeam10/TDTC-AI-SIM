"""
위험도 스코어링.

임의 가중치가 아니라 공인된 인파 안전 기준에 근거한다.

근거 자료
---------
1) 행정안전부「다중운집인파사고 안전관리 가이드라인」(2024.9)
   - 서서 관람하는 구역 기준 1인당 0.46 m^2 할당 권장 (미국 NFPA 101 준용)
   - 0.46 m^2/인 = 약 2.17 명/m^2 가 수용 한계선에 해당

2) G. Keith Still (군중안전 전문가) 밀집도 임계 기준
   - 1 m^2 당 5명부터 위험 구간으로 분류
   - 10.29 이태원 참사 당시 밀집도는 1 m^2 당 약 5.6~6.6명으로 추정

3) Fruin's Level of Service (보행자 서비스 수준, 국제 표준)
   - LOS A~C: 1인당 1.39 m^2 이상, 자유로운 보행 가능
   - LOS D~E: 0.46~1.39 m^2, 보행 속도 저하 및 신체 접촉 발생
   - LOS F  : 0.46 m^2 미만, 군중 유동(crowd crush) 위험

주의: 아래 임계값은 위 기준을 시뮬레이션용으로 단순화한 것이며,
      실제 관제 적용 시 대상 시장의 특성에 맞춘 캘리브레이션이 필요하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ---- 밀집도 임계값 (명/m^2) ----
DENSITY_COMFORTABLE = 0.72   # Fruin LOS C 하한 (1.39 m^2/인)
DENSITY_CAPACITY = 2.17      # 행안부/NFPA 수용 한계 (0.46 m^2/인)
DENSITY_DANGER = 4.0         # 위험 진입 구간
DENSITY_CRITICAL = 5.0       # Still 기준 압사 위험 구간

# ---- 통로 유동 용량 ----
FLOW_PER_METER_PER_SEC = 1.3   # 통로 폭 1m 당 초당 통과 가능 인원 (Fruin/SFPE 통상 설계값)
EGRESS_TIME_CRITICAL_SEC = 300.0  # 대피 소요 5분 이상이면 병목 최고 위험

# ---- 종합 점수 가중치 ----
# 밀집도가 압사 위험의 직접 원인이므로 지배적 비중을 둔다.
# 2026-07-23: 레이더(이동 흐름)/음향 센서를 완전히 제거하기로 결정하면서
# WEIGHT_FLOW(0.20)/WEIGHT_ACOUSTIC(0.15)도 함께 삭제했다. 남은 두 지표(밀집도/병목)의
# 가중치 값 자체는 그대로 두고, 기존에 있던 "결측 지표 제외 후 재정규화" 로직을 그대로
# 재사용해 두 지표만으로 100%를 채우도록 했다 (밀집도 0.55 : 병목 0.10 비율 유지,
# 실질 반영 비율은 84.6% : 15.4%).
WEIGHT_DENSITY = 0.55
WEIGHT_BOTTLENECK = 0.10  # 통로 폭 대비 부하


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class RiskAssessment:
    """구역 단위 위험도 평가 결과."""

    zone_id: int
    score: float               # 0 ~ 100
    level: RiskLevel
    density: float             # 명/m^2
    personal_space: float      # m^2/명
    density_score: float
    bottleneck_score: float
    reason: str


def density_to_score(density: float) -> float:
    """
    밀집도(명/m^2)를 0~100 점수로 변환.

    구간별 선형 보간을 사용해, 임계값이 점수 경계와 정확히 맞도록 한다.
      0            -> 0
      2.17(수용한계) -> 50
      4.0(위험)     -> 75
      5.0(심각)     -> 90
      6.6 이상      -> 100  (이태원 참사 추정 상단치)
    """
    if density <= 0:
        return 0.0
    if density <= DENSITY_CAPACITY:
        return 50.0 * (density / DENSITY_CAPACITY)
    if density <= DENSITY_DANGER:
        ratio = (density - DENSITY_CAPACITY) / (DENSITY_DANGER - DENSITY_CAPACITY)
        return 50.0 + 25.0 * ratio
    if density <= DENSITY_CRITICAL:
        ratio = (density - DENSITY_DANGER) / (DENSITY_CRITICAL - DENSITY_DANGER)
        return 75.0 + 15.0 * ratio
    ratio = min((density - DENSITY_CRITICAL) / (6.6 - DENSITY_CRITICAL), 1.0)
    return 90.0 + 10.0 * ratio


def bottleneck_to_score(
    visitor_count: int,
    path_width_m: float | None,
) -> float:
    """
    통로 병목 위험을 '대피 소요 시간'으로 환산해 점수화한다.

    보행 통로의 실용 유동 용량은 통상 폭 1 m 당 초당 약 1.3명으로 알려져 있다
    (Fruin / SFPE 보행자 유동 이론의 통상 설계값).
    해당 구역 인원 전체가 그 통로를 빠져나가는 데 걸리는 시간을 계산해,
    5분(300초) 이상이면 최고점으로 본다.

    밀집도와 달리 '구역이 좁아서 빠져나가지 못하는' 구조적 위험을 잡아낸다.
    """
    if not path_width_m or path_width_m <= 0 or visitor_count <= 0:
        return 0.0
    flow_capacity = FLOW_PER_METER_PER_SEC * path_width_m  # 명/초
    egress_seconds = visitor_count / flow_capacity
    return min(100.0, 100.0 * egress_seconds / EGRESS_TIME_CRITICAL_SEC)


def score_to_level(score: float) -> RiskLevel:
    """종합 점수를 4단계 등급으로 변환. 경계값은 밀집도 임계와 정합되도록 설정."""
    if score >= 75.0:
        return RiskLevel.CRITICAL
    if score >= 50.0:
        return RiskLevel.HIGH
    if score >= 25.0:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _build_reason(density: float, personal_space: float, level: RiskLevel) -> str:
    if level is RiskLevel.CRITICAL:
        return (
            f"밀집도 {density:.2f}명/m^2 (1인당 {personal_space:.2f}m^2). "
            f"압사 위험 임계({DENSITY_CRITICAL}명/m^2) 도달 수준으로 즉시 통제 필요."
        )
    if level is RiskLevel.HIGH:
        return (
            f"밀집도 {density:.2f}명/m^2 (1인당 {personal_space:.2f}m^2). "
            f"수용 한계({DENSITY_CAPACITY}명/m^2) 초과, 인파 유입 통제 검토 필요."
        )
    if level is RiskLevel.MEDIUM:
        return f"밀집도 {density:.2f}명/m^2. 혼잡 진행 중이나 통제 가능 범위."
    return f"밀집도 {density:.2f}명/m^2. 원활한 보행 가능."


def assess_zone(
    zone_id: int,
    visitor_count: int,
    area_m2: float,
    path_width_m: float | None = None,
) -> RiskAssessment:
    """구역 하나의 종합 위험도를 산출한다."""
    density = visitor_count / area_m2 if area_m2 > 0 else 0.0
    personal_space = area_m2 / visitor_count if visitor_count > 0 else float("inf")

    d_score = density_to_score(density)
    b_score = bottleneck_to_score(visitor_count, path_width_m)

    # 가중치 재정규화:
    # 통로 폭 데이터가 없으면 병목 지표를 제외하고 밀집도 100%로 계산한다.
    # (레이더/음향 지표는 2026-07-23부로 완전히 제거되어 애초에 계산 대상이 아님)
    components: list[tuple[float, float]] = [(d_score, WEIGHT_DENSITY)]
    if path_width_m:
        components.append((b_score, WEIGHT_BOTTLENECK))

    weight_sum = sum(w for _, w in components)
    total = sum(score * w for score, w in components) / weight_sum if weight_sum else 0.0
    total = round(min(100.0, max(0.0, total)), 2)

    level = score_to_level(total)

    # 안전 오버라이드:
    # 밀집도 단독으로 임계를 넘으면 다른 지표와 무관하게 등급을 강제 상향한다.
    # 압사는 밀집도만으로도 발생하므로, 종합 점수 평균에 희석되어선 안 된다.
    if density >= DENSITY_CRITICAL:
        level = RiskLevel.CRITICAL
    elif density >= DENSITY_CAPACITY and level in (RiskLevel.LOW, RiskLevel.MEDIUM):
        level = RiskLevel.HIGH

    return RiskAssessment(
        zone_id=zone_id,
        score=total,
        level=level,
        density=round(density, 4),
        personal_space=round(personal_space, 4) if visitor_count > 0 else -1.0,
        density_score=round(d_score, 2),
        bottleneck_score=round(b_score, 2),
        reason=_build_reason(density, personal_space, level),
    )
