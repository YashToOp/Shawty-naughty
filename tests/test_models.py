import pytest
from pydantic import ValidationError

from app.models import (
    Criterion,
    HumanOverride,
    Question,
    QuestionEvaluation,
    QuestionResult,
    Rubric,
    SubmissionReport,
    Transcript,
)


def make_question(qid="1", max_marks=4.0):
    return Question(
        id=qid,
        question_text="State Newton's second law.",
        max_marks=max_marks,
        criteria=[
            Criterion(id=f"{qid}-a", description="States the law", marks=2),
            Criterion(id=f"{qid}-b", description="Gives F = ma", marks=2),
        ],
    )


def test_rubric_total_marks():
    rubric = Rubric(title="Quiz", questions=[make_question("1"), make_question("2")])
    assert rubric.total_marks == 8


def test_criteria_must_sum_to_max_marks():
    with pytest.raises(ValidationError, match="criteria marks sum"):
        Question(
            id="1",
            question_text="Q",
            max_marks=5,
            criteria=[Criterion(id="c", description="d", marks=2)],
        )


def test_question_without_criteria_is_valid():
    q = Question(id="1", question_text="Q", max_marks=5)
    assert q.criteria == []


def test_final_marks_prefers_override():
    evaluation = QuestionEvaluation(
        question_id="1",
        marks_awarded=2.0,
        max_marks=4.0,
        criteria=[],
        overall_comment="ok",
        confidence="high",
        needs_human_review=False,
    )
    result = QuestionResult(evaluation=evaluation)
    assert result.final_marks == 2.0
    result.override = HumanOverride(marks_awarded=3.5, reason="partial credit", reviewer="t")
    assert result.final_marks == 3.5


def test_report_build_totals_and_flags():
    rubric = Rubric(title="Quiz", questions=[make_question("1"), make_question("2")])
    transcript = Transcript(student_identifier="A123", answers=[])

    def result(qid, marks, review):
        return QuestionResult(
            evaluation=QuestionEvaluation(
                question_id=qid,
                marks_awarded=marks,
                max_marks=4.0,
                criteria=[],
                overall_comment="",
                confidence="low" if review else "high",
                needs_human_review=review,
            )
        )

    report = SubmissionReport.build(rubric, transcript, [result("1", 3.0, False), result("2", 1.0, True)])
    assert report.total_awarded == 4.0
    assert report.total_available == 8.0
    assert report.questions_flagged_for_review == 1
    assert report.student_identifier == "A123"
