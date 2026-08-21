"""knowledge/source_docs의 PDF를 청크화하고 OpenAI embedding 인덱스를 생성한다."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import fitz
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
SOURCE_DIR = ROOT / "knowledge" / "source_docs"
OUTPUT_PATH = ROOT / "knowledge" / "vector_index.json"

MATH_ALPHANUMERIC_RE = re.compile(
    r"[\U0001D400-\U0001D7FF]"
)
NOISE_LINE_RE = re.compile(
    r"^(?:\d+|STEP\s*\d+|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)$",
    re.IGNORECASE,
)

# 페이지를 본문으로 인정하는 최소 글자 수(정제 후 기준).
MIN_PAGE_CHARS = 45
# 같은 문서에서 이 횟수 이상 똑같이 반복되면 머리말·꼬리말로 본다.
REPEAT_THRESHOLD = 3


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_noise_line(line: str) -> bool:
    """수식 조각·페이지 번호처럼 검색 근거로 쓰기 어려운 줄을 판별한다."""

    if not line or NOISE_LINE_RE.fullmatch(line):
        return True
    if len(MATH_ALPHANUMERIC_RE.findall(line)) >= 2:
        return True
    return False


def extract_readable_page_text(
    page: fitz.Page,
) -> str:
    """PDF 블록 순서를 유지하며 사람이 읽을 수 있는 본문만 추출한다."""

    paragraphs: list[str] = []
    for block in page.get_text(
        "blocks",
        sort=True,
    ):
        block_text = str(block[4])
        lines = [
            clean_text(line)
            for line in block_text.splitlines()
        ]
        readable_lines = [
            line
            for line in lines
            if not _is_noise_line(line)
        ]
        paragraph = clean_text(
            " ".join(readable_lines)
        )
        if paragraph:
            paragraphs.append(paragraph)

    text = "\n".join(paragraphs)
    compact = clean_text(text)

    # 표지·간지처럼 실질 내용이 없는 페이지는 출처 목록에 노출하지 않는다.
    #
    # 2026-08-20: 기준을 120자에서 45자로 낮춤. 안전관리 매뉴얼처럼 그림 위주라
    # 페이지당 글자 수가 적은 문서에서, 정작 인용하고 싶은 문단이 기준에 아슬아슬
    # 미달해 통째로 버려지고 있었다(예: "대피훈련을 해보세요..." 100자,
    # "소방시설 및 피난설비 - 경보설비/화재감지기/스프링클러..." 114자).
    #
    # 기준만 낮추면 반복되는 탐색 머리말이 대량 유입되므로, 아래 drop_repeated_pages가
    # 문서 단위로 그것을 제거하는 것과 반드시 함께 동작해야 한다.
    if len(compact) < MIN_PAGE_CHARS:
        return ""

    math_count = len(
        MATH_ALPHANUMERIC_RE.findall(compact)
    )
    if math_count >= 4:
        return ""

    return text


def drop_repeated_pages(pages: list[str]) -> list[str]:
    """한 문서 안에서 반복되는 머리말·꼬리말만 남은 페이지를 비운다.

    본문이 이미지로 들어간 PDF는 페이지마다 탐색용 머리말
    (예: "매뉴얼개요 기획·설계및준비 실시간모니터링및대응")만 텍스트로 남는다.
    이런 페이지를 인덱스에 넣으면 같은 문장이 수십 개 쌓여 검색 결과를 오염시킨다.

    정제 후 문자열이 같은 페이지가 REPEAT_THRESHOLD회 이상 나오면 머리말로 보고 버린다.
    실제 본문이 여러 페이지에 걸쳐 완전히 동일한 경우는 사실상 없다.
    """

    normalized = [clean_text(p) for p in pages]
    counts: dict[str, int] = {}
    for value in normalized:
        if value:
            counts[value] = counts.get(value, 0) + 1

    return [
        ""
        if value and counts.get(value, 0) >= REPEAT_THRESHOLD
        else page
        for page, value in zip(pages, normalized)
    ]


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def extract_chunks() -> list[dict]:
    items: list[dict] = []
    for pdf_path in sorted(SOURCE_DIR.glob("*.pdf")):
        document = fitz.open(pdf_path)
        # 머리말 판정은 문서 전체를 봐야 하므로 페이지를 먼저 모두 뽑아둔다.
        pages = [
            extract_readable_page_text(page)
            for page in document
        ]
        pages = drop_repeated_pages(pages)
        for page_index, text in enumerate(pages):
            for chunk_index, chunk in enumerate(chunk_text(text)):
                items.append(
                    {
                        "text": chunk,
                        "metadata": {
                            "source_id": f"{pdf_path.stem}-p{page_index + 1}-c{chunk_index + 1}",
                            "title": pdf_path.stem.replace("_", " "),
                            "page": page_index + 1,
                            "filename": pdf_path.name,
                            "document_role": (
                                "writing_guide"
                                if (
                                    "행정업무" in pdf_path.stem
                                    or "공문서" in pdf_path.stem
                                )
                                else "policy_evidence"
                            ),
                        },
                    }
                )
    return items


def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 설정되지 않았습니다.")
    model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    items = extract_chunks()
    if not items:
        raise RuntimeError(f"PDF 원문이 없습니다: {SOURCE_DIR}")

    client = OpenAI()
    batch_size = 64
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        response = client.embeddings.create(
            model=model,
            input=[item["text"] for item in batch],
        )
        for item, embedding in zip(batch, response.data):
            item["embedding"] = embedding.embedding
        print(f"embedded {min(start + batch_size, len(items))}/{len(items)}")

    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "index_version": 2,
                "embedding_model": model,
                "items": items,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"완료: {OUTPUT_PATH} ({len(items)} chunks)")


if __name__ == "__main__":
    main()
