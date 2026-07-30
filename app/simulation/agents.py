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

    2026-07-29: 에이전트 유형(통행/쇼핑/맛집투어)별로 목적지 일정(itinerary)을
    돌며 체류(STAYING)하는 로직을 도입. 진입(ENTERING) -> 이동(MOVING) ->
    체류(STAYING) -> 퇴장(EXITING) 4단계 행동 상태를 오간다.

    ⚠️ 이 파일은 model.py에 get_random_pois()/get_pois_near()/
    count_agents_near() 메서드가 구현돼 있어야 정상 작동한다. model.py가
    이 메서드들을 아직 안 갖고 있으면 시뮬레이션 실행 중 AttributeError가 난다.
    """

    MOVE_SPEED_MIN_M = 6.0
    MOVE_SPEED_MAX_M = 10.0
    """참고용 범위값(현재는 __init__에서 agent_type별 self.speed를 직접 배정하므로
    이 범위는 실제로는 안 쓰임 - 정리 필요할 수 있음)."""

    EVACUATION_THRESHOLD_BASE = 60.0
    EVACUATION_THRESHOLD_RANGE = 30.0
    """2026-07-27 추가: risk_tolerance(0.3~0.9)에 따라 개인별 대피 임계값을
    60~90점 사이로 다르게 잡아서, 위험이 예민한 사람부터 먼저 반응하고 둔감한
    사람은 늦게 반응하는 식으로 서서히 퍼지게 한다(임의 튜닝값)."""

    RETURN_PENALTY_FACTOR = 0.3
    """2026-07-27 추가: 정상 이동 시 방금 나온 구역으로 바로 되돌아갈 가중치에
    곱하는 페널티. 지금 버전에서는 목적지가 itinerary(POI 목록)로 결정되므로
    _maybe_plan_new_path()가 더 이상 안 쓰이지만, 하위 호환을 위해 남겨둠."""

    MOVE_DECISION_PROBABILITY = 0.5
    STAY_WEIGHT = 1.5

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
            self.speed = random.uniform(12.0, 14.0)  # 1.2~1.4 m/s * 10s
        elif rand_val < 0.8:
            self.agent_type = AgentType.SHOPPING
            self.speed = random.uniform(8.0, 10.0)  # 0.8~1.0 m/s * 10s
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
        self.patience_for_waiting = random.randint(15, 30)  # 대기 인원 15~30명 이상이면 포기

        # 2026-07-27 추가: 개인별 대피 임계값. ENTERING 종료 시점(_assign_initial_itinerary)에
        # 다시 계산되지만, 그 전에 위험 체크가 들어올 경우를 대비해 기본값도 미리 세팅.
        self.evacuation_threshold = (
            self.EVACUATION_THRESHOLD_BASE + self.EVACUATION_THRESHOLD_RANGE * self.risk_tolerance
        )
        self._previous_zone_id: int | None = None
        self.enter_timer = 1

    def _assign_initial_itinerary(self):
        # 목적지가 없으면 model에서 POI를 할당받음
        if self.agent_type == AgentType.PASS_THROUGH:
            # 통행형은 자신이 들어온 출구가 아닌 '반대편 출구' 중 하나를 목적지로 설정
            other_gates = [
                g for g in self.model.layout.gates
                if g.get("zone_id") != self.zone_id and g.get("zone_id") is not None
            ]
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

        # 2026-07-27 추가: 사람마다 다른 이동 속도(m/스텝). agent_type별 self.speed와
        # 별개로 재배정되던 값인데, 실제 이동에는 self.speed를 쓰므로 여기서는
        # 재계산하지 않고 유지만 한다(중복 속성 정리는 추후 필요).
        self.evacuation_threshold = (
            self.EVACUATION_THRESHOLD_BASE + self.EVACUATION_THRESHOLD_RANGE * self.risk_tolerance
        )
        self._previous_zone_id = None

    def step(self) -> None:
        """한 타임스텝 동안의 행동."""
        if self.action_state == ActionState.ENTERING:
            if self.enter_timer > 0:
                self.enter_timer -= 1
                return  # 첫 1스텝은 ENTERING 상태를 유지해 프론트엔드가 렌더링할 수 있게 함
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
            return  # 대피 중이면 머무르지 않음

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
                self._path = []  # 재계획 유도
                self._plan_next_destination()
                return

        self._advance_along_path()

    def _process_exiting(self) -> None:
        if not self._path:
            # 출구 주변에 도달 시 되돌아가기(Retracing) 확인
            if self.state != VisitorState.EVACUATING and random.random() < 0.1:  # 10% change of mind
                self.action_state = ActionState.MOVING
                self.itinerary = self.model.get_random_pois(count=1)
                self._plan_next_destination()
            else:
                self._ensure_path_to_exit()

        self._advance_along_path()

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
        """
        경로를 따라 이번 스텝에 이동 가능한 최대 거리(self.speed)만큼 이동한다.

        2026-07-29 수정: 이전 병합 과정에서 옛 버전(remaining 변수를 쓰는
        while 루프)과 새 버전(speed 변수를 직접 쓰는 if문) 코드가 중복으로
        겹쳐 들어가 문법 오류가 났던 부분을 정리했다. 지금은 "이동 여력이
        남는 한 여러 웨이포인트를 이어서 소진"하는 방식(2026-07-25 도입)과
        "목적지 도착 시 상태 전환/퇴장 처리"(2026-07-29 도입)를 하나로
        합쳤다.
        """
        if not self._path:
            return

        speed = self.speed
        if self.state is VisitorState.CONGESTED:
            speed *= 0.4

        remaining = speed
        while remaining > 0 and self._path:
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

                if not self._path:
                    # 경로를 다 걸었을 때만 도착 후속 처리를 한다.
                    if self.action_state == ActionState.EXITING:
                        # 2026-07-29: 출구(게이트)에 도착 -> 실제로 퇴장(소멸)
                        self.remove()
                        return
                    if self.action_state == ActionState.MOVING and self.itinerary:
                        target_poi = self.itinerary[0]
                        dist_to_poi = (
                            (self.x - target_poi["x"]) ** 2 + (self.y - target_poi["y"]) ** 2
                        ) ** 0.5
                        if dist_to_poi < 5.0:
                            self.action_state = ActionState.STAYING
                            self.itinerary.pop(0)
                            if self.agent_type == AgentType.FOOD_TOUR:
                                self.stay_timer = random.randint(90, 180)  # 15~30분 (1스텝=10초)
                            elif self.agent_type == AgentType.SHOPPING:
                                self.stay_timer = random.randint(18, 42)  # 3~7분
                            else:
                                self.stay_timer = random.randint(3, 6)  # 30초~1분
            else:
                ratio = remaining / dist
                self.x += dx * ratio
                self.y += dy * ratio
                remaining = 0

    def _ensure_path_to_exit(self) -> None:
        """대피/퇴장 경로가 없으면(또는 다 걸었으면) 다음 행동을 정한다.

        출구 구역보다 더 가까운 인접 구역이 있으면 그쪽으로 한 구역 더
        이동한다. 더 가까운 구역이 없다는 건 이미 출구 구역에 도착했다는
        뜻이므로, 그 구역 안의 열려있는 게이트로 마저 걸어가서 실제로
        퇴장하게 한다(도착 처리는 _advance_along_path에서 처리). 게이트가
        없으면(전부 닫힘) 아무 경로도 안 잡고 제자리에 멈춘다.
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
        # else: 열린 게이트가 없음 - 이 구역에 멈춰서 대기 (막힌 상태)

    def _maybe_plan_new_path(self) -> None:
        """
        (구버전 목적지 결정 로직, 하위 호환용으로 남겨둠 - 지금은 itinerary/POI
        기반 _plan_next_destination()이 이 역할을 대신하므로 실제로는 호출되지
        않는다.)
        """
        if self._path:
            return
        if random.random() > self.MOVE_DECISION_PROBABILITY:
            return

        neighbors = self.model.neighbors_of(self.zone_id)
        candidates = [self.zone_id] + neighbors
        weights = []
        for zid in candidates:
            if zid == self.zone_id:
                w = self.STAY_WEIGHT
            else:
                w = 1.0 + self.model.attraction_of(zid)
                if zid == self._previous_zone_id:
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