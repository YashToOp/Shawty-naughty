"""Stage 1: transcribe scanned answer sheets into a structured transcript.

Claude's vision reads the uploaded pages (images or PDFs) and returns a
Transcript keyed by rubric question ids. Transcription is deliberately
separated from grading so the intermediate artifact can be inspected,
audited, and corrected by a human before any marks are decided.
"""

import base64
from pathlib import Path

import anthropic

from .config import ANTHROPIC_MODEL, MAX_OUTPUT_TOKENS, SUPPORTED_MEDIA_TYPES
from .models import Rubric, Transcript


class TranscriptionRefused(Exception):
    """Raised when the model declines to process the submission."""


TRANSCRIPTION_SYSTEM = """You are a meticulous exam-paper transcriber. You convert scanned, often handwritten, answer sheets into a faithful digital transcript.

Rules:
- Transcribe the student's writing verbatim. Preserve their spelling, grammar, and wording exactly - never correct, complete, or improve an answer.
- Mark any unreadable segment as [illegible] rather than guessing.
- Match each answer to the correct question id from the provided question list, using question numbers written on the sheet and the question text as anchors.
- If work is crossed out, exclude it from answer_text and mention it in transcription_notes.
- Describe diagrams, graphs, or tables briefly in answer_text (e.g. "[diagram: force arrows acting on a block, labeled F and mg]").
- Put any writing you cannot attribute to a question into unmatched_content.
- Never invent content that is not on the page.
- For image pages, report bounding-box regions for each answer: pixel coordinates on that page image, covering all of the student's writing for the answer (including working and diagrams). Use one region per contiguous block; an answer continued elsewhere gets an additional region. Boxes must not include other questions' answers. For PDF pages, leave regions empty."""


def build_page_blocks(paths: list[Path]) -> list[dict]:
    """Convert uploaded files into labeled image/document content blocks.

    Each page is preceded by a text block naming its 0-based index so the
    model can anchor answer regions to the right page.
    """
    blocks: list[dict] = []
    for index, path in enumerate(paths):
        media_type = SUPPORTED_MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            raise ValueError(f"Unsupported file type: {path.name}")
        data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
        blocks.append({"type": "text", "text": f"Page index {index}:"})
        if media_type == "application/pdf":
            blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            })
        else:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            })
    return blocks


def _question_index(rubric: Rubric) -> str:
    lines = [
        f"- id: {q.id} | max marks: {q.max_marks} | question: {q.question_text}"
        for q in rubric.questions
    ]
    return "\n".join(lines)


def transcribe(client: anthropic.Anthropic, rubric: Rubric,
               file_paths: list[Path]) -> Transcript:
    prompt = (
        "Transcribe this answer sheet. The exam contains the following "
        "questions; attribute each answer to the matching question id:\n\n"
        f"{_question_index(rubric)}\n\n"
        "Return the complete transcript."
    )

    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=TRANSCRIPTION_SYSTEM,
        messages=[{
            "role": "user",
            "content": [*build_page_blocks(file_paths), {"type": "text", "text": prompt}],
        }],
        output_format=Transcript,
    )

    if response.stop_reason == "refusal":
        raise TranscriptionRefused(
            "The model declined to transcribe this submission."
        )
    if response.parsed_output is None:
        raise RuntimeError("Transcription returned no parseable output.")
    return response.parsed_output
