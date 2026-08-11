"""Offline tests for the official-papers fetch script (no network)."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "fetch_official_papers",
    Path(__file__).parent.parent / "scripts" / "fetch_official_papers.py")
fetcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetcher)

FIXTURE = """
<html><body>
  <a href="/cbsenew/QuestionPaper/2026/ClassX/Science_31-1-1_2026.pdf">Science set 1</a>
  <a href="QuestionPaper/2026/ClassXII/Physics_55_2_1.pdf">Physics</a>
  <a href="https://www.cbseacademic.nic.in/web_material/SQP/ClassXII_2025_26/English.pdf">SQP</a>
  <a href="https://some-aggregator.com/cbse/Science_2026.pdf">mirror (must be excluded)</a>
  <a href="/cbsenew/question-paper-2025.html">older years page</a>
  <a href="https://evil.example.com/page.html">offsite page (excluded)</a>
</body></html>
"""


def test_extract_links_keeps_official_domains_only():
    pdfs, pages = fetcher.extract_links(
        FIXTURE, "https://www.cbse.gov.in/cbsenew/question-paper.html")

    assert pdfs == [
        "https://www.cbse.gov.in/cbsenew/QuestionPaper/2026/ClassX/Science_31-1-1_2026.pdf",
        "https://www.cbse.gov.in/cbsenew/QuestionPaper/2026/ClassXII/Physics_55_2_1.pdf",
        "https://www.cbseacademic.nic.in/web_material/SQP/ClassXII_2025_26/English.pdf",
    ]
    assert pages == ["https://www.cbse.gov.in/cbsenew/question-paper-2025.html"]
    assert not any("aggregator" in p or "evil" in p for p in pdfs + pages)


def test_guess_metadata_from_url():
    meta = fetcher.guess_metadata(
        "https://www.cbse.gov.in/QuestionPaper/2026/ClassX/Science_31-1-1_2026.pdf")
    assert meta == {"board": "CBSE", "class_level": "10",
                    "subject": "Science", "year": "2026"}

    meta = fetcher.guess_metadata("https://www.cbse.gov.in/files/mystery.pdf")
    assert meta["class_level"] == "FILL_ME"
    assert meta["subject"] == "FILL_ME"


def test_ingest_command_comments_out_incomplete_metadata():
    complete = fetcher.ingest_command(
        Path("downloads/Science.pdf"),
        {"board": "CBSE", "class_level": "10", "subject": "Science", "year": "2026"})
    assert complete.startswith("python scripts/ingest_paper.py")

    incomplete = fetcher.ingest_command(
        Path("downloads/mystery.pdf"),
        {"board": "CBSE", "class_level": "FILL_ME",
         "subject": "FILL_ME", "year": "2026"})
    assert incomplete.startswith("# FILL METADATA")


def test_match_filter():
    assert fetcher.matches("https://x/ClassX_Science_2026.pdf", ["class-x", "2026"])
    assert not fetcher.matches("https://x/ClassX_Science_2019.pdf", ["2026"])
    assert fetcher.matches("https://x/anything.pdf", [])
