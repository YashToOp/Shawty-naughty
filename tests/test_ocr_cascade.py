"""Cascade OCR: gate, fallback, conflict resolution, degradation, routing."""

from pathlib import Path

import pytest
from PIL import Image

from app import config, ocr_cascade, pipeline, vision_ocr
from app.models import Question, Rubric
from app.ocr_cascade import EngineUnavailable, cascade_pages, merge_page
from app.vision_ocr import OCRLine, PageOCR, SegmentedAnswer, SegmentedTranscript
from tests.conftest import FakeClient, FakeParseResponse


def line(text, y, conf, page=0, x1=50, x2=500):
    return OCRLine(page_index=page, text=text, x1=x1, y1=y, x2=x2, y2=y + 30,
                   confidence=conf)


@pytest.fixture
def page_image(tmp_path):
    path = tmp_path / "page.png"
    Image.new("RGB", (600, 800), "white").save(path)
    return path


# ---------------------------------------------------------------------------
# Confidence gate
# ---------------------------------------------------------------------------

def test_high_confidence_page_is_accepted_without_vision(page_image):
    calls = {"vision": 0}

    def paddle(image, index):
        return [line("The mitochondria is the powerhouse of the cell", 100, 0.96)]

    def vision(path, index):
        calls["vision"] += 1
        return PageOCR(page_index=index)

    pages = cascade_pages([page_image], paddle_fn=paddle, vision_fn=vision)
    assert calls["vision"] == 0                       # HIGH branch: accepted
    assert pages[0].lines[0].confidence == 0.96


def test_low_confidence_or_sparse_pages_fall_through(page_image):
    calls = {"vision": 0}

    def weak_paddle(image, index):
        return [line("smudged???", 100, 0.4)]         # low confidence AND sparse

    def vision(path, index):
        calls["vision"] += 1
        return PageOCR(page_index=index,
                       lines=[line("smudged handwriting", 100, 0.9)])

    pages = cascade_pages([page_image], paddle_fn=weak_paddle, vision_fn=vision)
    assert calls["vision"] == 1                       # LOW branch: re-read
    assert pages[0].lines[0].text == "smudged handwriting"


# ---------------------------------------------------------------------------
# Compare results / resolve conflicts
# ---------------------------------------------------------------------------

def test_agreeing_engines_corroborate_confidence():
    merged = merge_page(
        [line("Force equals mass times acceleration", 100, 0.80)],
        PageOCR(page_index=0, lines=[
            line("Force equals mass times acceleration.", 105, 0.85)]),
        page_index=0)
    assert len(merged.lines) == 1
    assert merged.lines[0].confidence == pytest.approx(0.90)  # boosted best

def test_disagreeing_engines_flag_the_line():
    merged = merge_page(
        [line("F = ma is Newton's second law", 100, 0.80)],
        PageOCR(page_index=0, lines=[
            line("Completely different reading here", 102, 0.70)]),
        page_index=0)
    assert len(merged.lines) == 1
    assert merged.lines[0].text.startswith("F = ma")   # higher-conf engine wins
    assert merged.lines[0].confidence <= 0.5           # but the line is flagged
    assert merged.lines[0].confidence < config.OCR_CONFIDENCE_FLOOR


def test_lines_seen_by_only_one_engine_are_kept():
    merged = merge_page(
        [line("only paddle saw this", 400, 0.9)],
        PageOCR(page_index=0, lines=[line("only vision saw this", 100, 0.9)]),
        page_index=0)
    assert [l.text for l in merged.lines] == \
        ["only vision saw this", "only paddle saw this"]  # sorted by position


# ---------------------------------------------------------------------------
# Degradation + end-to-end + routing
# ---------------------------------------------------------------------------

def test_missing_paddle_degrades_to_vision_only(page_image):
    def no_paddle(image, index):
        raise EngineUnavailable("not installed")

    def vision(path, index):
        return PageOCR(page_index=index, lines=[line("vision text", 100, 0.9)])

    pages = cascade_pages([page_image, page_image],
                          paddle_fn=no_paddle, vision_fn=vision)
    assert all(p.lines[0].text == "vision text" for p in pages)


def test_cascade_transcribe_reuses_shared_segmentation(monkeypatch, page_image):
    monkeypatch.setattr(ocr_cascade, "cascade_pages", lambda paths: [
        PageOCR(page_index=0, lines=[
            line("Q1. Velocity is speed with direction", 100, 0.95)])])
    rubric = Rubric(id="r", title="Quiz", questions=[
        Question(id="1", question_text="Define velocity.", max_marks=2)])
    client = FakeClient([FakeParseResponse(SegmentedTranscript(answers=[
        SegmentedAnswer(question_id="1", line_start=0, line_end=0,
                        answer_text="Velocity is speed with direction")]))])

    transcript = ocr_cascade.transcribe(client, rubric, [page_image])

    assert transcript.answers[0].legibility == "clear"
    assert transcript.answers[0].regions[0].y1 == 100  # geometry from OCR boxes


def test_provider_routing_includes_cascade(monkeypatch):
    monkeypatch.setattr(config, "OCR_PROVIDER", "cascade")
    assert pipeline.transcriber() is ocr_cascade
    assert pipeline.paper_reader() is ocr_cascade
    monkeypatch.setattr(config, "OCR_PROVIDER", "google-vision")
    assert pipeline.transcriber() is vision_ocr
