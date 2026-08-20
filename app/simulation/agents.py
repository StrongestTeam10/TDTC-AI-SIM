"""Mesa 에이전트 정의."""

from __future__ import annotations

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

    에이전트 유형(통행/쇼핑/맛집투어)별로 목적지 일정(itinerary)을 돌며,
    진입(ENTERING) -> 이동(MOVING) -> 체류(STAYING) -> 퇴장(EXITING)
    4단계 행동 상태를 오간다.
    """

    EVACUATION_THRESHOLD_BASE = 60.0
    EVACUATION_THRESHOLD_RANGE = 30.0
    """risk_tolerance(0.3~0.9)에 따라 개인별 대피 임계값을 60~90점 사이로
    다르게 잡아, 위험에 예민한 사람부터 먼저 반응하고 둔감한 사람은 늦게
    반응하는 식으로 대피가 서서히 퍼지게 한다(임의 튜닝값)."""

    MAX_DISPLAY_SHIFT_M = 0.6
    """2026-08-20: 표시 오프셋이 한 스텝에 바뀔 수 있는 최대 거리(m).

    좌우 오프셋의 기준축은 진행 방향의 수직(-hy, hx)이라, 사람이 방향을 틀면
    축이 통째로 회전해 오프셋이 반대편으로 넘어간다. 최대 2 x LANE_HALF_WIDTH_M
    (4.8m)를 한 프레임에 건너뛰어 화면에서 옆으로 순간이동하는 것처럼 보였다.
    변화량을 이 값으로 묶어 여러 스텝에 걸쳐 옮겨가게 한다.
    표시 전용이라 self.x/y 와 밀집도·대피 계산에는 영향이 없다."""

    LANE_HALF_WIDTH_M = 2.4
    """대피 겹침 완화용 표시 좌우 확산의 최대 반폭(m). 통로가 좁으면
    _display_position이 walkable한 선까지 자동으로 줄인다. 표시 전용이라 시뮬
    좌표(self.x/y)·밀집도·경로엔 영향 없다."""

    DENSITY_ONLY_EVACUATION_MARGIN = 15.0
    """화재 등 명시적 이벤트의 영향이 전혀 없는 구역(순수 밀집도만으로
    위험도가 오른 경우)에는 대피 임계값에 이 값을 더해 덜 민감하게 만든다
    (60~90점 → 75~105점, 사실상 최고 밀집 상황에서만 대피). 화재 영향권
    구역(model.zone_has_forced_risk()가 True)은 이 마진 없이 기존 임계값을
    그대로 쓴다 - 화재에는 민감하게, 단순 혼잡에는 둔감하게(임의 튜닝값)."""

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

        # 에이전트 유형 할당: 통행 40% / 쇼핑 45% / 맛집투어 15%.
        # 통행형 비중을 높게 잡아 항상 상당수가 이동 중이도록 한다
        # (쇼핑/맛집 비율이 높으면 대부분이 매대에 머물러 화면이 정적임).
        rand_val = self.random.random()
        if rand_val < 0.4:
            self.agent_type = AgentType.PASS_THROUGH
            self.speed = self.random.uniform(11.0, 13.0)
        elif rand_val < 0.85:
            self.agent_type = AgentType.SHOPPING
            self.speed = self.random.uniform(7.0, 9.0)
        else:
            self.agent_type = AgentType.FOOD_TOUR
            self.speed = self.random.uniform(7.0, 9.0)

        self.risk_tolerance = (
            risk_tolerance if risk_tolerance is not None else self.random.uniform(0.3, 0.9)
        )
        self._path: list[tuple[float, float, int | None]] = []

        # 레인 오프셋: 통로에서 한 줄로 쏠려 걷는 걸 완화하기 위해 표시(렌더)
        # 좌표만 흩어놓는다. unique_id로 좌우 위치(lane_frac, 연속값 -1~1)와
        # 진행방향 앞뒤 어긋남(lane_stagger)을 줘 통로 폭을 연속으로 채운다.
        # ⚠️ 시뮬 좌표(self.x/y)는 절대 안 바꾼다 - 밀집도·대피·경로는 원래
        # 좌표 그대로. unique_id 기반이라 결정적(개입 전/후 동일).
        self._lane_frac = ((self.unique_id * 2654435761) % 1024) / 1024.0 * 2.0 - 1.0  # ~[-1,1)
        self._lane_stagger = (((self.unique_id * 40503) % 1024) / 1024.0 - 0.5) * 1.4  # ~±0.7m
        self._heading: tuple[float, float] | None = None  # 최근 이동 방향(단위벡터)
        # 2026-08-20: 마지막으로 화면에 적용한 표시 오프셋(표시좌표 - 시뮬좌표).
        # 걷다 멈추는 순간 오프셋이 0으로 돌아가 옆으로 미끄러지던 것을 막는다.
        # 표시 전용이며 self.x/y 와는 무관하다(_display_position 참고).
        self._last_display_delta: tuple[float, float] = (0.0, 0.0)

        # 일정 관리
        self.itinerary: list[dict] = []
        self.stay_timer = 0
        self.wandering_tendency = self.random.uniform(0.1, 0.3)
        self.patience_for_waiting = self.random.randint(15, 30)  # 대기 인원 15~30명 이상이면 포기

        # 화재 인지 상태. 화재가 나도 즉시 전원이 아는 게 아니라,
        # 화재 지점에서 퍼지는 인지 전선이 자기 위치에 닿아야 True가 되고 대피를
        # 시작한다. 한 번 알게 되면 계속 대피(다시 False로 안 돌아감).
        self.aware_of_fire = False
        # 개인별 반응 지연(거리 m로 환산). 인지 전선이 닿아도 사람마다 조금씩
        # 늦게 반응해서, 딱 떨어진 원이 아니라 자연스럽게 흩어지게 한다.
        self._fire_jitter = self.random.uniform(0.0, 12.0)

        # 쇼핑/맛집 유형이 일정을 다 돌았을 때 바로 퇴장하지 않고 새 목적지를
        # 더 잡아 계속 시장을 돌아다닐 확률. 상당수가 시뮬레이션 내내 매대
        # 사이를 오가며 활기가 유지되게 한다(통행형은 목적이 통과이므로 이
        # 값과 무관하게 반대편 출구로 나간다).
        self.reshop_probability = self.random.uniform(0.5, 0.75)

        # 개인별 대피 임계값. ENTERING 종료 시점(_assign_initial_itinerary)에
        # 다시 계산되지만, 그 전에 위험 체크가 들어올 경우를 대비해 미리 세팅.
        self.evacuation_threshold = (
            self.EVACUATION_THRESHOLD_BASE + self.EVACUATION_THRESHOLD_RANGE * self.risk_tolerance
        )
        self._previous_zone_id: int | None = None
        # 진입 대기 시간을 개인별로 흩어놓아 출발·도착·체류 타이밍을 분산시킨다.
        # 전원이 같은 스텝에 출발하면 도착·체류까지 동기화되어 화면상 움직이는
        # 사람이 뚝 끊기는 구간이 생긴다(1스텝=10초, 최소 1스텝은 진입 렌더링 유지).
        self.enter_timer = self.random.randint(1, 8)

    def _assign_initial_itinerary(self):
        # 목적지가 없으면 model에서 POI를 할당받음
        if self.agent_type == AgentType.PASS_THROUGH:
            # 통행형은 자신이 들어온 출구가 아닌 '반대편 출구' 중 하나를 목적지로 설정
            other_gates = [
                g for g in self.model.layout.gates
                if g.get("zone_id") != self.zone_id and g.get("zone_id") is not None
            ]
            if other_gates:
                target_gate = self.random.choice(other_gates)
                self.itinerary = [{"x": target_gate["x"], "y": target_gate["y"], "zone_id": target_gate["zone_id"]}]
            else:
                self.itinerary = []
        elif self.agent_type == AgentType.SHOPPING:
            self.itinerary = self.model.get_random_pois(count=self.random.randint(1, 3))
        elif self.agent_type == AgentType.FOOD_TOUR:
            self.itinerary = self.model.get_random_pois(count=self.random.randint(1, 2))

        self.action_state = ActionState.MOVING

        self.evacuation_threshold = (
            self.EVACUATION_THRESHOLD_BASE + self.EVACUATION_THRESHOLD_RANGE * self.risk_tolerance
        )
        self._previous_zone_id = None

    def step(self) -> None:
        """한 타임스텝 동안의 행동."""
        # 화재 인지 기반 대피.
        # 화재가 나도 전원이 동시에 아는 게 아니라, 화재 지점에서 통로를 따라
        # 퍼지는 인지 전선(model.fire_front_reached)이 자기 위치에 닿아야 비로소
        # 대피를 시작한다. 화재에 가까운 사람부터 순서대로 반응해 물결처럼
        # 바깥으로 퍼지고, 한 번 알게 되면(aware_of_fire=True) 쇼핑/관광/통행을
        # 전부 버리고 출구로 계속 향한다. 인지(대피 시작)는 '연소 중'일 때만
        # 새로 일어난다 - 진압된 뒤에는 아직 모르던 사람이나 새로 유입된
        # 사람이 대피를 시작하지 않고, 이미 대피 중이던 사람은 마저 나간다.
        if not self.aware_of_fire and self.model.fire_is_burning:
            if self.model.fire_front_reached(self.x, self.y, self._fire_jitter):
                self.aware_of_fire = True
                # 화재 대피 지표(ever_evacuating/evacuated_count/
                # evacuationTimeSeconds)는 '화재를 인지한 순간' 각 사람당 1회만
                # 집계한다. 상태(state)가 아니라 인지 전이로 세므로, 밀집으로
                # 이미 EVACUATING이던 사람이 뒤늦게 화재를 알아도 누락 없이
                # 정확히 1회 잡힌다(밀집 대피와 분리 - 아래 밀집 분기 참고).
                self.model.ever_evacuating = True
                self.model.evacuated_count += 1

        if self.aware_of_fire:
            if self.state is not VisitorState.EVACUATING:
                self.state = VisitorState.EVACUATING
                self.action_state = ActionState.EXITING
                self.itinerary = []  # 남은 쇼핑/관광 일정 폐기
                self._path = []      # 가던 경로 버리고 출구로 새 경로
            self._ensure_path_to_exit()
            self._advance_along_path()
            return

        if self.action_state == ActionState.ENTERING:
            if self.enter_timer > 0:
                self.enter_timer -= 1
                return  # 첫 1스텝은 ENTERING 상태를 유지해 프론트엔드가 렌더링할 수 있게 함
            self._assign_initial_itinerary()

        zone_risk = self.model.zone_risk_score(self.zone_id)

        # 여기 도달했다는 건 이 에이전트가 아직 화재를 인지하지 못했다는 뜻이다
        # (인지한 사람은 위 화재 분기에서 이미 return). 따라서 아래 대피 판단은
        # 순수 밀집도 위험(화재와 무관한 혼잡)만 다룬다.
        # 화재 영향권 구역(zone_has_forced_risk)에서는 오직 인지 전파로만 대피가
        # 시작되고, 순수 밀집도 대피는 화재 영향이 없는 구역에서만 한다 - 화재를
        # 모르는 사람이 부풀려진 위험도만으로 대피해버리는 모순 방지.
        fire_influenced = self.model.zone_has_forced_risk(self.zone_id)
        density_threshold = self.evacuation_threshold + self.DENSITY_ONLY_EVACUATION_MARGIN
        density_evacuate = (not fire_influenced) and zone_risk >= density_threshold

        # 1. Situation 평가 (위험도) - 순수 밀집도 기반
        if density_evacuate or self.state is VisitorState.EVACUATING:
            if self.state is not VisitorState.EVACUATING:
                # 순수 밀집 대피는 화재 대피 지표에 넣지 않는다. 화재 없이 붐비는
                # 상황(밀집도만으로 임계 초과)을 "대피 발생/미완료"로 오표기하지
                # 않기 위함이다. 사람은 여전히 혼잡을 피해 빠져나가지만
                # (행동 유지), evacuationTimeSeconds/evacuatedCount는 화재 전용이다.
                # 대피가 방금 시작된 시점이면 원래 경로를 버리고 게이트로 새 경로.
                self._path = []
            self.state = VisitorState.EVACUATING
            self.action_state = ActionState.EXITING
            self._ensure_path_to_exit()
        elif (not fire_influenced) and zone_risk >= 50.0 * self.risk_tolerance:
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

        # 즉흥적 이탈 (샛길 방문): SHOPPING/FOOD_TOUR만 즉흥적으로 상가에
        # 들른다. PASS_THROUGH(통행형)는 반대편 출구로 곧장 가는 유형이라 제외.
        if (
            self.agent_type != AgentType.PASS_THROUGH
            and self.itinerary
            and self.random.random() < self.wandering_tendency * 0.1
        ):
            near_pois = self.model.get_pois_near(self.x, self.y, radius=30.0)
            if near_pois:
                spontaneous_poi = self.random.choice(near_pois)
                # 현재 경로 스택 최상단에 추가
                self.itinerary.insert(0, spontaneous_poi)
                self._path = []  # 재계획 유도
                self._plan_next_destination()
                return

        self._advance_along_path()

    def _process_exiting(self) -> None:
        if not self._path:
            # 출구 주변에 도달 시 되돌아가기(Retracing) 확인.
            # 통행형은 상가를 들르지 않는 유형이므로 제외.
            if (
                self.agent_type != AgentType.PASS_THROUGH
                and self.state != VisitorState.EVACUATING
                and self.random.random() < 0.1  # 10% change of mind
            ):
                self.action_state = ActionState.MOVING
                self.itinerary = self.model.get_random_pois(count=1)
                self._plan_next_destination()
            else:
                self._ensure_path_to_exit()

        self._advance_along_path()

    def _plan_next_destination(self) -> None:
        if not self.itinerary:
            # 쇼핑/맛집 유형은 일정을 다 돌아도 곧바로 나가지 않고,
            # reshop_probability 확률로 새 목적지를 더 잡아 계속 시장을
            # 돌아다닌다(활기 유지). 나머지 확률로만 퇴장한다.
            if (
                self.agent_type != AgentType.PASS_THROUGH
                and self.random.random() < self.reshop_probability
            ):
                self.itinerary = self.model.get_random_pois(count=self.random.randint(1, 3))
                if self.itinerary:
                    self.action_state = ActionState.MOVING
                else:
                    self.action_state = ActionState.EXITING
                    self._ensure_path_to_exit()
                    return
            else:
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

        path = self.model.build_path(self.x, self.y, next_poi["x"], next_poi["y"], next_poi["zone_id"])
        if not path:
            # 목적지까지 가는 길 자체를 못 찾은 경우(통로 단절 등). 같은 목적지로
            # 영원히 재시도하며 제자리에 멈추지 않도록, 도달 불가능한 목적지는
            # 혼잡 포기(ABORT)와 동일하게 큐에서 빼고 다음 일정으로 넘어간다.
            # itinerary가 이걸로 비면 위 분기 그대로 EXITING으로 전환된다.
            self.itinerary.pop(0)
            self._plan_next_destination()
            return

        self._path = path

    def _advance_along_path(self) -> None:
        """
        경로를 따라 이번 스텝에 이동 가능한 최대 거리(self.speed)만큼 이동한다.
        이동 여력이 남는 한 여러 웨이포인트를 이어서 소진하고, 경로를 다 걸으면
        도착 후속 처리(체류 전환/퇴장)를 한다.
        """
        if not self._path:
            return

        speed = self.speed
        if self.state is VisitorState.CONGESTED:
            speed *= 0.4

        # 대피(EVACUATING) 중에는 주변 밀집도에 따라
        # 이동속도가 줄어든다 - 출구로 사람이 몰리면 병목이 생겨 느려지는 현실을
        # 반영. 반경 4m 안의 사람 수가 많을수록 느려지되(1명당 12%씩 감속), 최저
        # 25%까지만 떨어진다(완전히 멈추지는 않음). 인지가 시간차로 퍼져 대피가
        # 파상적으로 일어나면 출구 정체가 길어져 이 감속이 눈에 잘 띈다.
        if self.state is VisitorState.EVACUATING:
            nearby = self.model.count_agents_near(self.x, self.y, radius_m=4.0)
            crowd_factor = max(0.25, 1.0 - 0.12 * max(0, nearby - 1))
            speed *= crowd_factor

        remaining = speed
        while remaining > 0 and self._path:
            target_x, target_y, arrive_zone = self._path[0]
            dx, dy = target_x - self.x, target_y - self.y
            dist = (dx * dx + dy * dy) ** 0.5

            # 레인 오프셋: 표시용 좌우 치우침의 기준이 되는 진행 방향 저장.
            if dist > 1e-9:
                self._heading = (dx / dist, dy / dist)

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
                        # 출구(게이트)에 도착 -> 실제로 퇴장(소멸)
                        self.remove()
                        return
                    if self.action_state == ActionState.MOVING and self.itinerary:
                        target_poi = self.itinerary[0]
                        dist_to_poi = (
                            (self.x - target_poi["x"]) ** 2 + (self.y - target_poi["y"]) ** 2
                        ) ** 0.5
                        if dist_to_poi < 5.0:
                            self.action_state = ActionState.STAYING
                            kind = target_poi.get("kind")
                            self.itinerary.pop(0)
                            # 일반적인 시뮬레이션 길이(30스텝 내외=5분)에서도
                            # 매대 사이를 여러 번 오가는 게 보이도록 체류 시간을
                            # 짧게 잡는다(1스텝=10초).
                            if self.agent_type == AgentType.FOOD_TOUR:
                                self.stay_timer = self.random.randint(12, 24)  # 2~4분
                            elif self.agent_type == AgentType.SHOPPING:
                                self.stay_timer = self.random.randint(6, 15)  # 1~2.5분
                            else:
                                self.stay_timer = self.random.randint(2, 4)  # 20~40초
                            # 유인 오브젝트(행사장/휴게공간)에서는
                            # 사람들이 더 오래 머물러 군집(밀집)이 유지되게 한다.
                            if kind == "event_zone":
                                self.stay_timer += self.random.randint(12, 24)  # 행사 관람
                            elif kind == "rest_area":
                                self.stay_timer += self.random.randint(6, 15)   # 잠깐 휴식
            else:
                ratio = remaining / dist
                self.x += dx * ratio
                self.y += dy * ratio
                remaining = 0

    def _ensure_path_to_exit(self) -> None:
        """대피/퇴장 경로가 없으면(또는 다 걸었으면) 다음 행동을 정한다.

        model.nearest_reachable_gate_path()로 실제 경로가 나오는 가장 가까운
        열린 게이트를 찾는다(직선거리 최근접 게이트는 통로 단절 등으로 걸어갈
        수 없을 수 있음). 열린 게이트가 없거나 전부 도달 불가능하면 아무 경로도
        안 잡고 제자리에 멈춘다. 빈 경로가 반환되면 이미 게이트에 도달한 것이므로
        즉시 퇴장(remove)시킨다 - 빈 경로를 _path에 넣으면 remove()가 호출되지
        않아 출구 위에 멈춰있게 된다.
        """
        if self._path:
            return
        result = self.model.nearest_reachable_gate_path(self.x, self.y)
        if result is None:
            return  # 갈 수 있는 게이트가 없음 - 이 자리에 멈춰서 대기 (막힌 상태)
        _, path = result
        if path:
            self._path = path
        else:
            # 이미 게이트에 도달 -> 실제로 퇴장(소멸)
            self.remove()

    def _display_position(self) -> tuple[float, float]:
        """표시용 좌표. 걷는 중이면 (1) 진행방향 앞뒤로 살짝 어긋내 같은 줄에
        딱 맞춰 서지 않게 하고, (2) 좌우로 통로 폭만큼 연속 분산시켜 겹침을 푼다. 원하는
        좌우 오프셋이 통로 밖이면 walkable한 선까지 크기를 줄여가며 최대한 벌린다(예전처럼
        중앙으로 확 붙지 않음). ⚠️ 시뮬 좌표(self.x/y)는 안 바뀐다 - 밀집도·대피·경로엔
        원래 좌표가 쓰인다. 어디로도 못 벌리면 원래 좌표 그대로.

        2026-08-20 수정 - 표시 좌표가 프레임마다 옆으로 튀던 것을 잡는다.
          (1) 경로가 비면(목적지 도착 · 체류 · 진입 대기) 오프셋을 0으로 되돌리는
              대신 마지막에 쓰던 오프셋을 유지한다. 예전에는 걷다 멈추는 순간
              최대 LANE_HALF_WIDTH_M(2.4m)만큼 옆으로 미끄러졌다.
          (2) 통로가 좁아 오프셋을 줄일 때 단계가 (1.0, 0.7, 0.45, 0.25)로 성겨서,
              프레임마다 다른 단계가 뽑히면 좌우로 덜컹거렸다. 촘촘하게 나눈다.
        """
        grid = self.model.layout.walkable_grid

        def hold() -> tuple[float, float]:
            """직전 오프셋을 그대로 유지한다. 그 자리가 통로 밖이면 원좌표."""
            dx, dy = self._last_display_delta
            if dx == 0.0 and dy == 0.0:
                return self.x, self.y
            cx, cy = self.x + dx, self.y + dy
            if grid.is_walkable(grid.to_cell(cx, cy)):
                return cx, cy
            return self.x, self.y

        if not self._path or self._heading is None:
            return hold()

        hx, hy = self._heading
        px, py = -hy, hx  # 진행 방향에 수직(좌우)
        # (1) 진행 방향 앞뒤 어긋냄 - 같은 진행위치에 나란히 겹치는 걸 깬다.
        bx = self.x + hx * self._lane_stagger
        by = self.y + hy * self._lane_stagger
        # (2) 원하는 좌우 오프셋에서 시작해, 통로 밖이면 점점 줄여 walkable한 최대치를 쓴다.
        want = self._lane_frac * self.LANE_HALF_WIDTH_M
        ox, oy = self._last_display_delta
        for scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1):
            off = want * scale
            dx = (bx + px * off) - self.x
            dy = (by + py * off) - self.y
            # 오프셋이 한 번에 반대편으로 넘어가지 않도록 변화량을 묶는다.
            mx, my = dx - ox, dy - oy
            shift = (mx * mx + my * my) ** 0.5
            if shift > self.MAX_DISPLAY_SHIFT_M:
                k = self.MAX_DISPLAY_SHIFT_M / shift
                dx, dy = ox + mx * k, oy + my * k
            cx, cy = self.x + dx, self.y + dy
            if grid.is_walkable(grid.to_cell(cx, cy)):
                self._last_display_delta = (dx, dy)
                return cx, cy
        return hold()

    def to_dict(self) -> dict:
        disp_x, disp_y = self._display_position()
        return {
            "agentId": self.unique_id,
            "zoneId": self.zone_id,
            "x": round(disp_x, 3),
            "y": round(disp_y, 3),
            "state": self.state.value,
            "agentType": self.agent_type.value,
            "actionState": self.action_state.value,
            # FE 색 구분용 위험도(0~100). 화재를 아직 모르면(파랑)
            # 0, 화재를 인지했으면 구역 위험도(화재 근처=빨강)로 하되 최소
            # AWARE_MIN_DANGER(노랑)을 보장해 "인지+먼 곳=노랑"이 되게 한다.
            # 화재가 없는 평상시에는 기존대로 구역 위험도(밀집)를 그대로 쓴다.
            "dangerLevel": round(self._display_danger(), 1),
        }

    def _display_danger(self) -> float:
        zone_risk = self.model.zone_risk_score(self.zone_id)
        if self.aware_of_fire:
            from app.simulation.model import AWARE_MIN_DANGER
            return max(zone_risk, AWARE_MIN_DANGER)
        if self.model.has_active_fire:
            return 0.0  # 화재는 났지만 아직 이 사람은 모름 -> 파랑
        return zone_risk  # 평상시: 밀집 기반 위험도 그대로