"""Pydantic schemas for rubrics, transcripts, evaluations and jobs.

The Transcript and QuestionEvaluation models double as the structured-output
schemas sent to Claude, so their field descriptions are written for the model
as much as for human readers.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Rubric (authored by a human examiner)
# ---------------------------------------------------------------------------

class Criterion(BaseModel):
    id: str
    description: str
    marks: float


class Question(BaseModel):
    id: str
    question_text: str
    max_marks: float
    model_answer: Optional[str] = None
    criteria: list[Criterion] = Field(default_factory=list)

    @model_validator(mode="after")
    def criteria_must_sum_to_max_marks(self) -> "Question":
        if self.criteria:
            total = sum(c.marks for c in self.criteria)
            if abs(total - self.max_marks) > 1e-6:
                raise ValueError(
                    f"Question {self.id!r}: criteria marks sum to {total}, "
                    f"but max_marks is {self.max_marks}"
                )
        return self


class Rubric(BaseModel):
    id: Optional[str] = None
    title: str
    instructions: Optional[str] = None
    questions: list[Question]

    @property
    def total_marks(self) -> float:
        return sum(q.max_marks for q in self.questions)


# ---------------------------------------------------------------------------
# Transcript (structured output of the OCR stage)
# ---------------------------------------------------------------------------

class TranscribedAnswer(BaseModel):
    question_id: str = Field(
        description="The rubric question id this answer belongs to."
    )
    answer_text: str = Field(
        description=(
            "The student's answer transcribed verbatim, preserving spelling and "
            "layout. Use [illegible] for segments that cannot be read."
        )
    )
    legibility: Literal["clear", "partial", "illegible"] = Field(
        description=(
            "clear = fully readable; partial = some segments unreadable; "
            "illegible = mostly or entirely unreadable."
        )
    )
    transcription_notes: Optional[str] = Field(
        default=None,
        description=(
            "Anything a human re-checker should know: crossed-out work, arrows "
            "reordering paragraphs, diagrams that could not be transcribed, etc."
        ),
    )


class Transcript(BaseModel):
    student_identifier: Optional[str] = Field(
        default=None,
        description="Student name / roll number if visible on the sheet, else null.",
    )
    answers: list[TranscribedAnswer]
    unmatched_content: Optional[str] = Field(
        default=None,
        description=(
            "Any written content that could not be matched to a rubric question, "
            "transcribed verbatim."
        ),
    )


# ---------------------------------------------------------------------------
# Evaluation (structured output of the grading stage)
# ---------------------------------------------------------------------------

class CriterionEvaluation(BaseModel):
    criterion_id: str
    met: Literal["fully", "partially", "not_met"]
    marks_awarded: float = Field(
        description="Marks awarded for this criterion, between 0 and the criterion's marks."
    )
    rationale: str = Field(
        description="One or two sentences explaining exactly why these marks were awarded."
    )
    evidence: list[str] = Field(
        description=(
            "Verbatim quotes from the student's answer that support the decision. "
            "Empty if the criterion is not addressed at all."
        )
    )


class QuestionEvaluation(BaseModel):
    question_id: str
    marks_awarded: float
    max_marks: float
    criteria: list[CriterionEvaluation]
    overall_comment: str = Field(
        description="A short, constructive summary of the answer's strengths and gaps."
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "low when the transcript is partly illegible, the answer is ambiguous, "
            "or the rubric does not clearly cover the answer given."
        )
    )
    needs_human_review: bool = Field(
        description="True whenever a human should double-check this question's marks."
    )


class HumanOverride(BaseModel):
    marks_awarded: float
    reason: str
    reviewer: str
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class QuestionResult(BaseModel):
    """A question evaluation plus any human override — the unit shown in reports."""

    evaluation: QuestionEvaluation
    override: Optional[HumanOverride] = None

    @property
    def final_marks(self) -> float:
        if self.override is not None:
            return self.override.marks_awarded
        return self.evaluation.marks_awarded


class SubmissionReport(BaseModel):
    rubric_id: str
    rubric_title: str
    student_identifier: Optional[str] = None
    results: list[QuestionResult]
    total_awarded: float
    total_available: float
    questions_flagged_for_review: int

    @staticmethod
    def build(rubric: Rubric, transcript: Transcript,
              results: list[QuestionResult]) -> "SubmissionReport":
        return SubmissionReport(
            rubric_id=rubric.id or "",
            rubric_title=rubric.title,
            student_identifier=transcript.student_identifier,
            results=results,
            total_awarded=sum(r.final_marks for r in results),
            total_available=rubric.total_marks,
            questions_flagged_for_review=sum(
                1 for r in results
                if r.evaluation.needs_human_review and r.override is None
            ),
        )


# ---------------------------------------------------------------------------
# Job tracking
# ---------------------------------------------------------------------------

JobStatus = Literal["queued", "transcribing", "evaluating", "completed", "failed"]


class Job(BaseModel):
    id: str
    rubric_id: str
    status: JobStatus = "queued"
    error: Optional[str] = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    uploaded_files: list[str] = Field(default_factory=list)
