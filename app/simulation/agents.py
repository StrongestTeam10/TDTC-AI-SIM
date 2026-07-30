"""Mesa 에이전트 정의."""

from __future__ import annotations

import random
from enum import Enum

from mesa import Agent


class VisitorState(str, Enum):
    NORMAL = "normal"
    """정상 보행."""
    CONGESTED = "congested"
    """혼잡으로 보행 속도가 저하된 상태."""
    EVACUATING = "evacuating"
    """위험 감지로 출구를 향해 대피 중."""

class ActionState(str, Enum):
    ENTERING = "ENTERING"
    MOVING = "MOVING"
    STAYING = "STAYING"
    EXITING = "EXITING"

class AgentType(str, Enum):
    PASS_THROUGH = "PASS_THROUGH"
    SHOPPING = "SHOPPING"
    FOOD_TOUR = "FOOD_TOUR"


class VisitorAgent(Agent):
    """
    시장 방문객 에이전트.

    파이프라인 A(실측 미러링)에서는 센서 집계값으로 위치가 결정되므로
    이동 로직이 거의 사용되지 않지만, 파이프라인 B(What-if 시나리오)와 예측
    시뮬레이션에서 동일한 에이전트를 재사용하기 위해 이동/대피 로직을 함께 정의한다.

    2026-07-24: 목적지를 정하면 한 스텝 만에 그 자리로 이동하던 방식(순간이동처럼
    보이고, 폴리곤 바깥을 가로지르기도 함)을 폐기하고, 매 스텝 최대
    MOVE_SPEED_M만큼만 목적지 쪽으로 걸어가는 방식으로 바꿨다. 구역을 넘어갈 때는
    두 구역이 실제로 맞닿은 경계 지점(model.build_path)을 먼저 지나가게 해서
    실제 통로를 걸어가는 것처럼 보이게 한다.
    """

    MOVE_SPEED_MIN_M = 6.0
    MOVE_SPEED_MAX_M = 10.0
    """2026-07-27 변경: 기존엔 전원 6.0m/스텝 고정이었음. STEP_DURATION_SECONDS=10초
    기준으로 6.0m/10s는 초속 0.6m/s인데, 이건 근거 없이 잡은 값이었다. 일반적으로
    알려진 보통 성인 보행 속도(약 1.0~1.5m/s)의 하한에 가까운 값을 혼잡한 시장
    상황을 감안해 채택, 8~12m/10s(초속 0.8~1.2m/s) 구간에서 사람마다 랜덤 배정한다
    (정확한 실측 근거는 없는 추정치 - 실측 데이터 확보 시 재보정 필요).

    MOVE_SPEED_MIN_M/MAX_M은 클래스 상수(범위 정의)이고, 실제 각 에이전트의
    속도는 __init__에서 self.move_speed_m으로 개별 배정된다."""

    EVACUATION_THRESHOLD_BASE = 60.0
    EVACUATION_THRESHOLD_RANGE = 30.0
    """2026-07-27 추가: 예전엔 위험도 75점이 전원에게 똑같이 적용되는 대피 기준선이라,
    임계값을 넘는 순간 그 구역 사람들이 한꺼번에 대피를 시작했다. 이제
    risk_tolerance(0.3~0.9)에 따라 개인별 대피 임계값을 60~90점 사이로 다르게
    잡아서(threshold = 60 + 30*risk_tolerance), 위험이 예민한 사람부터 먼저
    반응하고 둔감한 사람은 늦게 반응하는 식으로 서서히 퍼지게 한다(임의 튜닝값)."""

    RETURN_PENALTY_FACTOR = 0.3
    """2026-07-27 추가: 정상 이동 시 방금 나온 구역으로 바로 되돌아갈 가중치에
    곱하는 페널티(1.0이면 페널티 없음, 0에 가까울수록 강하게 억제). 완전히
    막지는 않되, 목적 없이 왔다갔다하는 부자연스러운 움직임을 줄인다(임의 튜닝값)."""

    MOVE_DECISION_PROBABILITY = 0.5
    """2026-07-25 변경: 매 결정 시점(경로가 빈 상태)마다 이 확률로 "다음 목적지를
    다시 고를지" 정한다. 안 고르면 이번 스텝은 제자리에 머문다(자연스러운 정지/배회)."""

    STAY_WEIGHT = 1.5
    """2026-07-25 추가: 목적지 후보 중 "지금 있는 구역"에 주는 기본 가중치.
    인접 구역들(가중치 1.0 + 매력도)과 비교되는 값이라, 값을 낮출수록 구역
    경계를 자주 넘나든다. 시장 구역 구분은 순전히 우리가 관리 편의상 나눈 것일 뿐,
    실제 사람은 옆 구역이라고 안 넘어가지 않는다는 점을 반영 - 구역을 "물리적
    장벽"이 아니라 "동등한 선택지 중 하나"로 취급한다(임의 튜닝값)."""

    def __init__(
        self,
        model,
        zone_id: int,
        x: float = 0.0,
        y: float = 0.0,
        risk_tolerance: float | None = None,
    ) -> None:
        super().__init__(model)
        self.zone_id = zone_id
        self.x = x
        self.y = y
        self.state = VisitorState.NORMAL
        self.action_state = ActionState.ENTERING

        # 에이전트 유형 할당 (20%, 60%, 20%)
        rand_val = random.random()
        if rand_val < 0.2:
            self.agent_type = AgentType.PASS_THROUGH
            self.speed = random.uniform(12.0, 14.0) # 1.2~1.4 m/s * 10s
        elif rand_val < 0.8:
            self.agent_type = AgentType.SHOPPING
            self.speed = random.uniform(8.0, 10.0) # 0.8~1.0 m/s * 10s
        else:
            self.agent_type = AgentType.FOOD_TOUR
            self.speed = random.uniform(8.0, 10.0)

        self.risk_tolerance = (
            risk_tolerance if risk_tolerance is not None else random.uniform(0.3, 0.9)
        )
        self._path: list[tuple[float, float, int | None]] = []

        # 일정 관리
        self.itinerary: list[dict] = []
        self.stay_timer = 0
        self.wandering_tendency = random.uniform(0.1, 0.3)
        self.patience_for_waiting = random.randint(15, 30) # 대기 인원 15~30명 이상이면 포기

        # 스폰 후 초기화 단계에서 방문 목적지 할당 (model 쪽에서 호출되거나 step 1에서 처리)

    def _assign_initial_itinerary(self):
        # 목적지가 없으면 model에서 POI를 할당받음
        if self.agent_type == AgentType.PASS_THROUGH:
            # 통행형은 자신이 들어온 출구가 아닌 '반대편 출구' 중 하나를 목적지로 설정
            other_gates = [g for g in self.model.layout.gates if g.get("zone_id") != self.zone_id and g.get("zone_id") is not None]
            if other_gates:
                target_gate = random.choice(other_gates)
                self.itinerary = [{"x": target_gate["x"], "y": target_gate["y"], "zone_id": target_gate["zone_id"]}]
            else:
                self.itinerary = []
        elif self.agent_type == AgentType.SHOPPING:
            self.itinerary = self.model.get_random_pois(count=random.randint(1, 3))
        elif self.agent_type == AgentType.FOOD_TOUR:
            self.itinerary = self.model.get_random_pois(count=random.randint(1, 2))

        self.action_state = ActionState.MOVING

        # 2026-07-27 추가: 사람마다 다른 이동 속도(m/스텝).
        self.move_speed_m = random.uniform(self.MOVE_SPEED_MIN_M, self.MOVE_SPEED_MAX_M)

        # 2026-07-27 추가: 사람마다 다른 대피 시작 임계값. risk_tolerance가 낮을수록
        # (위험에 예민할수록) 더 낮은 위험도에서 먼저 대피를 시작한다.
        self.evacuation_threshold = (
            self.EVACUATION_THRESHOLD_BASE + self.EVACUATION_THRESHOLD_RANGE * self.risk_tolerance
        )

        # 2026-07-27 추가: 방금 있었던 구역(직전 구역). 정상 이동 시 이 구역으로
        # 바로 되돌아가는 걸 억제하는 데 쓰인다(완전 차단은 아님).
        self._previous_zone_id: int | None = None

    def step(self) -> None:
        """한 타임스텝 동안의 행동."""
        if self.action_state == ActionState.ENTERING:
            if getattr(self, "enter_timer", 1) > 0:
                self.enter_timer = getattr(self, "enter_timer", 1) - 1
                return # 첫 1스텝은 ENTERING 상태를 유지해 프론트엔드가 렌더링할 수 있게 함
            self._assign_initial_itinerary()

        zone_risk = self.model.zone_risk_score(self.zone_id)

        # 1. Situation 평가 (위험도)
        if zone_risk >= self.evacuation_threshold or self.state is VisitorState.EVACUATING:
            if self.state is not VisitorState.EVACUATING:
                self.model.ever_evacuating = True
                self.model.evacuated_count += 1
            self.state = VisitorState.EVACUATING
            self.action_state = ActionState.EXITING
            self._ensure_path_to_exit()
        elif zone_risk >= 50.0 * self.risk_tolerance:
            self.state = VisitorState.CONGESTED
        else:
            self.state = VisitorState.NORMAL

        # 2. Action 평가 및 실행
        if self.action_state == ActionState.STAYING:
            self._process_staying()
        elif self.action_state == ActionState.MOVING:
            self._process_moving()
        elif self.action_state == ActionState.EXITING:
            self._process_exiting()

    def _process_staying(self) -> None:
        if self.state == VisitorState.EVACUATING:
            return # 대피 중이면 머무르지 않음

        if self.stay_timer > 0:
            self.stay_timer -= 1
        else:
            # 체류 완료 후 다음 목적지로
            self.action_state = ActionState.MOVING
            self._plan_next_destination()

    def _process_moving(self) -> None:
        # 목적지가 없고 이동 중이면 다음 목적지를 계획
        if not self._path:
            self._plan_next_destination()

        # 즉흥적 이탈 (샛길 방문)
        if self.itinerary and random.random() < self.wandering_tendency * 0.1:
            near_pois = self.model.get_pois_near(self.x, self.y, radius=30.0)
            if near_pois:
                spontaneous_poi = random.choice(near_pois)
                # 현재 경로 스택 최상단에 추가
                self.itinerary.insert(0, spontaneous_poi)
                self._path = [] # 재계획 유도
                self._plan_next_destination()
                return

        self._advance_along_path()

    def _process_exiting(self) -> None:
        if not self._path:
            # 출구 주변에 도달 시 되돌아가기(Retracing) 확인
            if self.state != VisitorState.EVACUATING and random.random() < 0.1: # 10% change of mind
                self.action_state = ActionState.MOVING
                self.itinerary = self.model.get_random_pois(count=1)
                self._plan_next_destination()
            else:
                self._ensure_path_to_exit()

        self._advance_along_path()
        # 실제 시뮬레이션에서는 맵 경계를 벗어나면 에이전트 소멸 처리를 할 수 있지만,
        # 이 코드베이스는 에이전트 리스트에서 지우는 로직이 model에 있을 것이라 가정(또는 단순히 외곽에 뭉침)

    def _plan_next_destination(self) -> None:
        if not self.itinerary:
            self.action_state = ActionState.EXITING
            self._ensure_path_to_exit()
            return

        next_poi = self.itinerary[0]

        # 혼잡도 체크하여 포기할지 결정 (ABORT)
        wait_count = self.model.count_agents_near(next_poi["x"], next_poi["y"], radius_m=5.0)
        if wait_count > self.patience_for_waiting:
            # 포기하고 큐에서 제거
            self.itinerary.pop(0)
            self._plan_next_destination()
            return

        self._path = self.model.build_path(self.x, self.y, next_poi["x"], next_poi["y"], next_poi["zone_id"])

    def _advance_along_path(self) -> None:
        """경로를 따라 이번 스텝에 이동 가능한 최대 거리(self.move_speed_m)만큼 이동한다.

        2026-07-25 수정: 예전엔 첫 웨이포인트 하나만 보고, 그게 이동 가능 거리보다
        가까우면 남은 이동 여력을 그냥 버렸다. WalkableGrid 경유점이 촘촘해서
        (칸 하나당 1m 안팎) 실제로는 한 스텝에 1m씩만 전진하는 꼴이 됐고, 목적지가
        멀면 수십 스텝이 걸려 "거의 안 움직이는" 것처럼 보였다. 이제 남은 이동
        여력이 있는 한 다음 웨이포인트로 계속 이어서 소진한다.

        2026-07-27 수정: 속도가 클래스 공통값(MOVE_SPEED_M)에서 개인별 값
        (self.move_speed_m)으로 바뀌었다.
        """
        if not self._path:
            return

        speed = self.move_speed_m
        if self.state is VisitorState.CONGESTED:
            speed *= 0.4

        target_x, target_y, arrive_zone = self._path[0]
        dx, dy = target_x - self.x, target_y - self.y
        dist = (dx * dx + dy * dy) ** 0.5

            if dist <= remaining or dist == 0:
                self.x, self.y = target_x, target_y
                if arrive_zone is not None and arrive_zone != self.zone_id:
                    self._previous_zone_id = self.zone_id
                    self.zone_id = arrive_zone
                self._path.pop(0)
                remaining -= dist
                if not self._path and self._heading_to_exit_gate:
                    self.remove()
                    return
            else:
                ratio = remaining / dist
                self.x += dx * ratio
                self.y += dy * ratio
                remaining = 0
        if dist <= speed or dist == 0:
            self.x, self.y = target_x, target_y
            if arrive_zone is not None:
                self.zone_id = arrive_zone
            self._path.pop(0)

            # 도착했는데 현재 EXITING 상태이고 더 이상 경로가 없다면 화면에서 완전히 퇴장(소멸)
            if not self._path and self.action_state == ActionState.EXITING:
                if self in self.model.agents:
                    self.model.agents.remove(self)
                return

            # 경로를 다 걸었고, 현재 목표가 itinerary의 POI였다면 STAYING 상태로 전환
            if not self._path and self.action_state == ActionState.MOVING and self.itinerary:
                target_poi = self.itinerary[0]
                # 타겟 근처에 도달했는지 확인
                dist_to_poi = ((self.x - target_poi["x"])**2 + (self.y - target_poi["y"])**2)**0.5
                if dist_to_poi < 5.0:
                    self.action_state = ActionState.STAYING
                    self.itinerary.pop(0)
                    if self.agent_type == AgentType.FOOD_TOUR:
                        self.stay_timer = random.randint(90, 180) # 15~30 min (1 step = 10s -> 90~180 steps)
                    elif self.agent_type == AgentType.SHOPPING:
                        self.stay_timer = random.randint(18, 42) # 3~7 min
                    else:
                        self.stay_timer = random.randint(3, 6) # 30s ~ 1m
        else:
            ratio = speed / dist
            self.x += dx * ratio
            self.y += dy * ratio

    def _ensure_path_to_exit(self) -> None:
        """대피 경로가 없으면(또는 다 걸었으면) 다음 행동을 정한다.

        2026-07-25 변경: 출구 구역보다 더 가까운 인접 구역이 있으면 예전처럼
        그쪽으로 한 구역 더 이동한다. 더 가까운 구역이 없다는 건 이미 출구
        구역에 도착했다는 뜻이므로, 그 구역 안의 열려있는 게이트로 마저
        걸어가서 실제로 퇴장하게 한다. 게이트가 없으면(전부 닫힘) 아무 경로도
        안 잡고 제자리에 멈춘다.
        """
        if self._path:
            return
        next_zone = self.model.next_zone_toward_exit(self.zone_id)
        if next_zone is not None and next_zone != self.zone_id:
            dest_x, dest_y = self.model.random_point_in_zone(next_zone)
            self._path = self.model.build_path(self.x, self.y, dest_x, dest_y, next_zone)
            return

        gate = self.model.gate_in_zone(self.zone_id)
        if gate is not None:
            self._path = self.model.build_path(self.x, self.y, gate["x"], gate["y"], None)
            self._heading_to_exit_gate = True
        # else: 열린 게이트가 없음 - 이 구역에 멈춰서 대기 (막힌 상태)

    def _maybe_plan_new_path(self) -> None:
        """
        다음 목적지를 정한다.

        2026-07-25 전면 변경: 예전엔 "매력도가 더 높은 인접 구역이 있을 때만"
        구역을 넘어갔고, 그마저도 별도의 낮은 확률로만 허용했다. 그 결과
        오브젝트(푸드트럭 등)를 안 놓으면 사람들이 자기 구역 밖으로 사실상
        못 나가는, 마치 구역 경계가 물리적 벽인 것처럼 보이는 부자연스러운
        움직임이 됐다.

        구역은 우리가 관리 편의상 나눈 것일 뿐 실제 시장에는 그런 경계선이
        없으므로, 이제 "지금 구역에 머무르기"와 "각 인접 구역으로 이동하기"를
        전부 동등한 후보로 놓고 가중치 추첨(weighted random choice)한다.
        매력도는 그 가중치에 보너스를 줄 뿐, 이동 자체를 막는 게이트가 아니다.

        2026-07-27 추가: 후보가 "방금 나왔던 구역"이면 가중치에
        RETURN_PENALTY_FACTOR를 곱해서 즉시 왕복(되돌아가기)을 억제한다.
        """
        if self._path:
            return
        if random.random() > self.MOVE_DECISION_PROBABILITY:
            return  # 이번엔 새 목적지를 안 고름 - 제자리에 머무름

        neighbors = self.model.neighbors_of(self.zone_id)
        candidates = [self.zone_id] + neighbors
        weights = []
        for zid in candidates:
            if zid == self.zone_id:
                w = self.STAY_WEIGHT
            else:
                w = 1.0 + self.model.attraction_of(zid)
                if zid == self._previous_zone_id:
                    # 2026-07-27 추가: 방금 나온 구역으로 바로 되돌아가는 걸 억제
                    w *= self.RETURN_PENALTY_FACTOR
            weights.append(w)
        target_zone = random.choices(candidates, weights=weights, k=1)[0]

        dest_x, dest_y = self.model.random_point_in_zone(target_zone)
        arrive_zone = target_zone if target_zone != self.zone_id else None
        self._path = self.model.build_path(self.x, self.y, dest_x, dest_y, arrive_zone)

    def to_dict(self) -> dict:
        return {
            "agentId": self.unique_id,
            "zoneId": self.zone_id,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "state": self.state.value,
            "agentType": self.agent_type.value,
            "actionState": self.action_state.value,
        }
