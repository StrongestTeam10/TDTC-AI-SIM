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
    이동 로직이 거의 사용되지 않지만, 파이프라인 B(What-if 시나리오)에서
    동일한 에이전트를 재사용하기 위해 이동/대피 로직을 함께 정의한다.

    평상시(NORMAL)에는 배치된 오브젝트(푸드트럭/행사존)의 매력도(attraction)에
    이끌려 인접 구역으로 이동할 수 있다.
    """

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

    def step(self) -> None:
        """한 타임스텝 동안의 행동."""
        zone_risk = self.model.zone_risk_score(self.zone_id)

        if zone_risk >= 75.0 or self.state is VisitorState.EVACUATING:
            self.state = VisitorState.EVACUATING
            self._move_toward_exit()
        elif zone_risk >= 50.0 * self.risk_tolerance:
            self.state = VisitorState.CONGESTED
        else:
            self.state = VisitorState.NORMAL
            self._maybe_move_toward_attraction()

    def _move_toward_exit(self) -> None:
        """가장 가까운 출구 방향의 인접 구역으로 이동한다."""
        next_zone = self.model.next_zone_toward_exit(self.zone_id)
        if next_zone is not None and next_zone != self.zone_id:
            self.zone_id = next_zone
            self.x, self.y = self.model.random_point_in_zone(next_zone)

    def _maybe_move_toward_attraction(self) -> None:
        """푸드트럭/행사존처럼 매력도가 높은 인접 구역으로 확률적으로 이동한다."""
        current_attraction = self.model.attraction_of(self.zone_id)
        best_zone, best_attraction = self.zone_id, current_attraction
        for neighbor in self.model.movement_graph.neighbors(self.zone_id):
            a = self.model.attraction_of(neighbor)
            if a > best_attraction:
                best_zone, best_attraction = neighbor, a

        if best_zone != self.zone_id and random.random() < best_attraction:
            self.zone_id = best_zone
            self.x, self.y = self.model.random_point_in_zone(best_zone)

    def to_dict(self) -> dict:
        return {
            "agentId": self.unique_id,
            "zoneId": self.zone_id,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "state": self.state.value,
        }