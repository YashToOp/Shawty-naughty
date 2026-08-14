"""PDF uploads become page images at the upload boundary."""

import io

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, pipeline, rasterize, storage
from app.main import app
from app.rasterize import RasterizeError, expand_uploads
from tests.conftest import FakeClient


def make_pdf(pages: int = 2) -> bytes:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(595, 842)  # A4 in points
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_pdf_expands_to_ordered_named_pngs():
    expanded = expand_uploads([("scan.pdf", make_pdf(3))])
    assert [name for name, _ in expanded] == \
        ["scan_p01.png", "scan_p02.png", "scan_p03.png"]
    image = Image.open(io.BytesIO(expanded[0][1]))
    assert image.format == "PNG"
    assert image.width > 1500  # 200 DPI on A4 — plenty for OCR and boxes


def test_images_pass_through_and_order_is_preserved():
    expanded = expand_uploads([
        ("front.jpg", b"jpgbytes"),
        ("middle.pdf", make_pdf(2)),
        ("back.png", b"pngbytes"),
    ])
    assert [name for name, _ in expanded] == \
        ["front.jpg", "middle_p01.png", "middle_p02.png", "back.png"]
    assert expanded[0][1] == b"jpgbytes"  # untouched


def test_corrupt_pdf_and_page_cap_are_rejected(monkeypatch):
    with pytest.raises(RasterizeError, match="corrupt"):
        expand_uploads([("bad.pdf", b"not a pdf at all")])

    monkeypatch.setattr(config, "MAX_PDF_PAGES", 2)
    with pytest.raises(RasterizeError, match="limit per PDF"):
        expand_uploads([("big.pdf", make_pdf(3))])


def test_api_upload_stores_rendered_pages_not_the_pdf(monkeypatch):
    monkeypatch.setattr(pipeline, "get_client", lambda: FakeClient([]))
    with TestClient(app) as http:
        response = http.post(
            "/api/contribute",
            data={"board": "CBSE", "class_level": "12", "subject": "Physics",
                  "exam_year": "2027"},
            files=[("files", ("paper.pdf", make_pdf(2), "application/pdf"))],
        )
        assert response.status_code == 202
        job = storage.get_job(response.json()["job_id"])
    assert job.uploaded_files == ["paper_p01.png", "paper_p02.png"]

    for name in job.uploaded_files:  # stored files are real PNGs on disk
        path = [p for p in storage.job_upload_paths(job) if p.name == name][0]
        assert Image.open(path).format == "PNG"


def test_api_rejects_over_limit_pdf_with_400(monkeypatch):
    monkeypatch.setattr(config, "MAX_PDF_PAGES", 1)
    with TestClient(app) as http:
        response = http.post(
            "/api/contribute",
            data={"board": "CBSE", "class_level": "12", "subject": "Physics",
                  "exam_year": "2027"},
            files=[("files", ("big.pdf", make_pdf(2), "application/pdf"))],
        )
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]
