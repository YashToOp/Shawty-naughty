"""Free-stack OCR path: Vision parsing, segmentation, and provider routing."""

from pathlib import Path

import pytest

from app import config, ocr, pipeline, vision_ocr
from app.models import Question, Rubric
from app.vision_ocr import (
    OCRLine,
    PageOCR,
    SegmentedAnswer,
    SegmentedTranscript,
    _parse_annotation,
)
from tests.conftest import FakeClient, FakeParseResponse


def rubric():
    return Rubric(id="r1", title="Quiz", questions=[
        Question(id="1", question_text="Define velocity.", max_marks=2),
        Question(id="2", question_text="State Newton's second law.", max_marks=3),
    ])


# ---------------------------------------------------------------------------
# Vision response parsing
# ---------------------------------------------------------------------------

def word(text, confidence=0.95):
    return {"symbols": [{"text": ch} for ch in text], "confidence": confidence}


def paragraph(words, box):
    (x1, y1), (x2, y2) = box
    return {
        "boundingBox": {"vertices": [
            {"x": x1, "y": y1}, {"x": x2, "y": y1},
            {"x": x2, "y": y2}, {"x": x1, "y": y2},
        ]},
        "words": words,
    }


def vision_payload():
    return {"responses": [{"fullTextAnnotation": {
        "text": "Q1. Velocity is speed with direction",
        "pages": [{
            "width": 900, "height": 1200,
            "blocks": [{"paragraphs": [
                paragraph([word("Q1."), word("Velocity"), word("is")],
                          ((50, 100), (400, 130))),
                paragraph([word("speed", 0.4), word("with", 0.4),
                           word("direction", 0.4)],
                          ((50, 140), (380, 170))),
            ]}],
        }],
    }}]}


def test_parse_annotation_lines_boxes_and_confidence():
    page = _parse_annotation(vision_payload(), page_index=0)
    assert page.width == 900 and page.height == 1200
    assert [line.text for line in page.lines] == \
        ["Q1. Velocity is", "speed with direction"]
    first = page.lines[0]
    assert (first.x1, first.y1, first.x2, first.y2) == (50, 100, 400, 130)
    assert first.confidence == pytest.approx(0.95)
    assert page.lines[1].confidence == pytest.approx(0.4)


def test_parse_annotation_surfaces_api_errors():
    with pytest.raises(RuntimeError, match="quota"):
        _parse_annotation(
            {"responses": [{"error": {"message": "quota exceeded"}}]}, 0)


def test_pdf_uploads_are_rejected(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_VISION_API_KEY", "key")
    with pytest.raises(ValueError, match="image pages only"):
        vision_ocr._ocr_pages([Path("sheet.pdf")])


# ---------------------------------------------------------------------------
# Segmentation -> Transcript (regions and legibility computed in code)
# ---------------------------------------------------------------------------

def canned_pages():
    return [PageOCR(page_index=0, width=900, height=1200, lines=[
        OCRLine(page_index=0, text="Q1. Velocity is speed with direction",
                x1=50, y1=100, x2=400, y2=130, confidence=0.95),
        OCRLine(page_index=0, text="it also has magnitude",
                x1=60, y1=140, x2=380, y2=170, confidence=0.9),
        OCRLine(page_index=0, text="Q2. F = ma something smudged",
                x1=50, y1=300, x2=420, y2=340, confidence=0.35),
    ])]


def segmented():
    return SegmentedTranscript(
        student_identifier="Roll 17",
        answers=[
            SegmentedAnswer(question_id="1", line_start=0, line_end=1,
                            answer_text="Velocity is speed with direction\n"
                                        "it also has magnitude"),
            SegmentedAnswer(question_id="2", line_start=2, line_end=2,
                            answer_text="F = ma [illegible]"),
        ],
        unmatched_content=None,
    )


def test_transcribe_builds_regions_and_enforces_legibility(monkeypatch):
    monkeypatch.setattr(vision_ocr, "_ocr_pages", lambda paths: canned_pages())
    client = FakeClient([FakeParseResponse(segmented())])

    transcript = vision_ocr.transcribe(client, rubric(), [Path("page1.png")])

    q1, q2 = transcript.answers
    assert transcript.student_identifier == "Roll 17"

    # Region = union box of the answer's own OCR lines, computed in code.
    region = q1.regions[0]
    assert (region.x1, region.y1, region.x2, region.y2) == (50, 100, 400, 170)
    assert q1.legibility == "clear"

    # Low Vision confidence caps legibility regardless of the model's text.
    assert q2.legibility != "clear"
    assert q2.regions[0].y1 == 300

    # The segmentation prompt carried the numbered lines + question index.
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "[0] (page 0)" in prompt
    assert "LOW-CONFIDENCE" in prompt          # the smudged Q2 line was flagged
    assert "Define velocity." in prompt


def test_transcribe_clamps_out_of_range_line_indices(monkeypatch):
    monkeypatch.setattr(vision_ocr, "_ocr_pages", lambda paths: canned_pages())
    bad = SegmentedTranscript(answers=[
        SegmentedAnswer(question_id="1", line_start=1, line_end=99,
                        answer_text="whatever"),
    ])
    client = FakeClient([FakeParseResponse(bad)])
    transcript = vision_ocr.transcribe(client, rubric(), [Path("p.png")])
    region = transcript.answers[0].regions[0]
    assert region.y2 == 340  # clamped to the last real line, no crash


def test_extract_metadata_uses_ocr_text(monkeypatch):
    monkeypatch.setattr(vision_ocr, "_ocr_pages", lambda paths: canned_pages())
    from app.models import SheetMetadata
    meta = SheetMetadata(board="CBSE", class_level="12", exam_year=2026)
    client = FakeClient([FakeParseResponse(meta)])

    result = vision_ocr.extract_metadata(client, Path("front.png"))

    assert result.board == "CBSE"
    call = client.messages.calls[0]
    assert "OCR text" in call["messages"][0]["content"]
    assert "Velocity" in call["messages"][0]["content"]
    assert "OCR" in call["system"]  # the OCR-input caveat rode along


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

def test_pipeline_routes_transcriber_by_config(monkeypatch):
    assert pipeline.transcriber() is ocr
    monkeypatch.setattr(config, "OCR_PROVIDER", "google-vision")
    assert pipeline.transcriber() is vision_ocr


def test_get_client_requires_workers_credentials(monkeypatch):
    from app.providers import ProviderError
    monkeypatch.setattr(config, "EVAL_PROVIDER", "workers-ai")
    monkeypatch.setattr(config, "CF_ACCOUNT_ID", "")
    with pytest.raises(ProviderError):
        pipeline.get_client()
