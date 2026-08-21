"""knowledge/source_docs의 PDF가 인덱스에 얼마나 반영되는지, 안 되면 왜인지 진단한다.

build_rag_index.py 는 페이지 단위로 텍스트를 뽑은 뒤 세 가지 필터로 걸러낸다.
이 스크립트는 그 필터 중 무엇이 페이지를 탈락시켰는지 문서별로 집계만 한다
(임베딩 호출 없음 = 비용 0).

  python scripts/diagnose_rag_sources.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import importlib.util

spec = importlib.util.spec_from_file_location("brix", ROOT / "scripts" / "build_rag_index.py")
brix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(brix)

SOURCE_DIR = ROOT / "knowledge" / "source_docs"

REASON_EMPTY = "본문 없음(텍스트 레이어 X = 스캔 이미지)"
REASON_SHORT = "120자 미만(표지·간지·도표만)"
REASON_MATH = "수식문자 4개 이상"
REASON_NOISE = "노이즈 줄만 남음(쪽번호·STEP·로마숫자)"
REASON_OK = "정상 반영"


def classify(page) -> tuple[str, int, int]:
    """(사유, 원문길이, 정제후길이)"""
    raw = page.get_text().strip()
    kept = brix.extract_readable_page_text(page)
    if kept:
        return REASON_OK, len(raw), len(kept)
    if not raw:
        return REASON_EMPTY, 0, 0

    # 어떤 필터에 걸렸는지 재현
    paragraphs = []
    for block in page.get_text("blocks", sort=True):
        lines = [brix.clean_text(l) for l in str(block[4]).splitlines()]
        readable = [l for l in lines if not brix._is_noise_line(l)]
        p = brix.clean_text(" ".join(readable))
        if p:
            paragraphs.append(p)
    compact = brix.clean_text("\n".join(paragraphs))
    if not compact:
        return REASON_NOISE, len(raw), 0
    if len(compact) < 120:
        return REASON_SHORT, len(raw), len(compact)
    if len(brix.MATH_ALPHANUMERIC_RE.findall(compact)) >= 4:
        return REASON_MATH, len(raw), len(compact)
    return "미상", len(raw), len(compact)


def main() -> None:
    pdfs = sorted(SOURCE_DIR.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"PDF가 없습니다: {SOURCE_DIR}")

    grand_pages = grand_kept = 0
    for path in pdfs:
        doc = fitz.open(path)
        tally: dict[str, int] = {}
        raw_total = 0
        for page in doc:
            reason, raw_len, _ = classify(page)
            tally[reason] = tally.get(reason, 0) + 1
            raw_total += raw_len
        pages = len(doc)
        kept = tally.get(REASON_OK, 0)
        grand_pages += pages
        grand_kept += kept

        pct = kept / pages * 100 if pages else 0
        flag = "  ⚠️" if pct < 50 else ""
        print(f"\n{path.name}{flag}")
        print(f"  {pages}쪽 중 {kept}쪽 반영 ({pct:.0f}%)  ·  추출 원문 {raw_total:,}자  ·  {path.stat().st_size/1024/1024:.1f}MB")
        for reason, n in sorted(tally.items(), key=lambda kv: -kv[1]):
            if reason == REASON_OK:
                continue
            print(f"    - {n:4d}쪽  {reason}")
        doc.close()

    print(f"\n{'='*70}")
    print(f"전체: {grand_pages}쪽 중 {grand_kept}쪽 반영 ({grand_kept/grand_pages*100:.0f}%)")


if __name__ == "__main__":
    main()
