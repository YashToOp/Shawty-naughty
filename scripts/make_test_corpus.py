#!/usr/bin/env python
"""Render the ingestion test corpus into scan-like page images.

Reads the original practice papers in sample_data/test_corpus/*.json and
writes page PNGs to test_corpus_rendered/<paper-stem>/page_N.png, ready to
feed to scripts/ingest_paper.py. Use this to exercise the ingestion +
rubric-generation pipeline end to end without any board content.

    python scripts/make_test_corpus.py
    python scripts/ingest_paper.py test_corpus_rendered/maths10-430-1-1/*.png \
        --board CBSE --class-level 10 --subject "Mathematics (Standard)" --year 2026
"""

import json
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "sample_data" / "test_corpus"
OUT = ROOT / "test_corpus_rendered"

PAGE_W, PAGE_H, MARGIN, LH = 900, 1200, 55, 25
WRAP = 78


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    path = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def render_paper(spec: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: list[Image.Image] = []

    img = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    draw = ImageDraw.Draw(img)
    draw.text((MARGIN, 30), f"{spec['board']}  {spec['subject']}  "
              f"Class {spec['class_level']}  ({spec['year']})",
              fill=(20, 20, 60), font=_font(22, bold=True))
    draw.text((MARGIN, 64), f"Q.P. Code: {spec['paper_code']}     "
              f"Time: 90 minutes     Maximum marks: "
              f"{sum(q['max_marks'] for q in spec['questions']):g}",
              fill=(20, 20, 60), font=_font(16))
    y = 110

    for q in spec["questions"]:
        lines = []
        for para in q["question_text"].split("\n"):
            lines.extend(textwrap.wrap(para, WRAP) or [""])
        header = f"Q{q['id']}. [{q['max_marks']:g} mark" \
                 f"{'s' if q['max_marks'] != 1 else ''}]"
        block_h = (len(lines) + 1) * LH + 16
        if y + block_h > PAGE_H - 40:
            pages.append(img)
            img = Image.new("RGB", (PAGE_W, PAGE_H), "white")
            draw = ImageDraw.Draw(img)
            y = 45
        draw.text((MARGIN, y), header, fill=(25, 25, 70), font=_font(16, bold=True))
        y += LH
        for line in lines:
            draw.text((MARGIN + 14, y), line, fill=(25, 25, 70), font=_font(15))
            y += LH
        y += 16
    pages.append(img)

    written = []
    for i, page in enumerate(pages):
        path = out_dir / f"page_{i + 1}.png"
        page.save(path)
        written.append(path)
    return written


def main() -> None:
    specs = sorted(CORPUS.glob("*.json"))
    if not specs:
        sys.exit(f"No corpus definitions found in {CORPUS}")
    for spec_path in specs:
        spec = json.loads(spec_path.read_text())
        pages = render_paper(spec, OUT / spec_path.stem)
        print(f"{spec_path.stem}: {len(pages)} page(s) -> {OUT / spec_path.stem}")


if __name__ == "__main__":
    main()
