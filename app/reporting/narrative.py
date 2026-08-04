"""대안별 변화량과 RAG 근거를 정책 사전검토 보고서 문장으로 변환한다.

LLM은 사실을 종합하는 역할만 하며 점수·순위·승인 상태를 새로 산출하지 않는다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .analytics import AlternativeAssessment
from app.schemas.report_models import (
    EvidenceItem,
    Intervention,
    ReportRequest,
)


@dataclass
class ScenarioFlowAnalysis:
    alternative_id: str
    alternative_name: str
    analysis: str


@dataclass
class EvidenceNote:
    source_id: str
    title: str
    page: int | None
    summary: str


@dataclass
class Narrative:
    report_title: str
    executive_summary: str
    current_issue: str
    recommendation_rationale: str
    implementation_plan: list[str]
    limitations: list[str]
    flow_analysis: list[ScenarioFlowAnalysis]
    evidence_notes: list[EvidenceNote]

    # 위험 이벤트가 있을 때만 채워진다. 이벤트가 없거나 템플릿 모드일 때는
    # 빈 목록이고, 렌더러가 해당 절을 통째로 생략한다.
    emergency_response: list[str] = field(default_factory=list)


class NarrativeGenerator:
    def __init__(self) -> None:
        self.last_mode = "template"
        self.last_error: str | None = None

    @staticmethod
    def _normalize_string_list(
        value: Any,
        field_name: str,
    ) -> list[str]:
        """LLM 응답을 비어 있지 않은 문자열 목록으로 정규화한다."""

        if isinstance(value, list):
            result = [
                str(item).strip()
                for item in value
                if str(item).strip()
            ]
            if not result:
                raise ValueError(
                    f"{field_name} 배열이 비어 있습니다."
                )
            return result

        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError(
                    f"{field_name} 문자열이 비어 있습니다."
                )

            parts = re.split(
                r"(?=\d+[.)]\s*)",
                text,
            )
            normalized = [
                re.sub(
                    r"^\d+[.)]\s*",
                    "",
                    part,
                ).strip()
                for part in parts
                if part.strip()
            ]
            return normalized or [text]

        raise TypeError(
            f"{field_name}은 문자열 또는 문자열 배열이어야 합니다. "
            f"실제 타입: {type(value).__name__}"
        )

    @staticmethod
    def _normalize_title(
        value: Any,
        fallback: str,
    ) -> str:
        """LLM 제목을 한 줄로 정리하고 비정상 응답에는 기본 제목을 사용한다."""

        title = " ".join(
            str(value or "").split()
        ).strip(" \"'")
        if not title or len(title) > 80:
            return fallback
        return title

    @staticmethod
    def _direction_label(value: Any) -> str:
        labels = {
            "BIDIRECTIONAL": "양방향 통행",
            "N_TO_S": "북문에서 남문 방향",
            "S_TO_N": "남문에서 북문 방향",
            "E_TO_W": "동문에서 서문 방향",
            "W_TO_E": "서문에서 동문 방향",
        }
        return labels.get(
            str(value),
            str(value),
        )

    def _template_flow_analysis(
        self,
        request: ReportRequest,
    ) -> list[ScenarioFlowAnalysis]:
        """LLM 실패 시 원본 유동 데이터를 사실 기반 문장으로 변환한다."""

        results: list[ScenarioFlowAnalysis] = []
        for alternative in [
            request.baseline,
            *request.alternatives,
        ]:
            flow = alternative.flow_direction

            if isinstance(flow, dict):
                mode = str(flow.get("mode", ""))
                direction = self._direction_label(
                    flow.get("direction", "미입력")
                )
                target = flow.get("target_zone")

                if mode == "one_way":
                    summary = (
                        f"{target or '대상 통로'}는 {direction}의 "
                        "일방통행으로 설정되었다. 교차 이동을 줄이는 효과를 "
                        "기대할 수 있으나, 진입부의 대기와 우회 동선 발생 여부를 "
                        "현장에서 함께 확인할 필요가 있다."
                    )
                elif mode == "existing":
                    summary = (
                        f"기존 {direction} 방식을 유지하는 시나리오다. "
                        "방문객의 이동 선택권은 유지되지만, 혼잡 시간대에는 "
                        "마주 오는 보행 흐름과 교차 지점의 병목 가능성을 "
                        "점검해야 한다."
                    )
                else:
                    summary = (
                        f"유동 방향은 {direction}으로 설정되었다. "
                        "세부 운영 방식과 병목구간 변화는 시뮬레이션 결과 및 "
                        "현장 조건과 함께 검토해야 한다."
                    )
            elif isinstance(flow, list):
                summary = (
                    "복수의 유동 방향 정보가 전달되었다. 구역별 이동 방향과 "
                    "교차 지점을 중심으로 혼잡 분산 효과를 확인해야 한다."
                )
            elif isinstance(flow, str) and flow.strip():
                summary = (
                    f"유동 방향 분석 결과는 '{flow.strip()}'로 전달되었다. "
                    "해당 흐름이 출입구와 주요 통로의 병목에 미치는 영향을 "
                    "현장 운영계획과 함께 검토해야 한다."
                )
            else:
                summary = (
                    "유동 방향 데이터가 제공되지 않아 이동 흐름의 변화를 "
                    "정성적으로 판단하기 어렵다. 시뮬레이션 결과에 구역별 "
                    "이동 방향을 포함한 뒤 재검토할 필요가 있다."
                )

            if alternative.economic_effect_analysis:
                summary += (
                    " 추가 분석 결과는 다음과 같다: "
                    f"{alternative.economic_effect_analysis}"
                )

            results.append(
                ScenarioFlowAnalysis(
                    alternative_id=alternative.alternative_id,
                    alternative_name=alternative.alternative_name,
                    analysis=summary,
                )
            )
        return results

    def _normalize_flow_analysis(
        self,
        value: Any,
        request: ReportRequest,
    ) -> list[ScenarioFlowAnalysis]:
        """LLM 유동 분석을 시나리오 ID 기준으로 검증하고 누락분을 보완한다."""

        fallback = {
            item.alternative_id: item
            for item in self._template_flow_analysis(
                request
            )
        }
        if not isinstance(value, list):
            return list(fallback.values())

        generated: dict[str, ScenarioFlowAnalysis] = {}
        for item in value:
            if not isinstance(item, dict):
                continue

            alternative_id = str(
                item.get("alternative_id", "")
            ).strip()
            analysis = str(
                item.get("analysis", "")
            ).strip()
            if (
                alternative_id not in fallback
                or not analysis
            ):
                continue

            generated[alternative_id] = (
                ScenarioFlowAnalysis(
                    alternative_id=alternative_id,
                    alternative_name=(
                        fallback[
                            alternative_id
                        ].alternative_name
                    ),
                    analysis=analysis,
                )
            )

        return [
            generated.get(
                alternative_id,
                fallback[alternative_id],
            )
            for alternative_id in fallback
        ]

    @staticmethod
    def _fallback_evidence_notes(
        evidence: list[EvidenceItem],
    ) -> list[EvidenceNote]:
        """검색 원문의 읽을 수 있는 첫 문장을 근거 설명으로 사용한다."""

        notes: list[EvidenceNote] = []
        for item in evidence:
            excerpt = " ".join(
                item.excerpt.split()
            )
            sentences = re.split(
                r"(?<=[.!?])\s+|(?<=다\.)\s+",
                excerpt,
            )
            summary = next(
                (
                    sentence.strip()
                    for sentence in sentences
                    if len(sentence.strip()) >= 40
                ),
                excerpt,
            )
            if len(summary) > 320:
                summary = (
                    summary[:320].rstrip()
                    + "…"
                )
            if not summary:
                summary = (
                    "해당 페이지에서 관광지 혼잡도 운영과 "
                    "정책 검토에 관련된 근거를 확인하였다."
                )

            notes.append(
                EvidenceNote(
                    source_id=item.source_id,
                    title=item.title,
                    page=item.page,
                    summary=summary,
                )
            )
        return notes

    def _normalize_evidence_notes(
        self,
        value: Any,
        evidence: list[EvidenceItem],
    ) -> list[EvidenceNote]:
        """LLM 근거 요약을 source_id 기준으로 검증하고 누락분을 보완한다."""

        fallback = {
            note.source_id: note
            for note in self._fallback_evidence_notes(
                evidence
            )
        }
        if not isinstance(value, list):
            return list(fallback.values())

        generated: dict[str, EvidenceNote] = {}
        for item in value:
            if not isinstance(item, dict):
                continue

            source_id = str(
                item.get("source_id", "")
            ).strip()
            summary = " ".join(
                str(
                    item.get("summary", "")
                ).split()
            )
            if (
                source_id not in fallback
                or len(summary) < 20
            ):
                continue

            source = fallback[source_id]
            generated[source_id] = EvidenceNote(
                source_id=source_id,
                title=source.title,
                page=source.page,
                summary=summary,
            )

        return [
            generated.get(
                source_id,
                fallback[source_id],
            )
            for source_id in fallback
        ]

    def generate(
        self,
        *,
        request: ReportRequest,
        assessments: list[AlternativeAssessment],
        evidence: list[EvidenceItem],
    ) -> Narrative:
        mode = os.getenv(
            "NARRATIVE_MODE",
            "template",
        ).lower().strip()
        self.last_mode = mode
        self.last_error = None

        if mode == "openai":
            try:
                return self._generate_with_openai(
                    request,
                    assessments,
                    evidence,
                )
            except Exception as exc:
                self.last_error = str(exc)
                if (
                    os.getenv(
                        "NARRATIVE_STRICT",
                        "false",
                    ).lower()
                    == "true"
                ):
                    raise
                self.last_mode = "template_fallback"

        return self._generate_template(
            request,
            assessments,
            evidence,
        )

    @staticmethod
    def _trigger_steps(intervention: Intervention) -> list[int]:
        """개입 parameters에서 발동 스텝을 꺼낸다.

        _merge_duplicates()가 같은 설명의 이벤트를 묶으면 parameters가
        {"items": [원본, ...]} 형태로 바뀌므로 두 모양을 모두 처리한다.
        """

        params = intervention.parameters or {}
        raw = params.get("items")
        candidates = raw if isinstance(raw, list) else [params]

        steps: list[int] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            step = item.get("triggerStep")
            if isinstance(step, int) and step not in steps:
                steps.append(step)
        return sorted(steps)

    @classmethod
    def _template_emergency_response(
        cls,
        request: ReportRequest,
        assessments: list[AlternativeAssessment],
    ) -> list[str]:
        """위험 이벤트가 상정된 시나리오에 한해 대응 검토 항목을 만든다.

        전달받은 값만 사용하고 대응 조치를 새로 지어내지 않는다. 시뮬레이션이
        산출한 결과(대피 인원, 최대 밀집 구역, 위험점수 변화)와 시나리오에
        실제로 들어 있는 이벤트 설정만 문장으로 옮긴다.
        """

        deltas_by_id = {
            assessment.alternative_id: {
                delta.key: delta
                for delta in assessment.deltas
            }
            for assessment in assessments
        }

        baseline_evacuated = request.baseline.metrics.evacuated_count
        bullets: list[str] = []
        has_event = False

        for alternative in request.alternatives:
            events = [
                item
                for item in alternative.interventions
                if item.intervention_type == "event_trigger"
            ]
            if not events:
                continue
            has_event = True

            prefix = (
                f"{alternative.alternative_name}: "
                if len(request.alternatives) > 1
                else ""
            )

            summary = ", ".join(item.description for item in events)
            steps = sorted(
                {
                    step
                    for item in events
                    for step in cls._trigger_steps(item)
                }
            )
            if steps:
                step_text = ", ".join(f"{step}스텝" for step in steps)
                summary += f" (발동 시점: {step_text})"
            bullets.append(f"{prefix}상정된 위험 이벤트 — {summary}")

            evacuated = alternative.metrics.evacuated_count
            if evacuated is not None:
                if baseline_evacuated is not None:
                    bullets.append(
                        f"{prefix}이 조건에서 대피 상태로 전환된 방문객은 "
                        f"{evacuated}명으로, 현행안 {baseline_evacuated}명과 비교된다. "
                        "대피 인원의 증감만으로는 안전성 개선 여부를 판단할 수 없으므로 "
                        "아래 밀집·위험 지표와 함께 검토한다."
                    )
                else:
                    bullets.append(
                        f"{prefix}이 조건에서 대피 상태로 전환된 방문객은 "
                        f"{evacuated}명이다."
                    )

            if alternative.max_density_zone_name:
                bullets.append(
                    f"{prefix}대피 과정에서 최대 밀집도가 관측된 구역은 "
                    f"{alternative.max_density_zone_name}이다. "
                    "해당 구역의 통로 폭과 출입구 접근성을 우선 점검 대상으로 둔다."
                )

            risk_delta = deltas_by_id.get(
                alternative.alternative_id, {}
            ).get("risk_score")
            if (
                risk_delta is not None
                and risk_delta.percent is not None
            ):
                direction = (
                    "낮아졌다"
                    if risk_delta.percent < 0
                    else "높아졌다"
                )
                bullets.append(
                    f"{prefix}이벤트를 포함한 조건의 예측 위험점수는 현행안 대비 "
                    f"{abs(risk_delta.percent):.1f}% {direction}"
                    f"({risk_delta.baseline} → {risk_delta.candidate})."
                )

        if not has_event:
            return []

        bullets.append(
            "본 시뮬레이션은 화재의 인접 구역 확산과 음향의 거리별 감쇠를 "
            "모델링하지 않는다. 화재는 발생 구역의 위험도를 높은 값으로 "
            "고정하고, 음향 이상은 지정 반경 안의 방문객을 한 차례 대피시키는 "
            "방식으로 반영된다. 따라서 위 수치는 실제 확산 양상이 아니라 "
            "해당 조건에서의 대피 흐름을 나타낸다."
        )
        return bullets

    @staticmethod
    def _delta_sentences(
        assessment: AlternativeAssessment,
    ) -> list[str]:
        sentences: list[str] = []
        for delta in assessment.deltas:
            if (
                delta.baseline is None
                or delta.candidate is None
            ):
                continue

            if delta.percent is None:
                sentences.append(
                    f"{delta.label}은 기준안 {delta.baseline}에서 "
                    f"{delta.candidate}로 나타났다."
                )
            else:
                change = (
                    "감소"
                    if delta.percent < 0
                    else "증가"
                )
                sentences.append(
                    f"{delta.label}은 기준안 대비 "
                    f"{abs(delta.percent):.1f}% {change}했다."
                )
        return sentences

    def _generate_template(
        self,
        request: ReportRequest,
        assessments: list[AlternativeAssessment],
        evidence: list[EvidenceItem],
    ) -> Narrative:
        comparison_summaries: list[str] = []
        for assessment in assessments:
            # 상한을 두지 않는다. _delta_sentences()가 기준안·대안 중 한쪽이라도
            # 값이 없는 지표를 이미 걸러내므로, 남은 것은 전부 실제로 산출된 지표다.
            # (예전에는 앞 3개만 썼는데, 지표가 늘면서 대피 인원처럼 뒤에 등록된
            #  항목이 조용히 잘려나갔다.)
            delta_text = " ".join(
                self._delta_sentences(assessment)
            )
            if delta_text:
                comparison_summaries.append(
                    f"{assessment.alternative_name}: {delta_text}"
                )

        comparison_text = " ".join(
            comparison_summaries
        )
        executive_summary = (
            f"{request.market.market_name}의 "
            f"'{request.report_title}' 작성을 위해 기준안과 "
            f"{len(request.alternatives)}개 대안을 비교했다. "
            f"{comparison_text or '비교 가능한 지표가 충분히 제공되지 않았다.'} "
            "본 결과는 대안별 예상 영향과 상충관계를 검토하기 위한 "
            "정책 사전검토 자료이며, 특정 대안의 승인이나 우선순위를 "
            "자동으로 결정하지 않는다."
        )

        baseline_values = (
            request.baseline.metrics.model_dump(
                exclude_none=True
            )
        )
        if baseline_values:
            current_issue = (
                "기준안에서 확인된 시뮬레이션 지표는 "
                f"{baseline_values}이다. 대안별 최대·평균 밀집도, "
                "외부 위험도 분석 시스템이 전달한 위험점수 및 "
                "평균 체류시간의 변화가 서로 다르므로, 담당자는 "
                "안전성과 운영 목적을 함께 검토해야 한다."
            )
        else:
            current_issue = (
                "기준안의 예측 지표가 제공되지 않아 정량 비교가 제한된다. "
                "시뮬레이션 결과를 보완한 뒤 다시 검토할 필요가 있다."
            )

        source_text = ", ".join(
            dict.fromkeys(
                item.title
                for item in evidence[:3]
            )
        )
        rationale = (
            "본 검토는 SIMRSLT01D에서 전달받은 결과의 기준안 대비 "
            "절대 변화량과 변화율을 계산하고, "
            f"{source_text or '벡터 검색으로 조회한 공개 근거자료'}의 "
            "관련 내용을 함께 제시했다. 보고서 생성 과정에서는 "
            "위험점수, 종합점수, 정책 순위 또는 검토 상태를 "
            "새로 산출하지 않는다."
        )

        plan = [
            "담당 부서와 시장 운영 주체가 대안별 안전성·운영성의 상충관계를 검토한다.",
            "실행 후보 대안은 제한된 시간대 또는 구역에서 시범 운영한다.",
            "시범 운영의 실측 밀집도와 체류시간을 시뮬레이션 예측값과 비교한다.",
            "예측 오차와 현장 의견을 반영해 시뮬레이션 조건과 실행계획을 보완한다.",
        ]

        limitations = [
            request.disclaimer,
            "본 보고서는 ERD에 정의된 시뮬레이션 요약 결과만 사용하며 구역별·시간대별 세부 결과는 포함하지 않는다.",
            "위험점수의 산출 기준과 정확도는 외부 위험도 분석 시스템의 검증 범위에 따른다.",
            "검색 근거의 적용 가능성과 인용 정확성은 담당자가 원문을 확인해야 한다.",
        ]
        return Narrative(
            report_title=request.report_title,
            executive_summary=executive_summary,
            current_issue=current_issue,
            recommendation_rationale=rationale,
            implementation_plan=plan,
            limitations=limitations,
            flow_analysis=self._template_flow_analysis(
                request
            ),
            evidence_notes=self._fallback_evidence_notes(
                evidence
            ),
            emergency_response=self._template_emergency_response(
                request,
                assessments,
            ),
        )

    def _generate_with_openai(
        self,
        request: ReportRequest,
        assessments: list[AlternativeAssessment],
        evidence: list[EvidenceItem],
    ) -> Narrative:
        from openai import OpenAI

        facts = {
            "request": request.model_dump(
                mode="json"
            ),
            "metric_comparisons": [
                assessment.to_dict()
                for assessment in assessments
            ],
            "retrieved_evidence": [
                item.model_dump(mode="json")
                for item in evidence
            ],
        }
        system = (
            "당신은 지방자치단체 전통시장 담당자를 위한 정책 사전검토 "
            "보고서 초안을 작성한다. 기준안과 모든 대안의 결과 및 "
            "상충관계를 균형 있게 설명한다. 보고서 엔진은 위험점수, "
            "종합점수, 정책 순위, 추천 대안 또는 승인 상태를 새로 "
            "산출하지 않으므로 이를 만들거나 단정하지 않는다. "
            "retrieved_evidence와 facts에 없는 수치·법령·효과를 만들지 않는다. "
            "응답은 report_title, executive_summary, current_issue, "
            "recommendation_rationale, implementation_plan, limitations, "
            "flow_analysis, evidence_notes, emergency_response 키를 "
            "가진 JSON 객체만 출력한다. report_title은 시장명과 검토 정책을 "
            "드러내는 50자 이내의 공공 보고서 제목으로 작성하되, 결과 수치나 "
            "특정 대안의 우수성을 제목에서 단정하지 않는다. "
            "facts.request.generate_report_title이 false이면 "
            "facts.request.report_title을 그대로 반환한다. "
            "report_title, executive_summary, current_issue, "
            "recommendation_rationale는 문자열로, "
            "implementation_plan과 limitations는 반드시 문자열 배열로 작성한다. "
            "flow_analysis는 기준안과 모든 대안을 입력 순서대로 한 번씩 포함한 "
            "객체 배열로 작성한다. 각 객체는 alternative_id, alternative_name, "
            "analysis 키를 가지며, analysis에는 원본 flow_direction과 "
            "interventions를 자연스러운 행정 보고서 문장으로 풀어 쓴다. "
            "이동 방향이 혼잡과 운영에 미칠 수 있는 영향 및 확인사항을 "
            "설명하되 facts에 없는 효과나 수치를 단정하지 않는다. "
            "evidence_notes는 retrieved_evidence의 각 source_id를 한 번씩 "
            "포함하는 객체 배열로 작성한다. 각 객체는 source_id와 summary "
            "키를 가지며, summary에는 해당 근거의 핵심 내용과 이번 정책 "
            "검토에 활용되는 이유를 1~2문장으로 설명한다. 수식이나 원문을 "
            "그대로 복사하지 말고 retrieved_evidence에 없는 내용을 만들지 않는다. "
            "emergency_response는 문자열 배열로 작성한다. facts의 어느 대안에도 "
            "intervention_type이 event_trigger인 항목이 없으면 반드시 빈 배열을 "
            "반환한다. 있을 때만 상정된 이벤트, 대피 인원, 최대 밀집 구역, "
            "위험점수 변화를 근거로 담당자가 점검할 사항을 작성하되, 시뮬레이션이 "
            "산출하지 않은 대응 절차나 법정 기준을 지어내지 않는다."
        )
        response = OpenAI().chat.completions.create(
            model=os.getenv(
                "OPENAI_MODEL",
                "gpt-4.1-mini",
            ),
            temperature=0.1,
            response_format={
                "type": "json_object"
            },
            messages=[
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        facts,
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        payload: dict[str, Any] = json.loads(
            response.choices[0].message.content
            or "{}"
        )
        return Narrative(
            report_title=(
                self._normalize_title(
                    payload.get("report_title"),
                    request.report_title,
                )
                if request.generate_report_title
                else request.report_title
            ),
            executive_summary=str(
                payload["executive_summary"]
            ).strip(),
            current_issue=str(
                payload["current_issue"]
            ).strip(),
            recommendation_rationale=str(
                payload["recommendation_rationale"]
            ).strip(),
            implementation_plan=self._normalize_string_list(
                payload["implementation_plan"],
                "implementation_plan",
            ),
            limitations=self._normalize_string_list(
                payload["limitations"],
                "limitations",
            ),
            flow_analysis=self._normalize_flow_analysis(
                payload.get("flow_analysis"),
                request,
            ),
            evidence_notes=self._normalize_evidence_notes(
                payload.get("evidence_notes"),
                evidence,
            ),
            # LLM이 이 키를 빠뜨리거나 형식을 어겨도 이벤트가 있는 시나리오에서는
            # 절이 사라지지 않도록, 전달받은 값에서 직접 만든 템플릿 결과를
            # 바닥으로 깐다. 반대로 이벤트가 없으면 템플릿도 빈 목록이므로
            # LLM이 지어낸 대응 방안이 섞여 들어가지 않는다.
            emergency_response=self._normalize_emergency_response(
                payload.get("emergency_response"),
                request,
                assessments,
            ),
        )

    def _normalize_emergency_response(
        self,
        value: Any,
        request: ReportRequest,
        assessments: list[AlternativeAssessment],
    ) -> list[str]:
        """LLM의 위험 이벤트 대응 항목을 검증하고 필요하면 템플릿으로 대체한다."""

        fallback = self._template_emergency_response(
            request,
            assessments,
        )
        if not fallback:
            # 상정된 이벤트가 없는 시나리오. LLM이 무엇을 반환했든 절을 만들지 않는다.
            return []

        if value is None:
            return fallback
        try:
            return self._normalize_string_list(
                value,
                "emergency_response",
            )
        except (TypeError, ValueError):
            return fallback

    def status(self) -> dict[str, Any]:
        return {
            "configured_mode": os.getenv(
                "NARRATIVE_MODE",
                "template",
            ),
            "last_mode": self.last_mode,
            "last_error": self.last_error,
        }
