"""Mesa 에이전트 정의."""

from __future__ import annotations

import random
from enum import Enum

from mesa import Agent


class VisitorState(str, Enum):
    NORMAL = "normal"
    CONGESTED = "congested"
    EVACUATING = "evacuating"


class VisitorAgent(Agent):
    """
    시장 방문객 에이전트.

    2026-07-25 추가: 대피 중인 방문객이 출구 구역에 도착한 뒤 실제로
    "게이트를 통과해서 밖으로 나가는" 동작이 없어서, 게이트를 열고 닫아도
    아무 차이가 없던 문제를 고쳤다. 이제 출구 구역 도착 후에는 그 구역 안의
    열려있는 게이트 좌표까지 마저 걸어가고, 도착하면 시뮬레이션에서 완전히
    제거된다(model.remove_agent 대신 Mesa 3.x의 Agent.remove() 사용).
    게이트가 전부 닫혀 있으면 next_zone_toward_exit()가 갈 곳을 못 찾아서
    그 자리에 멈춰있게 된다 - 이게 "막혔다"는 게 눈에 보이는 효과다.
    """

    MOVE_SPEED_M = 6.0

    MOVE_DECISION_PROBABILITY = 0.5
    """2026-07-25 변경: 매 결정 시점(경로가 빈 상태)마다 이 확률로 "다음 목적지를
    다시 고를지" 정한다. 안 고르면 이번 스텝은 제자리에 머문다(자연스러운 정지/배회)."""

    STAY_WEIGHT = 1.5
    """2026-07-25 추가: 목적지 후보 중 "지금 있는 구역"에 주는 기본 가중치.
    인접 구역들(가중치 1.0 + 매력도)과 비교되는 값이라, 값을 낮출수록 구역
    경계를 자주 넘나든다. 시장 구역 구분은 순전히 우리가 편의상 나눈 것일 뿐,
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
        self.risk_tolerance = (
            risk_tolerance if risk_tolerance is not None else random.uniform(0.3, 0.9)
        )
        self._path: list[tuple[float, float, int | None]] = []
        # 2026-07-25 추가: 지금 걷고 있는 경로의 목적지가 "게이트(실제 퇴장 지점)"인지 표시.
        # True인 채로 경로를 다 걸으면 model에서 완전히 제거된다.
        self._heading_to_exit_gate = False

    def step(self) -> None:
        """한 타임스텝 동안의 행동."""
        zone_risk = self.model.zone_risk_score(self.zone_id)

        if zone_risk >= 75.0 or self.state is VisitorState.EVACUATING:
            if self.state is not VisitorState.EVACUATING:
                self.model.ever_evacuating = True
            self.state = VisitorState.EVACUATING
            self._ensure_path_to_exit()
        elif zone_risk >= 50.0 * self.risk_tolerance:
            self.state = VisitorState.CONGESTED
        else:
            self.state = VisitorState.NORMAL
            self._maybe_plan_new_path()

        self._advance_along_path()

    def _advance_along_path(self) -> None:
        """경로를 따라 이번 스텝에 이동 가능한 최대 거리(MOVE_SPEED_M)만큼 이동한다.

        2026-07-25 수정: 예전엔 첫 웨이포인트 하나만 보고, 그게 이동 가능 거리보다
        가까우면 남은 이동 여력을 그냥 버렸다. WalkableGrid 경유점이 촘촘해서
        (칸 하나당 1m 안팎) 실제로는 한 스텝에 1m씩만 전진하는 꼴이 됐고, 목적지가
        멀면 수십 스텝이 걸려 "거의 안 움직이는" 것처럼 보였다. 이제 남은 이동
        여력이 있는 한 다음 웨이포인트로 계속 이어서 소진한다.
        """
        if not self._path:
            return

        speed = self.MOVE_SPEED_M
        if self.state is VisitorState.CONGESTED:
            speed *= 0.4

        remaining = speed
        while remaining > 0 and self._path:
            target_x, target_y, arrive_zone = self._path[0]
            dx, dy = target_x - self.x, target_y - self.y
            dist = (dx * dx + dy * dy) ** 0.5

            if dist <= remaining or dist == 0:
                self.x, self.y = target_x, target_y
                if arrive_zone is not None:
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
        """
        if self._path:
            return
        if random.random() > self.MOVE_DECISION_PROBABILITY:
            return  # 이번엔 새 목적지를 안 고름 - 제자리에 머무름

        neighbors = self.model.neighbors_of(self.zone_id)
        candidates = [self.zone_id] + neighbors
        weights = [
            self.STAY_WEIGHT if zid == self.zone_id else 1.0 + self.model.attraction_of(zid)
            for zid in candidates
        ]
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
        }