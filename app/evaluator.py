"""Stage 2: grade the transcript against the human-authored rubric.

Each question is evaluated in its own request so rationales stay focused and
a single bad response can't corrupt the whole paper. The rubric rides in the
system prompt behind a cache breakpoint, so after the first question the
shared prefix is served from the prompt cache at ~10% of input cost.

Marks are clamped server-side: the model explains and proposes, but the
arithmetic invariants (0 <= criterion marks <= available, question total =
sum of criteria) are enforced in code. Any clamp flags the question for
human review.
"""

import anthropic

from .config import ANTHROPIC_MODEL, MAX_OUTPUT_TOKENS
from .models import (
    CriterionEvaluation,
    Question,
    QuestionEvaluation,
    QuestionResult,
    Rubric,
    TranscribedAnswer,
    Transcript,
)


class EvaluationRefused(Exception):
    """Raised when the model declines to grade a question."""


GRADING_SYSTEM = """You are an experienced examiner marking student answer sheets against a fixed rubric.

Rules:
- Award marks only for what the rubric's criteria describe. Do not reward or penalize anything outside the rubric.
- Judge the substance of the answer, not spelling, grammar, or handwriting artifacts, unless a criterion explicitly requires them.
- For every criterion, quote the exact words from the student's answer that earned the marks. If nothing in the answer addresses the criterion, award 0 for it with an empty evidence list.
- Partial credit is allowed within a criterion when the answer partially satisfies it; explain the shortfall in the rationale.
- The transcript may contain [illegible] markers. Never assume illegible content is correct or incorrect - if it could plausibly change the marks, lower your confidence and set needs_human_review to true.
- Set needs_human_review to true whenever your confidence is not high, the answer is ambiguous relative to the rubric, or the rubric does not cleanly cover what the student wrote.
- Be consistent: identical answers must always receive identical marks."""


def _grading_context(rubric: Rubric) -> list[dict]:
    """System blocks: stable instructions + rubric, cached across question calls."""
    return [
        {"type": "text", "text": GRADING_SYSTEM},
        {
            "type": "text",
            "text": "The full rubric for this exam:\n\n" + rubric.model_dump_json(indent=2),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _question_prompt(question: Question, answer: TranscribedAnswer) -> str:
    parts = [
        f"Grade question {question.id}.",
        f"\nQuestion text:\n{question.question_text}",
        f"\nMaximum marks: {question.max_marks}",
    ]
    if question.model_answer:
        parts.append(f"\nExaminer's model answer:\n{question.model_answer}")
    parts.append(
        "\nCriteria:\n" + "\n".join(
            f"- [{c.id}] ({c.marks} marks) {c.description}" for c in question.criteria
        )
    )
    parts.append(f"\nStudent's answer (legibility: {answer.legibility}):\n{answer.answer_text}")
    if answer.transcription_notes:
        parts.append(f"\nTranscriber's notes: {answer.transcription_notes}")
    parts.append("\nReturn the evaluation for this question only.")
    return "\n".join(parts)


def _missing_answer_evaluation(question: Question) -> QuestionEvaluation:
    return QuestionEvaluation(
        question_id=question.id,
        marks_awarded=0.0,
        max_marks=question.max_marks,
        criteria=[
            CriterionEvaluation(
                criterion_id=c.id,
                met="not_met",
                marks_awarded=0.0,
                rationale="No answer for this question was found in the transcript.",
                evidence=[],
            )
            for c in question.criteria
        ],
        overall_comment="No answer found in the transcript for this question.",
        confidence="low",
        needs_human_review=True,
    )


def sanitize(evaluation: QuestionEvaluation, question: Question) -> QuestionEvaluation:
    """Enforce arithmetic invariants; flag for review if anything was corrected."""
    clamped = False
    available = {c.id: c.marks for c in question.criteria}

    for crit in evaluation.criteria:
        limit = available.get(crit.criterion_id, question.max_marks)
        bounded = min(max(crit.marks_awarded, 0.0), limit)
        if bounded != crit.marks_awarded:
            crit.marks_awarded = bounded
            clamped = True

    if question.criteria:
        recomputed = round(sum(c.marks_awarded for c in evaluation.criteria), 4)
        if abs(recomputed - evaluation.marks_awarded) > 1e-6:
            evaluation.marks_awarded = recomputed
            clamped = True
    else:
        bounded = min(max(evaluation.marks_awarded, 0.0), question.max_marks)
        if bounded != evaluation.marks_awarded:
            evaluation.marks_awarded = bounded
            clamped = True

    evaluation.max_marks = question.max_marks
    if clamped:
        evaluation.needs_human_review = True
        evaluation.confidence = "low"
    return evaluation


def evaluate_question(client: anthropic.Anthropic, rubric: Rubric,
                      question: Question,
                      answer: TranscribedAnswer) -> QuestionEvaluation:
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=_grading_context(rubric),
        messages=[{"role": "user", "content": _question_prompt(question, answer)}],
        output_format=QuestionEvaluation,
    )
    if response.stop_reason == "refusal":
        raise EvaluationRefused(
            f"The model declined to grade question {question.id}."
        )
    if response.parsed_output is None:
        raise RuntimeError(f"Question {question.id}: no parseable evaluation returned.")
    return sanitize(response.parsed_output, question)


def evaluate_submission(client: anthropic.Anthropic, rubric: Rubric,
                        transcript: Transcript) -> list[QuestionResult]:
    answers_by_id = {a.question_id: a for a in transcript.answers}
    results: list[QuestionResult] = []

    for question in rubric.questions:
        answer = answers_by_id.get(question.id)
        if answer is None:
            evaluation = _missing_answer_evaluation(question)
        else:
            evaluation = evaluate_question(client, rubric, question, answer)
            if answer.legibility != "clear":
                evaluation.needs_human_review = True
        results.append(QuestionResult(evaluation=evaluation))

    return results
