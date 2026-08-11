from app import evaluator
from app.models import (
    Criterion,
    CriterionEvaluation,
    Question,
    QuestionEvaluation,
    Rubric,
    TranscribedAnswer,
    Transcript,
)
from tests.conftest import FakeParseResponse


def make_rubric():
    return Rubric(
        id="r1",
        title="Quiz",
        questions=[
            Question(
                id="1",
                question_text="State Newton's second law.",
                max_marks=4,
                criteria=[
                    Criterion(id="1-a", description="States the law", marks=2),
                    Criterion(id="1-b", description="Gives F = ma", marks=2),
                ],
            )
        ],
    )


def model_evaluation(crit_a=2.0, crit_b=1.0, total=None, confidence="high", review=False):
    return QuestionEvaluation(
        question_id="1",
        marks_awarded=total if total is not None else crit_a + crit_b,
        max_marks=4,
        criteria=[
            CriterionEvaluation(
                criterion_id="1-a", met="fully", marks_awarded=crit_a,
                rationale="States the law correctly.", evidence=["force is mass times acceleration"],
            ),
            CriterionEvaluation(
                criterion_id="1-b", met="partially", marks_awarded=crit_b,
                rationale="Equation partially correct.", evidence=["F = ma"],
            ),
        ],
        overall_comment="Good answer.",
        confidence=confidence,
        needs_human_review=review,
    )


def clear_answer():
    return TranscribedAnswer(
        question_id="1",
        answer_text="force is mass times acceleration, F = ma",
        legibility="clear",
    )


def test_evaluate_submission_happy_path(fake_client_factory):
    client = fake_client_factory([FakeParseResponse(model_evaluation())])
    rubric = make_rubric()
    transcript = Transcript(answers=[clear_answer()])

    results = evaluator.evaluate_submission(client, rubric, transcript)

    assert len(results) == 1
    assert results[0].evaluation.marks_awarded == 3.0
    assert results[0].evaluation.needs_human_review is False
    # rubric context is cached: the system blocks carry a cache_control marker
    system = client.messages.calls[0]["system"]
    assert system[-1]["cache_control"] == {"type": "ephemeral"}


def test_sanitize_clamps_out_of_range_marks(fake_client_factory):
    # model over-awards criterion 1-a (5 > 2 available) and misreports the total
    bad = model_evaluation(crit_a=5.0, crit_b=1.0, total=9.0)
    client = fake_client_factory([FakeParseResponse(bad)])
    transcript = Transcript(answers=[clear_answer()])

    results = evaluator.evaluate_submission(client, make_rubric(), transcript)

    ev = results[0].evaluation
    assert ev.criteria[0].marks_awarded == 2.0  # clamped to criterion max
    assert ev.marks_awarded == 3.0              # recomputed from criteria
    assert ev.needs_human_review is True        # clamping flags for review
    assert ev.confidence == "low"


def test_missing_answer_scores_zero_and_flags(fake_client_factory):
    client = fake_client_factory([])  # no API calls expected
    transcript = Transcript(answers=[])

    results = evaluator.evaluate_submission(client, make_rubric(), transcript)

    ev = results[0].evaluation
    assert ev.marks_awarded == 0.0
    assert ev.needs_human_review is True
    assert client.messages.calls == []


def test_partial_legibility_forces_review(fake_client_factory):
    client = fake_client_factory([FakeParseResponse(model_evaluation())])
    answer = TranscribedAnswer(
        question_id="1",
        answer_text="force is [illegible], F = ma",
        legibility="partial",
    )
    results = evaluator.evaluate_submission(client, make_rubric(), Transcript(answers=[answer]))
    assert results[0].evaluation.needs_human_review is True


def test_refusal_raises(fake_client_factory):
    client = fake_client_factory([FakeParseResponse(None, stop_reason="refusal")])
    transcript = Transcript(answers=[clear_answer()])
    try:
        evaluator.evaluate_submission(client, make_rubric(), transcript)
        assert False, "expected EvaluationRefused"
    except evaluator.EvaluationRefused:
        pass
