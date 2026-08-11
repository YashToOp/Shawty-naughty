"""Front-page metadata extraction for the student self-service flow.

Reads only the first uploaded page (where boards print the administrative
header) and returns board / class / subject / year / paper code, which keys
the question-paper registry lookup.
"""

from pathlib import Path

import anthropic

from .config import ANTHROPIC_MODEL
from .models import SheetMetadata
from .ocr import build_page_blocks

METADATA_SYSTEM = """You extract administrative metadata from the front page of an examination answer sheet or question paper.

Rules:
- Report only what is actually printed or written on the page. Any field you cannot read is null - never guess.
- paper_code is the question-paper code / set number (formats like 1/1/1, 058/1/2, SET-2). Do not confuse it with the roll number or subject code alone.
- exam_year is the year of the examination as a 4-digit number.
- Mention anything ambiguous (smudged code, two candidate values) in notes."""


class MetadataRefused(Exception):
    pass


def extract(client: anthropic.Anthropic, first_page: Path) -> SheetMetadata:
    response = client.messages.parse(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        system=METADATA_SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                *build_page_blocks([first_page]),
                {"type": "text",
                 "text": "Extract the examination metadata from this front page."},
            ],
        }],
        output_format=SheetMetadata,
    )
    if response.stop_reason == "refusal":
        raise MetadataRefused("The model declined to read this page.")
    if response.parsed_output is None:
        raise RuntimeError("Metadata extraction returned no parseable output.")
    return response.parsed_output
