"""Cascade OCR: free local engine first, paid engine only where needed.

                        IMAGE
                          |
                  Preprocessing            (EXIF orientation, grayscale,
                          |                 autocontrast)
                  Page / region detection  (PaddleOCR's detector: line boxes)
                          |
                    PaddleOCR              (free, self-hosted recognition)
                          |
                 Confidence / quality
                     /          \\
                  HIGH           LOW
                   |              |
                Accept       Google Vision   (paid fallback, this page only)
                                  |
                           Compare results   (line-level match by geometry)
                                  |
                           Resolve conflicts (agree -> boost confidence;
                                  |           disagree -> keep better engine's
                                  |           text, cap confidence so the line
                                  |           is flagged LOW-CONFIDENCE)
                             Grading LLM     (existing segmentation ->
                                              evaluation stages, unchanged)

Selected with OCR_PROVIDER=cascade. Printed pages (question papers, typed
sheets) clear the gate and cost nothing; messy handwriting falls through to
Google Vision page by page, so the paid engine is only bought where the free
one actually failed. Conflict resolution is deterministic code, not another
model call — same "model judges, code enforces" rule as everywhere else.

If PaddleOCR isn't installed (optional heavy dependency:
`pip install paddleocr paddlepaddle`), the cascade degrades to the pure
Google Vision path with a logged warning rather than failing jobs.
"""

import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageOps

from . import config, vision_ocr
from .models import ExtractedQuestionPaper, Rubric, SheetMetadata, Transcript
from .vision_ocr import OCRLine, PageOCR

logger = logging.getLogger(__name__)

PaddleFn = Callable[[Image.Image, int], list[OCRLine]]
VisionFn = Callable[[Path, int], PageOCR]


class EngineUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

def preprocess(path: Path) -> Image.Image:
    """Normalise a page photo for OCR: honour EXIF rotation, flatten to
    grayscale, stretch contrast. Deliberately mild — aggressive binarisation
    hurts more handwriting than it helps."""
    image = Image.open(path)
    image = ImageOps.exif_transpose(image)
    image = ImageOps.autocontrast(image.convert("L"), cutoff=1)
    return image.convert("RGB")


# ---------------------------------------------------------------------------
# PaddleOCR engine (lazy import; optional dependency)
# ---------------------------------------------------------------------------

_paddle = None


def _paddle_lines(image: Image.Image, page_index: int) -> list[OCRLine]:
    global _paddle
    if _paddle is None:
        try:
            from paddleocr import PaddleOCR  # noqa: heavy, optional
        except ImportError as exc:
            raise EngineUnavailable(
                "PaddleOCR is not installed — pip install paddleocr "
                "paddlepaddle, or use OCR_PROVIDER=google-vision/claude."
            ) from exc
        _paddle = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)

    import numpy as np

    result = _paddle.ocr(np.array(image), cls=True)
    lines: list[OCRLine] = []
    for page in result or []:
        for entry in page or []:
            try:
                box, (text, confidence) = entry
            except (TypeError, ValueError):
                continue
            if not str(text).strip():
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            lines.append(OCRLine(
                page_index=page_index, text=str(text).strip(),
                x1=int(min(xs)), y1=int(min(ys)),
                x2=int(max(xs)), y2=int(max(ys)),
                confidence=float(confidence),
            ))
    lines.sort(key=lambda l: (l.y1, l.x1))
    return lines


def _vision_page(path: Path, page_index: int) -> PageOCR:
    page = vision_ocr._ocr_pages([path])[0]
    page.page_index = page_index
    for line in page.lines:
        line.page_index = page_index
    return page


# ---------------------------------------------------------------------------
# Confidence gate + conflict resolution
# ---------------------------------------------------------------------------

def _passes_gate(lines: list[OCRLine]) -> bool:
    """HIGH branch: enough recognised text, confidently."""
    chars = sum(len(l.text) for l in lines)
    if chars < config.CASCADE_MIN_CHARS:
        return False
    mean = sum(l.confidence for l in lines) / len(lines)
    return mean >= config.CASCADE_ACCEPT_CONFIDENCE


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def _overlaps(a: OCRLine, b: OCRLine) -> bool:
    """Same physical line? Vertical overlap dominates; require some
    horizontal intersection too."""
    vertical = min(a.y2, b.y2) - max(a.y1, b.y1)
    if vertical <= 0:
        return False
    if vertical / max(1, min(a.y2 - a.y1, b.y2 - b.y1)) < 0.5:
        return False
    return min(a.x2, b.x2) - max(a.x1, b.x1) > 0


def merge_page(paddle_lines: list[OCRLine], vision_page: PageOCR,
               page_index: int) -> PageOCR:
    """Compare both engines' reads line by line and resolve conflicts.

    Agreement corroborates (confidence boosted); disagreement keeps the
    more confident engine's text but caps confidence at 0.5, which flags
    the line LOW-CONFIDENCE for the segmentation prompt and caps the
    answer's legibility downstream. Lines only one engine saw are kept."""
    merged: list[OCRLine] = []
    unclaimed = list(paddle_lines)

    for v_line in vision_page.lines:
        partner = next((p for p in unclaimed if _overlaps(p, v_line)), None)
        if partner is None:
            merged.append(v_line)
            continue
        unclaimed.remove(partner)
        similarity = SequenceMatcher(
            None, _norm(partner.text), _norm(v_line.text)).ratio()
        best = max(partner, v_line, key=lambda l: l.confidence)
        if similarity >= config.CASCADE_AGREE_SIMILARITY:
            confidence = min(1.0, best.confidence + 0.05)  # corroborated
        else:
            confidence = min(best.confidence, 0.5)         # disputed
        merged.append(OCRLine(
            page_index=page_index, text=best.text,
            x1=min(partner.x1, v_line.x1), y1=min(partner.y1, v_line.y1),
            x2=max(partner.x2, v_line.x2), y2=max(partner.y2, v_line.y2),
            confidence=confidence,
        ))

    merged.extend(unclaimed)  # text only PaddleOCR saw
    merged.sort(key=lambda l: (l.y1, l.x1))
    return PageOCR(page_index=page_index, width=vision_page.width,
                   height=vision_page.height, lines=merged)


# ---------------------------------------------------------------------------
# The cascade
# ---------------------------------------------------------------------------

def cascade_pages(paths: list[Path],
                  paddle_fn: Optional[PaddleFn] = None,
                  vision_fn: Optional[VisionFn] = None) -> list[PageOCR]:
    paddle_fn = paddle_fn or _paddle_lines
    vision_fn = vision_fn or _vision_page
    paddle_ok = True

    pages: list[PageOCR] = []
    for index, path in enumerate(paths):
        lines: list[OCRLine] = []
        if paddle_ok:
            try:
                lines = paddle_fn(preprocess(path), index)
            except EngineUnavailable as exc:
                logger.warning("Cascade degrading to Google Vision only: %s", exc)
                paddle_ok = False

        if paddle_ok and _passes_gate(lines):
            pages.append(PageOCR(page_index=index, lines=lines))   # HIGH
        else:
            vision_page = vision_fn(path, index)                   # LOW
            pages.append(merge_page(lines, vision_page, index))
    return pages


def transcribe(client, rubric: Rubric, file_paths: list[Path]) -> Transcript:
    """Drop-in for ocr.transcribe() on the cascade path."""
    return vision_ocr.segment_transcript(client, rubric,
                                         cascade_pages(file_paths))


def extract_metadata(client, first_page: Path) -> SheetMetadata:
    return vision_ocr.metadata_from_pages(client, cascade_pages([first_page]))


def extract_questions(client, paper_files: list[Path]) -> ExtractedQuestionPaper:
    return vision_ocr.questions_from_pages(client, cascade_pages(paper_files))
