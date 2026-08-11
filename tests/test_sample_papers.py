"""Integrity checks for everything bundled in sample_data.

These are the guarantees behind 'rubrics correct and intact': every seeded
paper and question bank must parse, rubrics must cover exactly their
questions, criteria must sum to each question's marks, set variants must map
completely onto valid bank questions, and totals must match the board's
published structure.
"""

from pathlib import Path

import pytest

from app.models import Paper, QuestionBank
from app.storage import materialize_variant

SAMPLE = Path(__file__).parent.parent / "sample_data"
PAPER_FILES = sorted((SAMPLE / "papers").glob("*.json"))
BANK_FILES = sorted((SAMPLE / "banks").glob("*.json"))


@pytest.mark.parametrize("path", PAPER_FILES, ids=lambda p: p.stem)
def test_paper_parses_and_rubric_is_intact(path):
    paper = Paper.model_validate_json(path.read_text())

    assert paper.rubric is not None, "seeded papers must ship with a rubric"
    assert paper.rubric_status in ("verified", "ai_generated")

    question_ids = [q.id for q in paper.questions]
    rubric_ids = [q.id for q in paper.rubric.questions]
    assert question_ids == rubric_ids, "rubric must cover exactly the paper's questions, in order"

    for pq, rq in zip(paper.questions, paper.rubric.questions):
        assert pq.max_marks == rq.max_marks, f"Q{pq.id}: paper/rubric marks disagree"
        assert rq.criteria, f"Q{pq.id}: rubric question has no criteria"
        total = sum(c.marks for c in rq.criteria)
        assert abs(total - rq.max_marks) < 1e-6
        for c in rq.criteria:
            assert c.id.startswith(f"{rq.id}-")
            assert c.marks > 0


@pytest.mark.parametrize("path", BANK_FILES, ids=lambda p: p.stem)
def test_bank_parses_and_rubrics_are_intact(path):
    bank = QuestionBank.model_validate_json(path.read_text())

    assert bank.rubric_status in ("verified", "ai_generated")
    assert bank.variants, "seeded banks must register at least one set variant"

    ids = [q.id for q in bank.questions]
    assert len(ids) == len(set(ids)), "bank question ids must be unique"
    for bq in bank.questions:
        assert bq.criteria, f"{bq.id}: bank question has no criteria"
        total = sum(c.marks for c in bq.criteria)
        assert abs(total - bq.max_marks) < 1e-6, f"{bq.id}: criteria sum {total} != {bq.max_marks}"

    for variant in bank.variants:
        mapped = list(variant.question_map.values())
        assert set(mapped) <= set(ids), f"{variant.paper_code}: maps to unknown bank ids"
        assert len(mapped) == len(set(mapped)), f"{variant.paper_code}: repeats a bank question"
        assert len(mapped) == len(bank.questions), \
            f"{variant.paper_code}: does not cover the full paper"


def test_english_2026_totals():
    paper = Paper.model_validate_json(
        (SAMPLE / "papers" / "cbse-12-english-core-2026.json").read_text())
    assert sum(q.max_marks for q in paper.questions) == 80


def test_science_2026_bank_follows_sectional_pattern():
    """The real Feb-2026 structure: 39 questions - Biology 16Q/30M,
    Chemistry 13Q/25M, Physics 10Q/25M - preserved by every set variant."""
    bank = QuestionBank.model_validate_json(
        (SAMPLE / "banks" / "cbse-10-science-2026.json").read_text())
    assert len(bank.questions) == 39

    by_section = {}
    for q in bank.questions:
        by_section.setdefault(q.section, []).append(q)
    assert (len(by_section["A (Biology)"]),
            sum(q.max_marks for q in by_section["A (Biology)"])) == (16, 30)
    assert (len(by_section["B (Chemistry)"]),
            sum(q.max_marks for q in by_section["B (Chemistry)"])) == (13, 25)
    assert (len(by_section["C (Physics)"]),
            sum(q.max_marks for q in by_section["C (Physics)"])) == (10, 25)

    assert [v.paper_code for v in bank.variants] == ["31/1/1", "31/2/1", "31/3/1"]

    # every set materializes to a valid 80-mark paper, and each shuffled slot
    # keeps the mark value of its position
    marks_by_slot = {}
    for variant in bank.variants:
        paper = materialize_variant(bank, variant)
        assert sum(q.max_marks for q in paper.questions) == 80
        for q in paper.questions:
            marks_by_slot.setdefault(q.id, set()).add(q.max_marks)
    assert all(len(v) == 1 for v in marks_by_slot.values()), \
        "a question slot changed its mark value between sets"

    # the sets genuinely shuffle content
    v1, v2 = bank.variants[0].question_map, bank.variants[1].question_map
    assert any(v1[k] != v2[k] for k in v1)
