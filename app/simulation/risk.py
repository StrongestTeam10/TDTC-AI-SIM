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
WEIGHT_DENSITY = 0.55
WEIGHT_FLOW = 0.20      # 보행 속도 저하 (레이더 avg_speed 기반)
WEIGHT_ACOUSTIC = 0.15  # 이상 음향 이벤트 (비명/충돌음)
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
    flow_score: float
    acoustic_score: float
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


def flow_to_score(avg_speed_cm_s: float | None) -> float:
    """
    평균 보행 속도(cm/s)를 위험 점수로 변환.

    자유 보행 속도는 통상 130 cm/s 내외이며, 밀집이 심해질수록 급격히 떨어진다.
    속도가 30 cm/s 이하로 떨어지면 군중 유동이 정체된 상태로 본다.
    데이터가 없으면 0점(위험 신호 없음)으로 처리한다.
    """
    if avg_speed_cm_s is None:
        return 0.0
    free_flow = 130.0
    standstill = 30.0
    if avg_speed_cm_s >= free_flow:
        return 0.0
    if avg_speed_cm_s <= standstill:
        return 100.0
    return 100.0 * (free_flow - avg_speed_cm_s) / (free_flow - standstill)


def acoustic_to_score(event_count: int, max_confidence: float | None) -> float:
    """
    이상 음향 이벤트를 위험 점수로 변환.

    단일 이벤트라도 신뢰도가 높으면 즉시 높은 점수를 부여하고,
    이벤트가 반복될수록 가중한다.
    """
    if event_count <= 0:
        return 0.0
    confidence = max_confidence if max_confidence is not None else 0.5
    base = 60.0 * confidence
    repeat_bonus = min(40.0, 10.0 * (event_count - 1))
    return min(100.0, base + repeat_bonus)


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
    avg_speed_cm_s: float | None = None,
    acoustic_event_count: int = 0,
    acoustic_max_confidence: float | None = None,
    path_width_m: float | None = None,
) -> RiskAssessment:
    """구역 하나의 종합 위험도를 산출한다."""
    density = visitor_count / area_m2 if area_m2 > 0 else 0.0
    personal_space = area_m2 / visitor_count if visitor_count > 0 else float("inf")

    d_score = density_to_score(density)
    f_score = flow_to_score(avg_speed_cm_s)
    a_score = acoustic_to_score(acoustic_event_count, acoustic_max_confidence)
    b_score = bottleneck_to_score(visitor_count, path_width_m)

    # 가중치 재정규화:
    # 센서가 아직 설치되지 않았거나 데이터가 결측이면 해당 지표의 가중치를 제외하고
    # 나머지 지표로 100%를 다시 배분한다. 이렇게 하지 않으면 결측 지표의 가중치가
    # 그대로 0점으로 반영되어, 밀집도가 아무리 높아도 상위 등급에 도달하지 못한다.
    components: list[tuple[float, float]] = [(d_score, WEIGHT_DENSITY)]
    if avg_speed_cm_s is not None:
        components.append((f_score, WEIGHT_FLOW))
    if acoustic_event_count > 0:
        components.append((a_score, WEIGHT_ACOUSTIC))
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
        flow_score=round(f_score, 2),
        acoustic_score=round(a_score, 2),
        bottleneck_score=round(b_score, 2),
        reason=_build_reason(density, personal_space, level),
    )
