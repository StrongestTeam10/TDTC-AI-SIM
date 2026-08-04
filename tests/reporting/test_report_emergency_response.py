"""위험 이벤트가 상정된 시나리오의 대응 검토 항목 생성을 검증한다.

이 항목들은 시뮬레이션이 산출한 값(대피 인원, 최대 밀집 구역, 위험점수)과
virtual_config에 실제로 들어 있는 이벤트만으로 만들어져야 한다. 이벤트가 없는
시나리오에서는 절 자체가 생기지 않아야 한다.
"""

import json
from pathlib import Path

from app.db.report_adapter import build_report_request
from app.reporting.analytics import assess_request
from app.reporting.narrative import NarrativeGenerator
from app.schemas.report_db_models import DbReportBundle


ROOT = Path(__file__).resolve().parents[2]
MOCK_PATH = ROOT / "data" / "db" / "night_market.json"

# 실제 DB에 저장된 화재 시나리오의 virtual_config 형태.
# 같은 구역·강도의 이벤트가 여러 건 들어오는 경우까지 포함한다.
FIRE_CONFIG = {
    "marketId": 1,
    "agentCount": 100,
    "steps": 30,
    "objects": [],
    "corridorPolicies": [],
    "events": [
        {"eventType": "fire", "zoneId": 2, "intensity": 0.5, "triggerStep": 14},
        {"eventType": "fire", "zoneId": 2, "intensity": 0.5, "triggerStep": 14},
        {"eventType": "fire", "zoneId": 1, "intensity": 0.5, "triggerStep": 14},
    ],
    "closedGateIds": [5, 4, 1],
}

NO_EVENT_CONFIG = {
    "marketId": 1,
    "agentCount": 100,
    "steps": 50,
    "objects": [],
    "corridorPolicies": [],
    "events": [],
    "closedGateIds": [],
}


def _build_request(
    virtual_config,
    *,
    evacuated_count=47,
    max_density_zone_name="중앙 구역",
):
    payload = json.loads(MOCK_PATH.read_text(encoding="utf-8"))

    # 보고서 1건 = 현행안 1개 vs 대안 1개(BE가 이렇게 제한해 보낸다).
    payload["scenario_rows"] = payload["scenario_rows"][:1]
    payload["result_rows"] = payload["result_rows"][:1]

    payload["scenario_rows"][0]["virtual_config"] = virtual_config
    payload["baseline_result"]["evacuated_count"] = 12
    payload["result_rows"][0]["evacuated_count"] = evacuated_count
    payload["result_rows"][0]["max_density_zone_name"] = max_density_zone_name

    return build_report_request(DbReportBundle.model_validate(payload))


def _template_narrative(request):
    return NarrativeGenerator()._generate_template(
        request,
        assess_request(request),
        [],
    )


def test_fire_scenario_produces_emergency_response():
    request = _build_request(FIRE_CONFIG)

    bullets = _template_narrative(request).emergency_response
    joined = " ".join(bullets)

    assert bullets

    # 같은 설명의 이벤트는 건수로 묶여 나온다.
    assert "2구역 화재 발생 상정 (강도 0.5) 2건" in joined
    assert "1구역 화재 발생 상정 (강도 0.5)" in joined

    # 발동 시점과 시뮬레이션 산출값이 근거로 실린다.
    assert "14스텝" in joined
    assert "47명" in joined and "12명" in joined
    assert "중앙 구역" in joined

    # 모델링하지 않는 부분을 고지한다.
    assert "확산" in joined


def test_scenario_without_events_has_no_emergency_response():
    request = _build_request(
        NO_EVENT_CONFIG,
        evacuated_count=0,
        max_density_zone_name=None,
    )

    assert _template_narrative(request).emergency_response == []


def test_llm_response_falls_back_to_template_when_unusable():
    """LLM이 키를 빠뜨리거나 형식을 어겨도 절이 사라지지 않아야 한다."""

    request = _build_request(FIRE_CONFIG)
    assessments = assess_request(request)
    generator = NarrativeGenerator()

    expected = generator._template_emergency_response(request, assessments)
    assert expected

    for unusable in (None, [], 12345, ""):
        assert generator._normalize_emergency_response(
            unusable,
            request,
            assessments,
        ) == expected

    # 정상 응답은 그대로 쓴다.
    assert generator._normalize_emergency_response(
        ["LLM이 작성한 대응 항목"],
        request,
        assessments,
    ) == ["LLM이 작성한 대응 항목"]


def test_llm_cannot_invent_emergency_response_without_events():
    """이벤트가 없으면 LLM이 무엇을 반환했든 절을 만들지 않는다."""

    request = _build_request(
        NO_EVENT_CONFIG,
        evacuated_count=0,
        max_density_zone_name=None,
    )

    assert NarrativeGenerator()._normalize_emergency_response(
        ["화재 발생 시 즉시 대피를 유도한다."],
        request,
        assess_request(request),
    ) == []
