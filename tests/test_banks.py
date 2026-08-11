"""Question-bank behaviors: set-variant resolution and PYQ dedupe on upload."""

import json
from pathlib import Path

from app import paper_ingest, pipeline, storage, student_pipeline
from app.models import (
    BankQuestion,
    Criterion,
    ExtractedQuestionPaper,
    GeneratedCriteria,
    PaperQuestion,
    QuestionBank,
    SetVariant,
    SheetMetadata,
)
from tests.conftest import FakeClient, FakeParseResponse

SCIENCE_BANK = (Path(__file__).parent.parent / "sample_data" / "banks"
                / "cbse-10-science-2026.json")


def seed_science_bank() -> QuestionBank:
    bank = QuestionBank.model_validate_json(SCIENCE_BANK.read_text())
    return storage.save_bank(bank)


def small_bank() -> QuestionBank:
    return storage.save_bank(QuestionBank(
        board="CBSE", class_level="11", subject="Physics", year=2026,
        title="Physics — Class 11 (2026)", rubric_status="ai_generated",
        questions=[
            BankQuestion(id="B01", question_text="Define instantaneous velocity.",
                         max_marks=2,
                         criteria=[Criterion(id="B01-a", description="Definition", marks=2)]),
            BankQuestion(id="B02", question_text="State the parallelogram law of vector addition.",
                         max_marks=3,
                         criteria=[Criterion(id="B02-a", description="Statement", marks=2),
                                   Criterion(id="B02-b", description="Diagram", marks=1)]),
        ],
        variants=[SetVariant(paper_code="55/1/1",
                             question_map={"1": "B01", "2": "B02"})],
    ))


def test_set_variant_resolves_and_materializes():
    seed_science_bank()
    meta = SheetMetadata(board="CBSE", class_level="10", subject="Science",
                         exam_year=2026, paper_code="31/2/1")

    paper = storage.find_paper(meta)

    assert paper is not None
    assert paper.id == "31-2-1_2026"
    assert paper.source_bank == "cbse-10-science_2026"
    assert len(paper.questions) == 39
    assert sum(q.max_marks for q in paper.questions) == 80
    assert paper.rubric_status == "verified"
    # criteria are relabeled to the set's own numbering
    assert paper.rubric.questions[0].criteria[0].id == "1-a"
    # materialized paper persists, so the next lookup is a plain registry hit
    assert storage.get_paper("31-2-1_2026") is not None

    # sets 1 and 2 shuffle content: at least one slot holds different questions
    paper1 = storage.find_paper(SheetMetadata(
        board="CBSE", class_level="10", subject="Science",
        exam_year=2026, paper_code="31/1/1"))
    texts1 = [q.question_text for q in paper1.questions]
    texts2 = [q.question_text for q in paper.questions]
    assert texts1 != texts2
    assert sorted(texts1) == sorted(texts2)  # same pool, different order


def test_unregistered_set_code_still_pauses():
    seed_science_bank()
    meta = SheetMetadata(board="CBSE", class_level="10", subject=None,
                         exam_year=2026, paper_code="31/4/1")
    assert storage.find_paper(meta) is None


def test_upload_dedupes_against_bank(monkeypatch):
    """A new shuffled set reuses existing rubrics; only the one genuinely new
    question triggers generation."""
    small_bank()
    job = storage.create_job(None, [("sheet.png", b"img")], kind="student")
    job.metadata = SheetMetadata(board="CBSE", class_level="11",
                                 subject="Physics", exam_year=2026,
                                 paper_code="55/2/1")
    storage.update_job(job, status="awaiting_paper")
    storage.save_paper_uploads(job, [("qp.png", b"img")])

    extracted = ExtractedQuestionPaper(
        title="Physics Set 2", paper_code="55/2/1",
        questions=[
            # same PYQs, lightly reworded, shuffled numbering
            PaperQuestion(id="1", question_text="State the parallelogram law of vector addition.",
                          max_marks=3),
            PaperQuestion(id="2", question_text="Define the instantaneous velocity.",
                          max_marks=2),
            # genuinely new question
            PaperQuestion(id="3", question_text="A ball is thrown up at 20 m/s; find the maximum height.",
                          max_marks=3),
        ])

    from app.models import (CriterionEvaluation, QuestionEvaluation,
                            TranscribedAnswer, Transcript)
    transcript = Transcript(answers=[TranscribedAnswer(
        question_id="1", answer_text="...", legibility="clear")])
    evaluation = QuestionEvaluation(
        question_id="1", marks_awarded=3.0, max_marks=3.0,
        criteria=[CriterionEvaluation(criterion_id="1-a", met="fully",
                                      marks_awarded=2.0, rationale="ok", evidence=["..."]),
                  CriterionEvaluation(criterion_id="1-b", met="fully",
                                      marks_awarded=1.0, rationale="ok", evidence=["..."])],
        overall_comment="ok", confidence="high", needs_human_review=False)

    client = FakeClient([
        FakeParseResponse(extracted),
        # exactly ONE generation call - for the new question only
        FakeParseResponse(GeneratedCriteria(criteria=[
            Criterion(id="a", description="Correct equation v^2 = u^2 - 2gh", marks=1),
            Criterion(id="b", description="Substitution with their values", marks=1),
            Criterion(id="c", description="h = 20.4 m with unit", marks=1),
        ])),
        FakeParseResponse(transcript),
        FakeParseResponse(evaluation),
    ])
    monkeypatch.setattr(pipeline, "get_client", lambda: client)

    student_pipeline.resume_with_uploaded_paper(job.id)

    assert storage.get_job(job.id).status == "completed"
    assert client.messages._responses == []  # no extra generation calls

    bank = storage.get_bank("cbse-11-physics_2026")
    assert len(bank.questions) == 3            # grew by exactly one
    assert [v.paper_code for v in bank.variants] == ["55/1/1", "55/2/1"]
    v2 = next(v for v in bank.variants if v.paper_code == "55/2/1")
    assert v2.question_map == {"1": "B02", "2": "B01", "3": "B03"}
    new_q = next(q for q in bank.questions if q.id == "B03")
    assert abs(sum(c.marks for c in new_q.criteria) - 3.0) < 1e-6


def test_match_bank_question_guards_on_marks():
    bank = QuestionBank(
        board="CBSE", class_level="11", subject="Physics", year=2026,
        title="t", questions=[BankQuestion(
            id="B01", question_text="Define instantaneous velocity.", max_marks=2,
            criteria=[Criterion(id="B01-a", description="d", marks=2)])],
    )
    same_text_different_marks = PaperQuestion(
        id="1", question_text="Define instantaneous velocity.", max_marks=3)
    assert paper_ingest.match_bank_question(bank, same_text_different_marks) is None
