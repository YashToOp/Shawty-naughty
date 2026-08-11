"""Question-paper ingestion and marking-scheme (rubric) generation.

When a student's paper code isn't in the registry, they upload the question
paper once: we extract every question, and — if the institution has published
no grading guidelines — write a complete marking scheme for each question in
the board's own conventions. Both are saved to the registry, so every later
student with the same paper code skips this entirely.

AI-generated schemes are stored with rubric_status="ai_generated" and shown
as such; an examiner upgrading one to "verified" is a registry edit.
"""

from pathlib import Path

import anthropic

from .config import ANTHROPIC_MODEL, MAX_OUTPUT_TOKENS
from .models import (
    Criterion,
    ExtractedQuestionPaper,
    GeneratedCriteria,
    Paper,
    PaperQuestion,
    Question,
    Rubric,
    SheetMetadata,
)
from .ocr import build_page_blocks


class PaperIngestRefused(Exception):
    pass


EXTRACTION_SYSTEM = """You read scanned examination question papers and produce a faithful structured listing of every question.

Rules:
- One entry per top-level question number, exactly as printed. Fold sub-parts into the parent question's text.
- question_text must preserve everything a marker needs: the task, sub-parts, internal choices ("attempt any five", "A or B"), and word limits. Condense long reading passages to a 2-3 sentence description of the passage plus the questions asked about it.
- max_marks is the total marks for that question number as printed.
- Record the section label (A, B, C ...) when the paper has sections.
- Do not invent questions, marks, or choices that are not printed."""


RUBRIC_SYSTEM = """You are a senior board examiner writing an official-style marking scheme for one examination question at a time.

Rules:
- Split the question's maximum marks into named criteria a marker can apply independently. Marks are in steps of 0.5 and must sum exactly to the maximum.
- Follow the board's own marking conventions where they exist. For CBSE English: writing tasks split as Format / Content / Organisation / Accuracy of language; literature answers split as Content & understanding / Textual evidence / Organisation / Expression; objective and very-short-answer parts get per-item marks.
- Each criterion description must be concrete enough that two different markers would award the same marks ("States any two values conveyed by the poem" - not "understands the poem").
- For questions with internal choice, write criteria that apply to whichever option the student attempted.
- Criterion ids are the question id plus a letter suffix: 5-a, 5-b, ..."""


def extract_questions(client: anthropic.Anthropic,
                      paper_files: list[Path]) -> ExtractedQuestionPaper:
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=EXTRACTION_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                *build_page_blocks(paper_files),
                {"type": "text",
                 "text": "List every question in this paper with its marks."},
            ],
        }],
        output_format=ExtractedQuestionPaper,
    )
    if response.stop_reason == "refusal":
        raise PaperIngestRefused("The model declined to read this question paper.")
    if response.parsed_output is None:
        raise RuntimeError("Question extraction returned no parseable output.")
    return response.parsed_output


def fit_criteria(question_id: str, criteria: list[Criterion],
                 max_marks: float) -> list[Criterion]:
    """Force generated criteria to be well-formed: ids prefixed, marks in
    [0.5, max], summing exactly to max. Never trust model arithmetic."""
    crits = [c for c in criteria if c.marks > 0]
    if not crits:
        return [Criterion(
            id=f"{question_id}-a",
            description="Overall quality of the response against the expected answer",
            marks=max_marks,
        )]

    for i, c in enumerate(crits):
        if not c.id.startswith(f"{question_id}-"):
            c.id = f"{question_id}-{chr(ord('a') + i)}"

    total = sum(c.marks for c in crits)
    if abs(total - max_marks) > 1e-6:
        scale = max_marks / total
        for c in crits:
            c.marks = max(0.5, round(c.marks * scale * 2) / 2)
        residual = round((max_marks - sum(c.marks for c in crits)) * 2) / 2
        if residual:
            largest = max(crits, key=lambda c: c.marks)
            largest.marks += residual
        if any(c.marks <= 0 for c in crits) or \
                abs(sum(c.marks for c in crits) - max_marks) > 1e-6:
            return [Criterion(
                id=f"{question_id}-a",
                description="Overall quality of the response against the expected answer",
                marks=max_marks,
            )]
    return crits


def _paper_context(paper: Paper) -> str:
    return (
        f"Examination: {paper.board} Class {paper.class_level} — {paper.subject} "
        f"({paper.year}), paper code {paper.paper_code}.\n"
        "Question list for context (write the scheme for one question at a time):\n"
        + "\n".join(
            f"- Q{q.id} ({q.max_marks} marks): {q.question_text[:160]}"
            for q in paper.questions
        )
    )


def generate_rubric(client: anthropic.Anthropic, paper: Paper) -> Rubric:
    """Write a full marking scheme, one request per question, paper context cached."""
    system = [
        {"type": "text", "text": RUBRIC_SYSTEM},
        {
            "type": "text",
            "text": _paper_context(paper),
            "cache_control": {"type": "ephemeral"},
        },
    ]

    questions: list[Question] = []
    for pq in paper.questions:
        response = client.messages.parse(
            model=ANTHROPIC_MODEL,
            max_tokens=4000,
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f"Write the marking scheme for question {pq.id} "
                    f"({pq.max_marks} marks):\n\n{pq.question_text}"
                ),
            }],
            output_format=GeneratedCriteria,
        )
        if response.stop_reason == "refusal":
            raise PaperIngestRefused(
                f"The model declined to write a scheme for question {pq.id}."
            )
        generated = response.parsed_output.criteria if response.parsed_output else []
        questions.append(Question(
            id=pq.id,
            question_text=pq.question_text,
            max_marks=pq.max_marks,
            criteria=fit_criteria(pq.id, generated, pq.max_marks),
        ))

    return Rubric(
        id=paper.id,
        title=paper.title,
        instructions=(
            "AI-generated marking scheme (no institutional guidelines were "
            "available for this paper). Award marks strictly per criterion."
        ),
        questions=questions,
    )


def build_paper(meta: SheetMetadata, extracted: ExtractedQuestionPaper) -> Paper:
    """Combine front-page metadata with the extracted question list."""
    code = extracted.paper_code or meta.paper_code or "uncoded"
    subject = meta.subject or extracted.title or "Unknown subject"
    year = meta.exam_year or 0
    return Paper(
        board=meta.board or "Unknown board",
        class_level=meta.class_level or "?",
        subject=subject,
        year=year,
        paper_code=code,
        title=extracted.title
        or f"{subject} — Class {meta.class_level or '?'} ({year})",
        questions=extracted.questions,
    )
