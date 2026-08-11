"""Integrity checks for every paper bundled in sample_data/papers.

These are the guarantees behind 'rubrics correct and intact': every seeded
paper must parse, its rubric must cover exactly its questions, criteria must
sum to each question's marks (also enforced by the model validator), and the
paper totals must match the board's published structure.
"""

import json
from pathlib import Path

import pytest

from app.models import Paper

PAPERS_DIR = Path(__file__).parent.parent / "sample_data" / "papers"
PAPER_FILES = sorted(PAPERS_DIR.glob("*.json"))

EXPECTED_TOTALS = {
    "1-1-1_2026": 80,    # CBSE 12 English Core
    "31-1-1_2026": 80,   # CBSE 10 Science
}


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
        assert abs(total - rq.max_marks) < 1e-6, f"Q{pq.id}: criteria sum {total} != {rq.max_marks}"
        for c in rq.criteria:
            assert c.id.startswith(f"{rq.id}-"), f"criterion {c.id} not namespaced to Q{rq.id}"
            assert c.marks > 0


@pytest.mark.parametrize("path", PAPER_FILES, ids=lambda p: p.stem)
def test_paper_totals_match_board_structure(path):
    paper = Paper.model_validate_json(path.read_text())
    from app.storage import paper_storage_id
    key = paper_storage_id(paper)
    assert key in EXPECTED_TOTALS, f"add expected total for new sample paper {key}"
    assert sum(q.max_marks for q in paper.questions) == EXPECTED_TOTALS[key]


def test_science_2026_follows_new_sectional_pattern():
    """The real Feb-2026 paper structure: 39 questions, Biology 16Q/30M,
    Chemistry 13Q/25M, Physics 10Q/25M."""
    paper = Paper.model_validate_json(
        (PAPERS_DIR / "cbse-10-science-2026.json").read_text()
    )
    assert len(paper.questions) == 39

    by_section = {}
    for q in paper.questions:
        by_section.setdefault(q.section, []).append(q)

    bio = by_section["A (Biology)"]
    chem = by_section["B (Chemistry)"]
    phys = by_section["C (Physics)"]
    assert (len(bio), sum(q.max_marks for q in bio)) == (16, 30)
    assert (len(chem), sum(q.max_marks for q in chem)) == (13, 25)
    assert (len(phys), sum(q.max_marks for q in phys)) == (10, 25)
