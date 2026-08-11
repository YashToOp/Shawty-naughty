"""Free-stack OCR: Google Cloud Vision pre-pass + text-only segmentation.

Replaces the Claude-vision transcription stage when OCR_PROVIDER=google-vision:

    page images -> Vision DOCUMENT_TEXT_DETECTION  (raw text + boxes + word
                                                    confidences; ~free tier)
                -> numbered line list              (low-confidence lines flagged)
                -> one cheap TEXT-ONLY model call  (segment lines into
                                                    per-question answers)
                -> Transcript                      (regions + legibility
                                                    computed in code)

The judgment split is deliberate: the model only decides which lines belong
to which question. Geometry (answer bounding boxes are unions of Vision's
own line boxes) and legibility floors (from Vision's word confidences) are
computed in code — the same "model judges, code enforces" rule as the
marking arithmetic.

Limits of this path, by design for now:
- Image pages only. PDFs need Vision's async files API; use OCR_PROVIDER=claude.
- One contiguous line range per answer; a continuation elsewhere is noted in
  transcription_notes rather than boxed separately.
"""

import base64
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from . import config, providers
from .config import ANTHROPIC_MODEL, MAX_OUTPUT_TOKENS
from .metadata import METADATA_SYSTEM, MetadataRefused
from .models import (
    AnswerRegion,
    ExtractedQuestionPaper,
    Rubric,
    SheetMetadata,
    TranscribedAnswer,
    Transcript,
)
from .ocr import TranscriptionRefused, _question_index
from .paper_ingest import EXTRACTION_SYSTEM, PaperIngestRefused

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"

OCR_TEXT_NOTE = (
    "\n\nThe page is provided as OCR text produced by a separate OCR engine, "
    "not as an image. OCR misreads are possible: treat garbled or ambiguous "
    "fields as unreadable rather than guessing."
)


# ---------------------------------------------------------------------------
# Google Vision call + response parsing
# ---------------------------------------------------------------------------

class OCRLine(BaseModel):
    """One OCR'd paragraph ("line") with its box and mean word confidence."""

    page_index: int
    text: str
    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float = 1.0


class PageOCR(BaseModel):
    page_index: int
    width: int = 0
    height: int = 0
    lines: list[OCRLine] = Field(default_factory=list)


def _parse_annotation(payload: dict, page_index: int) -> PageOCR:
    """Vision images:annotate response -> lines with boxes and confidences."""
    resp = payload["responses"][0]
    if "error" in resp:
        message = resp["error"].get("message", str(resp["error"]))
        raise RuntimeError(f"Google Vision error: {message}")
    full = resp.get("fullTextAnnotation")
    if not full:
        return PageOCR(page_index=page_index)  # blank page

    page = full["pages"][0]
    lines: list[OCRLine] = []
    for block in page.get("blocks", []):
        for para in block.get("paragraphs", []):
            words, confs = [], []
            for word in para.get("words", []):
                words.append("".join(
                    s.get("text", "") for s in word.get("symbols", [])
                ))
                if "confidence" in word:
                    confs.append(word["confidence"])
            text = " ".join(w for w in words if w).strip()
            if not text:
                continue
            vertices = para.get("boundingBox", {}).get("vertices", [])
            xs = [v.get("x", 0) for v in vertices] or [0]
            ys = [v.get("y", 0) for v in vertices] or [0]
            lines.append(OCRLine(
                page_index=page_index, text=text,
                x1=min(xs), y1=min(ys), x2=max(xs), y2=max(ys),
                confidence=sum(confs) / len(confs) if confs else 1.0,
            ))
    return PageOCR(page_index=page_index, width=page.get("width", 0),
                   height=page.get("height", 0), lines=lines)


def _annotate(http: httpx.Client, api_key: str, path: Path,
              page_index: int) -> PageOCR:
    payload = {"requests": [{
        "image": {"content": base64.standard_b64encode(path.read_bytes()).decode()},
        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
    }]}
    response = http.post(f"{VISION_URL}?key={api_key}", json=payload)
    response.raise_for_status()
    return _parse_annotation(response.json(), page_index)


def _ocr_pages(paths: list[Path]) -> list[PageOCR]:
    api_key = config.GOOGLE_VISION_API_KEY
    if not api_key:
        raise RuntimeError(
            "OCR_PROVIDER=google-vision needs GOOGLE_VISION_API_KEY "
            "(see .env.example)."
        )
    for path in paths:
        if path.suffix.lower() == ".pdf":
            raise ValueError(
                "The Google Vision OCR path handles image pages only. Upload "
                "page images, or set OCR_PROVIDER=claude for PDF submissions."
            )
    with httpx.Client(timeout=60) as http:
        return [_annotate(http, api_key, path, i)
                for i, path in enumerate(paths)]


# ---------------------------------------------------------------------------
# Segmentation: numbered OCR lines -> per-question answers (text model)
# ---------------------------------------------------------------------------

SEGMENTATION_SYSTEM = """You segment the OCR text of a student's answer sheet into per-question answers.

Rules:
- Work only from the numbered OCR lines you are given. Never invent, complete, or "fix" content.
- Preserve the student's wording and spelling exactly as OCR'd.
- A line belongs to at most one answer. Each answer is one contiguous run of lines; report its line_start and line_end. If an answer clearly continues elsewhere on the sheet, cover the main run and describe the continuation in transcription_notes.
- Use question numbers written on the sheet and the question text as anchors. If a line cannot be confidently attributed to a question, put it in unmatched_content rather than guessing.
- Lines marked LOW-CONFIDENCE were poorly read by the OCR engine: keep their text, but write [illegible] in answer_text where a low-confidence line is clearly garbled.
- Report the student's name or roll number in student_identifier when visible; page headers and administrative text belong in unmatched_content, not in answers."""


class SegmentedAnswer(BaseModel):
    question_id: str = Field(
        description="The rubric question id this answer belongs to."
    )
    line_start: int = Field(
        description="Index of the first OCR line of this answer (inclusive)."
    )
    line_end: int = Field(
        description="Index of the last OCR line of this answer (inclusive)."
    )
    answer_text: str = Field(
        description=(
            "The answer assembled verbatim from those lines, preserving the "
            "student's spelling. Use [illegible] where a LOW-CONFIDENCE line "
            "is clearly garbled."
        )
    )
    transcription_notes: Optional[str] = Field(
        default=None,
        description="Crossed-out work, continuations elsewhere, OCR doubts.",
    )


class SegmentedTranscript(BaseModel):
    student_identifier: Optional[str] = None
    answers: list[SegmentedAnswer]
    unmatched_content: Optional[str] = None


def _numbered_lines(pages: list[PageOCR]) -> tuple[list[OCRLine], str]:
    lines = [line for page in pages for line in page.lines]
    rendered = []
    for i, line in enumerate(lines):
        flag = (", LOW-CONFIDENCE"
                if line.confidence < config.OCR_CONFIDENCE_FLOOR else "")
        rendered.append(f"[{i}] (page {line.page_index}{flag}) {line.text}")
    return lines, "\n".join(rendered)


def _regions(lines: list[OCRLine]) -> list[AnswerRegion]:
    """Union box of the answer's lines, one region per page — pure geometry."""
    by_page: dict[int, list[int]] = {}
    for line in lines:
        box = by_page.get(line.page_index)
        if box is None:
            by_page[line.page_index] = [line.x1, line.y1, line.x2, line.y2]
        else:
            box[0] = min(box[0], line.x1)
            box[1] = min(box[1], line.y1)
            box[2] = max(box[2], line.x2)
            box[3] = max(box[3], line.y2)
    return [
        AnswerRegion(page_index=page, x1=b[0], y1=b[1], x2=b[2], y2=b[3])
        for page, b in sorted(by_page.items())
        if b[2] > b[0] and b[3] > b[1]
    ]


def _legibility(lines: list[OCRLine], answer_text: str) -> str:
    """Code-enforced floor from Vision's own confidences, not model opinion."""
    if not lines:
        return "partial"
    mean = sum(line.confidence for line in lines) / len(lines)
    if mean < 0.3:
        return "illegible"
    if mean < config.OCR_CONFIDENCE_FLOOR or "[illegible]" in answer_text:
        return "partial"
    return "clear"


def transcribe(client, rubric: Rubric, file_paths: list[Path]) -> Transcript:
    """Drop-in for ocr.transcribe() on the google-vision OCR path."""
    pages = _ocr_pages(file_paths)
    all_lines, rendered = _numbered_lines(pages)

    prompt = (
        "Below is the OCR of a student's answer sheet, as numbered lines in "
        "reading order. The exam contains the following questions:\n\n"
        f"{_question_index(rubric)}\n\n"
        f"OCR lines:\n{rendered if rendered else '(no text detected)'}\n\n"
        "Segment the lines into per-question answers and return the result."
    )

    seg = providers.segment_client(client)
    response = seg.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SEGMENTATION_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=SegmentedTranscript,
    )
    if response.stop_reason == "refusal":
        raise TranscriptionRefused(
            "The model declined to segment this submission."
        )
    segmented = response.parsed_output
    if segmented is None:
        raise RuntimeError("Segmentation returned no parseable output.")

    answers = []
    for answer in segmented.answers:
        chosen: list[OCRLine] = []
        if all_lines:
            lo = max(0, min(answer.line_start, len(all_lines) - 1))
            hi = max(lo, min(answer.line_end, len(all_lines) - 1))
            chosen = all_lines[lo:hi + 1]
        answers.append(TranscribedAnswer(
            question_id=answer.question_id,
            answer_text=answer.answer_text,
            legibility=_legibility(chosen, answer.answer_text),
            transcription_notes=answer.transcription_notes,
            regions=_regions(chosen),
        ))

    return Transcript(
        student_identifier=segmented.student_identifier,
        answers=answers,
        unmatched_content=segmented.unmatched_content,
    )


# ---------------------------------------------------------------------------
# OCR-text variants of the other vision stages (metadata, paper reading)
# ---------------------------------------------------------------------------

def _full_text(pages: list[PageOCR]) -> str:
    parts = []
    for page in pages:
        body = "\n".join(line.text for line in page.lines)
        parts.append(f"--- Page {page.page_index} ---\n{body}")
    return "\n\n".join(parts)


def extract_metadata(client, first_page: Path) -> SheetMetadata:
    """Drop-in for metadata.extract() on the google-vision OCR path."""
    pages = _ocr_pages([first_page])
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        system=METADATA_SYSTEM + OCR_TEXT_NOTE,
        messages=[{
            "role": "user",
            "content": (
                "OCR text of the answer sheet's front page:\n\n"
                f"{_full_text(pages)}\n\n"
                "Extract the examination metadata from this front page."
            ),
        }],
        output_format=SheetMetadata,
    )
    if response.stop_reason == "refusal":
        raise MetadataRefused("The model declined to read this page.")
    if response.parsed_output is None:
        raise RuntimeError("Metadata extraction returned no parseable output.")
    return response.parsed_output


def extract_questions(client, paper_files: list[Path]) -> ExtractedQuestionPaper:
    """Drop-in for paper_ingest.extract_questions() on the google-vision path.

    Question papers are printed, so plain OCR text is a reliable input."""
    pages = _ocr_pages(paper_files)
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=EXTRACTION_SYSTEM + OCR_TEXT_NOTE,
        messages=[{
            "role": "user",
            "content": (
                "OCR text of the question paper:\n\n"
                f"{_full_text(pages)}\n\n"
                "List every question in this paper with its marks."
            ),
        }],
        output_format=ExtractedQuestionPaper,
    )
    if response.stop_reason == "refusal":
        raise PaperIngestRefused("The model declined to read this question paper.")
    if response.parsed_output is None:
        raise RuntimeError("Question extraction returned no parseable output.")
    return response.parsed_output
