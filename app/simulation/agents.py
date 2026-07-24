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

    MOVE_SPEED_M = 6.0
    """한 스텝당 최대 이동 거리(로컬 좌표계, 대략 미터 단위). 임시 캘리브레이션 값."""

    WANDER_PROBABILITY = 0.4
    """매력도 차이가 없어도(=매대 데이터가 아직 없어도) 정상 보행 중에는 구역 안에서
    계속 걸어다니는 것처럼 보이게 하는 확률."""

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
        # 위험 감수 성향: 낮을수록 빨리 대피를 시작한다.
        self.risk_tolerance = (
            risk_tolerance if risk_tolerance is not None else random.uniform(0.3, 0.9)
        )
        # 걸어가고 있는 경로(웨이포인트 목록). 각 원소는
        # (목표 x, 목표 y, 도착 시 설정할 zone_id 또는 None(구역 유지)).
        self._path: list[tuple[float, float, int | None]] = []

    def step(self) -> None:
        """한 타임스텝 동안의 행동."""
        zone_risk = self.model.zone_risk_score(self.zone_id)

        if zone_risk >= 75.0 or self.state is VisitorState.EVACUATING:
            self.state = VisitorState.EVACUATING
            self._ensure_path_to_exit()
        elif zone_risk >= 50.0 * self.risk_tolerance:
            self.state = VisitorState.CONGESTED
            # 혼잡 상태에서는 새 목적지를 정하지 않고, 이미 걷고 있던 경로가
            # 있다면 느리게(감속) 계속 진행한다.
        else:
            self.state = VisitorState.NORMAL
            self._maybe_plan_new_path()

        self._advance_along_path()

    def _advance_along_path(self) -> None:
        """현재 경로의 첫 웨이포인트를 향해 최대 MOVE_SPEED_M만큼 이동한다."""
        if not self._path:
            return

        speed = self.MOVE_SPEED_M
        if self.state is VisitorState.CONGESTED:
            speed *= 0.4  # 혼잡하면 보행 속도가 느려짐

        target_x, target_y, arrive_zone = self._path[0]
        dx, dy = target_x - self.x, target_y - self.y
        dist = (dx * dx + dy * dy) ** 0.5

        if dist <= speed or dist == 0:
            self.x, self.y = target_x, target_y
            if arrive_zone is not None:
                self.zone_id = arrive_zone
            self._path.pop(0)
        else:
            ratio = speed / dist
            self.x += dx * ratio
            self.y += dy * ratio

    def _ensure_path_to_exit(self) -> None:
        """대피 경로가 없으면(또는 다 걸었으면) 출구 쪽으로 한 구역 더 나아갈 경로를 잡는다."""
        if self._path:
            return
        next_zone = self.model.next_zone_toward_exit(self.zone_id)
        if next_zone is not None and next_zone != self.zone_id:
            dest_x, dest_y = self.model.random_point_in_zone(next_zone)
            self._path = self.model.build_path(self.x, self.y, dest_x, dest_y, next_zone)

    def _maybe_plan_new_path(self) -> None:
        """푸드트럭/행사존처럼 매력도가 높은 인접 구역으로 확률적으로 새 경로를 잡거나,
        매력도 차이가 없으면 확률적으로 같은 구역 안에서 걸어다닐 목적지를 잡는다.
        이미 걷고 있는 경로가 있으면 끝까지 걷게 두고 새로 잡지 않는다.
        """
        if self._path:
            return

        current_attraction = self.model.attraction_of(self.zone_id)
        best_zone, best_attraction = self.zone_id, current_attraction
        for neighbor in self.model.movement_graph.neighbors(self.zone_id):
            a = self.model.attraction_of(neighbor)
            if a > best_attraction:
                best_zone, best_attraction = neighbor, a

        if best_zone != self.zone_id:
            # attraction은 weight 합(1 이상일 수 있음)이라 그대로 확률로 쓰면 안 된다.
            # weight 합이 1을 넘으면 매번 100% 이동하는 걸 막기 위해 0~0.8로 눌러서 확률화.
            move_probability = min(best_attraction * 0.1, 0.8)
            if random.random() < move_probability:
                dest_x, dest_y = self.model.random_point_in_zone(best_zone)
                self._path = self.model.build_path(self.x, self.y, dest_x, dest_y, best_zone)
                return

        if random.random() < self.WANDER_PROBABILITY:
            x, y = self.model.random_point_in_zone(self.zone_id)
            self._path = self.model.build_path(self.x, self.y, x, y, None)

    def to_dict(self) -> dict:
        return {
            "agentId": self.unique_id,
            "zoneId": self.zone_id,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "state": self.state.value,
        }
