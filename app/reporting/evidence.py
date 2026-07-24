"""PDF 원문 벡터 인덱스를 이용한 보고서 근거 검색 계층."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

from app.schemas.report_models import EvidenceItem, ReportRequest

MATH_ALPHANUMERIC_RE = re.compile(
    r"[\U0001D400-\U0001D7FF]"
)
WRITING_GUIDE_TITLE_MARKERS = (
    "행정업무운영",
    "공문서 쓰기",
)


def _query_text(request: ReportRequest) -> str:
    """시나리오 조건과 예측 결과를 벡터 검색용 질의로 변환한다."""

    interventions = [
        intervention.description
        for alternative in request.alternatives
        for intervention in alternative.interventions
    ]

    metrics: list[str] = []

    for alternative in [
        request.baseline,
        *request.alternatives,
    ]:
        values = alternative.metrics.model_dump(
            exclude_none=True
        )

        metrics.append(
            f"{alternative.alternative_name}: {values}"
        )

    return "\n".join(
        [
            f"시나리오 유형: {request.scenario_type.value}",
            f"보고서 제목: {request.report_title}",
            f"검토 질문: {request.decision_question}",
            (
                "정책 변경: "
                f"{'; '.join(interventions) or '변경사항 없음'}"
            ),
            f"예측 결과: {'; '.join(metrics)}",
            (
                "검색 목적: 전통시장 혼잡도, 안전관리, "
                "정책 대안의 영향과 현장 대응 근거"
            ),
        ]
    )


def _is_policy_evidence(
    item: dict[str, Any],
) -> bool:
    metadata = item.get("metadata", {})
    role = metadata.get("document_role")
    if role is not None:
        return role == "policy_evidence"

    title = str(
        metadata.get(
            "title",
            metadata.get("filename", ""),
        )
    )
    return not any(
        marker in title
        for marker in WRITING_GUIDE_TITLE_MARKERS
    )


def _clean_excerpt(text: str) -> str:
    """보고서에 표시할 검색 원문을 한 문단으로 정리한다."""

    cleaned = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()
    cleaned = re.sub(
        r"^\d+\s+",
        "",
        cleaned,
    )

    if len(cleaned) > 600:
        shortened = cleaned[:600]
        sentence_end = max(
            shortened.rfind("."),
            shortened.rfind("다."),
        )
        if sentence_end >= 180:
            cleaned = shortened[
                : sentence_end + 1
            ]
        else:
            cleaned = shortened.rstrip() + "…"
    return cleaned


def _is_readable_excerpt(text: str) -> bool:
    cleaned = _clean_excerpt(text)
    if len(cleaned) < 80:
        return False
    if (
        len(
            MATH_ALPHANUMERIC_RE.findall(
                cleaned
            )
        )
        >= 3
    ):
        return False
    return True


class OpenAIVectorEvidenceProvider:
    """로컬 벡터 인덱스에서 관련 PDF 청크를 검색한다."""

    def __init__(self, vector_index_path: Path):
        self.vector_index_path = vector_index_path

        if not vector_index_path.exists():
            raise FileNotFoundError(
                "벡터 인덱스가 없습니다: "
                f"{vector_index_path}. "
                "scripts/build_rag_index.py를 먼저 실행하세요."
            )

        payload = json.loads(
            vector_index_path.read_text(
                encoding="utf-8"
            )
        )

        self.items: list[dict[str, Any]] = payload.get(
            "items",
            [],
        )

        if not self.items:
            raise ValueError(
                "벡터 인덱스에 검색할 청크가 없습니다."
            )

        self.model = payload.get(
            "embedding_model",
            os.getenv(
                "OPENAI_EMBEDDING_MODEL",
                "text-embedding-3-small",
            ),
        )

    def retrieve(
        self,
        request: ReportRequest,
        limit: int = 5,
    ) -> list[EvidenceItem]:
        """질의와 유사한 상위 문서 청크를 반환한다."""

        from openai import OpenAI

        client = OpenAI()

        response = client.embeddings.create(
            input=[_query_text(request)],
            model=self.model,
        )

        query_vector = response.data[0].embedding

        scored: list[
            tuple[float, dict[str, Any]]
        ] = []

        for item in self.items:
            if not _is_policy_evidence(item):
                continue

            raw_text = str(
                item.get("text", "")
            )
            if not _is_readable_excerpt(
                raw_text
            ):
                continue

            embedding = item.get("embedding")

            if not embedding:
                continue

            score = self._cosine(
                query_vector,
                embedding,
            )

            scored.append((score, item))

        scored.sort(
            key=lambda pair: pair[0],
            reverse=True,
        )

        results: list[EvidenceItem] = []
        minimum_score = float(
            os.getenv(
                "RAG_MIN_VECTOR_SCORE",
                "0.35",
            )
        )

        for score, item in scored:
            if score < minimum_score:
                continue

            metadata = item.get("metadata", {})

            results.append(
                EvidenceItem(
                    source_id=str(
                        metadata.get(
                            "source_id",
                            "unknown",
                        )
                    ),
                    title=str(
                        metadata.get(
                            "title",
                            metadata.get(
                                "filename",
                                "제목 없음",
                            ),
                        )
                    ),
                    page=metadata.get("page"),
                    excerpt=_clean_excerpt(
                        str(
                            item.get("text", "")
                        )
                    ),
                    relevance_tags=[
                        f"vector_score:{score:.4f}"
                    ],
                )
            )
            if len(results) >= limit:
                break

        return results

    @staticmethod
    def _cosine(
        a: list[float],
        b: list[float],
    ) -> float:
        """두 벡터의 코사인 유사도를 계산한다."""

        if len(a) != len(b):
            raise ValueError(
                "질의 벡터와 문서 벡터의 차원이 "
                "일치하지 않습니다."
            )

        dot = sum(
            x * y
            for x, y in zip(a, b)
        )

        norm_a = math.sqrt(
            sum(x * x for x in a)
        )

        norm_b = math.sqrt(
            sum(x * x for x in b)
        )

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)


class EvidenceProvider:
    """보고서 생성 서비스에서 사용하는 RAG 검색 진입점."""

    def __init__(
        self,
        vector_index_path: Path,
    ):
        self.vector_index_path = vector_index_path
        self.provider = OpenAIVectorEvidenceProvider(
            vector_index_path
        )

        self.last_mode = "openai_vector"
        self.last_error: str | None = None

    def retrieve(
        self,
        request: ReportRequest,
        limit: int = 5,
    ) -> list[EvidenceItem]:
        self.last_mode = "openai_vector"
        self.last_error = None

        try:
            return self.provider.retrieve(
                request,
                limit,
            )

        except Exception as exc:
            self.last_error = str(exc)
            raise

    def status(self) -> dict[str, Any]:
        indexed_count = 0
        model: str | None = None

        if self.vector_index_path.exists():
            try:
                payload = json.loads(
                    self.vector_index_path.read_text(
                        encoding="utf-8"
                    )
                )

                indexed_count = len(
                    payload.get("items", [])
                )

                model = payload.get(
                    "embedding_model"
                )

            except Exception as exc:
                self.last_error = str(exc)

        return {
            "configured_mode": "openai_vector",
            "last_mode": self.last_mode,
            "last_error": self.last_error,
            "vector_index_exists": (
                self.vector_index_path.exists()
            ),
            "indexed_chunk_count": indexed_count,
            "embedding_model": model,
        }
