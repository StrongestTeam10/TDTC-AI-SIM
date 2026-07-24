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

    # 수식·도형만 남은 페이지는 출처 목록에 노출하지 않는다.
    if len(compact) < 120:
        return ""

    math_count = len(
        MATH_ALPHANUMERIC_RE.findall(compact)
    )
    if math_count >= 4:
        return ""

    return text


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
        for page_index, page in enumerate(document):
            text = extract_readable_page_text(
                page
            )
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
