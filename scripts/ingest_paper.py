#!/usr/bin/env python
"""Batch-ingest a question paper into the PYQ question-bank registry.

Point it at the scanned pages (images or PDF) of one paper set:

    python scripts/ingest_paper.py pages/*.png \
        --board CBSE --class-level 10 --subject "Mathematics (Standard)" \
        --year 2026 [--code "430/1/1"]

The pipeline extracts every question, dedupes against the subject-year bank
(questions already registered from other set variants are reused with their
existing rubrics), generates marking-scheme criteria only for genuinely new
questions, registers the set as a bank variant, and materializes the paper
so student sheets with this code resolve instantly.

Requires ANTHROPIC_API_KEY. Ingest one set per invocation; each successive
set of the same subject-year is cheaper as the bank fills.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paper_ingest, storage  # noqa: E402
from app.models import Paper, SheetMetadata  # noqa: E402


def ingest(files: list[Path], meta: SheetMetadata, client,
           code_override: str | None = None) -> Paper | None:
    """Run the full ingestion for one paper set. Returns the materialized
    paper, or None if the set was already registered."""
    print(f"Reading {len(files)} page(s)...")
    extracted = paper_ingest.extract_questions(client, files)

    if code_override:
        extracted.paper_code = code_override
    code = extracted.paper_code or meta.paper_code
    if not code:
        raise SystemExit(
            "No paper code found on the paper and none provided via --code")
    meta.paper_code = code

    if meta.exam_year:
        hit = storage.find_bank_variant(code, meta.exam_year)
        if hit is not None:
            print(f"Set {code} ({meta.exam_year}) is already registered "
                  f"in bank {hit[0].id} - nothing to do.")
            return None

    bank = storage.find_bank_for(meta)
    known_ids = {q.id for q in bank.questions} if bank else set()
    print(f"Extracted {len(extracted.questions)} questions "
          f"({sum(q.max_marks for q in extracted.questions):g} marks). "
          + (f"Deduping against bank {bank.id} "
             f"({len(bank.questions)} known questions)..." if bank
             else "No bank for this subject-year yet - creating one."))

    bank, variant = paper_ingest.ingest_into_bank(client, meta, extracted, bank)
    storage.save_bank(bank)
    paper = storage.materialize_variant(bank, variant)

    reused = sum(1 for bid in variant.question_map.values() if bid in known_ids)
    new = len(variant.question_map) - reused
    print(
        f"\nDone. Set {variant.paper_code} registered in bank {bank.id}\n"
        f"  questions reused from bank : {reused} (rubrics NOT regenerated)\n"
        f"  new questions added        : {new} (rubrics generated)\n"
        f"  bank now holds             : {len(bank.questions)} questions, "
        f"{len(bank.variants)} set variant(s): "
        f"{', '.join(v.paper_code for v in bank.variants)}\n"
        f"  materialized paper         : {paper.id} "
        f"({sum(q.max_marks for q in paper.questions):g} marks, "
        f"rubric_status={paper.rubric_status})"
    )
    return paper


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="page images or PDF of ONE paper set")
    ap.add_argument("--board", required=True, help='e.g. "CBSE"')
    ap.add_argument("--class-level", required=True, help='e.g. "10"')
    ap.add_argument("--subject", required=True, help='e.g. "Mathematics (Standard)"')
    ap.add_argument("--year", required=True, type=int, help="e.g. 2026")
    ap.add_argument("--code", default=None,
                    help="paper/set code; overrides what is read off the paper")
    args = ap.parse_args()

    paths = [Path(f) for f in args.files]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise SystemExit(f"File(s) not found: {', '.join(map(str, missing))}")

    meta = SheetMetadata(board=args.board, class_level=args.class_level,
                         subject=args.subject, exam_year=args.year,
                         paper_code=args.code)

    import anthropic
    ingest(paths, meta, anthropic.Anthropic(), code_override=args.code)


if __name__ == "__main__":
    main()
