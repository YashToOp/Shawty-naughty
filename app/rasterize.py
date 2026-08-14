"""Rasterize PDF uploads into page images, once, at the upload boundary.

Every downstream stage prefers pixels: Claude vision gets pages it can
return bounding boxes for, the Google Vision OCR path stops rejecting PDFs,
and the annotator can draw graded boxes on every submission instead of
falling back to a summary card. So PDFs are converted to PNGs the moment
they arrive and never stored; page order is preserved and pages are named
after the source file (`scan_p01.png`, `scan_p02.png`, ...).

A page cap bounds the model-call cost of a single upload — a 500-page PDF
is a mistake or an attack, not a submission.
"""

import io
from typing import Optional

import pypdfium2 as pdfium
from PIL import Image

from . import config


class RasterizeError(ValueError):
    pass


def rasterize_pdf(data: bytes, name: str = "upload.pdf") -> list[bytes]:
    """PDF bytes -> one PNG per page, rendered at RASTER_DPI."""
    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:
        raise RasterizeError(
            f"{name} could not be read as a PDF — is it corrupt or "
            "password-protected?"
        ) from exc

    try:
        page_count = len(document)
        if page_count == 0:
            raise RasterizeError(f"{name} contains no pages.")
        if page_count > config.MAX_PDF_PAGES:
            raise RasterizeError(
                f"{name} has {page_count} pages — the limit per PDF is "
                f"{config.MAX_PDF_PAGES}. Split it and upload the parts."
            )

        pages: list[bytes] = []
        scale = config.RASTER_DPI / 72  # PDF points are 1/72 inch
        for index in range(page_count):
            image: Image.Image = document[index].render(scale=scale).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            pages.append(buffer.getvalue())
        return pages
    finally:
        document.close()


def expand_uploads(contents: list[tuple[str, bytes]],
                   max_total_pages: Optional[int] = None
                   ) -> list[tuple[str, bytes]]:
    """Replace every PDF in an upload batch with its rendered pages.

    Non-PDF files pass through untouched; ordering is preserved so page
    indexes keep matching the reading order of the submission."""
    limit = max_total_pages or config.MAX_PDF_PAGES
    expanded: list[tuple[str, bytes]] = []
    for filename, data in contents:
        if not filename.lower().endswith(".pdf"):
            expanded.append((filename, data))
            continue
        stem = filename.rsplit(".", 1)[0]
        for i, png in enumerate(rasterize_pdf(data, name=filename), start=1):
            expanded.append((f"{stem}_p{i:02d}.png", png))
    if len(expanded) > limit:
        raise RasterizeError(
            f"This upload expands to {len(expanded)} pages — the limit is "
            f"{limit}. Split it into smaller submissions."
        )
    return expanded
