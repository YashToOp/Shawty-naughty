"""Stage 3: draw the evaluation back onto the student's own sheet.

For every answer with known bounding boxes, the original page image gets a
color-coded box (green = full marks, amber = partial, red = zero). The grade
("Q1 · 3/5") and the tags for criteria that cost marks ("✗ 1-b  ~ 1-c") are
stamped in a white gutter added to the right of the page, connected to the
box — so annotations never cover the student's writing. A final summary card
totals the paper and spells out what each tag means, so a student can read
their sheet and know exactly why they did not get full marks.

PDF uploads carry no pixel regions, so they get the summary card only.
"""

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import Job, QuestionResult, Rubric, SubmissionReport, Transcript

GREEN = (30, 125, 70)
AMBER = (179, 86, 11)
RED = (179, 38, 30)
INK = (28, 36, 48)
MUTED = (102, 112, 125)
LINE = (227, 230, 234)

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    path = _FONT_PATHS[0] if bold else _FONT_PATHS[1]
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _color_for(awarded: float, maximum: float) -> tuple[int, int, int]:
    if maximum > 0 and awarded >= maximum - 1e-6:
        return GREEN
    if awarded > 1e-6:
        return AMBER
    return RED


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def _tags(result: QuestionResult) -> str:
    """Compact criterion tags for marks lost: ✗ not met, ~ partial."""
    parts = []
    for c in result.evaluation.criteria:
        if c.met == "not_met":
            parts.append(f"✗ {c.criterion_id}")
        elif c.met == "partially":
            parts.append(f"~ {c.criterion_id}")
    return "   ".join(parts)


GUTTER = 340  # white margin added to the right of each page for grade stamps


def _stamp(draw: ImageDraw.ImageDraw, x: int, y: int, lines: list[str],
           color: tuple[int, int, int],
           font: ImageFont.ImageFont) -> tuple[int, int]:
    """Filled label with white text at (x, y) top-left. Returns (w, h)."""
    pad = 8
    widths, height = [], 0
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        widths.append(box[2] - box[0])
        height += (box[3] - box[1]) + 6
    w = max(widths) + pad * 2
    h = height + pad * 2 - 6
    draw.rectangle([x, y, x + w, y + h], fill=color)
    ty = y + pad
    for line in lines:
        draw.text((x + pad, ty), line, fill="white", font=font)
        box = draw.textbbox((0, 0), line, font=font)
        ty += (box[3] - box[1]) + 6
    return w, h


def annotate_pages(page_paths: list[Path], transcript: Transcript,
                   report: SubmissionReport, out_dir: Path) -> list[str]:
    """Draw graded boxes + gutter stamps on each image page.

    Returns written filenames. The stamps live in a white gutter appended to
    the right of the page so they never obscure the student's writing.
    """
    results_by_id = {r.evaluation.question_id: r for r in report.results}
    label_font = _font(24)
    written: list[str] = []

    pages: list[Image.Image] = []
    base_widths: list[int] = []
    for path in page_paths:
        if path.suffix.lower() == ".pdf":
            pages.append(None)  # no pixel space to draw on
            base_widths.append(0)
            continue
        original = Image.open(path).convert("RGB")
        canvas = Image.new("RGB", (original.width + GUTTER, original.height), "white")
        canvas.paste(original, (0, 0))
        ImageDraw.Draw(canvas).line(
            [(original.width + 4, 0), (original.width + 4, original.height)],
            fill=LINE, width=2,
        )
        pages.append(canvas)
        base_widths.append(original.width)

    # Track how far down each page's gutter is occupied to avoid overlaps.
    gutter_floor = [0] * len(pages)

    for answer in transcript.answers:
        result = results_by_id.get(answer.question_id)
        if result is None:
            continue
        ev = result.evaluation
        color = _color_for(result.final_marks, ev.max_marks)
        grade = f"Q{ev.question_id} · {_fmt(result.final_marks)}/{_fmt(ev.max_marks)}"
        if result.override:
            grade += " ✓reviewed"
        tags = _tags(result)

        for i, region in enumerate(answer.regions):
            if not 0 <= region.page_index < len(pages):
                continue
            page = pages[region.page_index]
            if page is None:
                continue
            base_w = base_widths[region.page_index]
            draw = ImageDraw.Draw(page)
            x1 = max(0, min(region.x1, base_w - 1))
            y1 = max(0, min(region.y1, page.height - 1))
            x2 = max(x1 + 1, min(region.x2, base_w))
            y2 = max(y1 + 1, min(region.y2, page.height))
            draw.rectangle([x1, y1, x2, y2], outline=color, width=4)

            if i == 0:  # stamp the grade once, on the first region
                lines = [grade]
                if tags:
                    lines.append(tags)
                sx = base_w + 14
                sy = max(y1, gutter_floor[region.page_index] + 10)
                sy = min(sy, page.height - 80)
                _, sh = _stamp(draw, sx, sy, lines, color, label_font)
                gutter_floor[region.page_index] = sy + sh
                # connector from the box's top-right corner to the stamp
                draw.line([(x2, y1 + 2), (sx, sy + sh // 2)], fill=color, width=3)

    out_dir.mkdir(parents=True, exist_ok=True)
    for index, page in enumerate(pages):
        if page is None:
            continue
        name = f"page_{index}_annotated.png"
        page.save(out_dir / name)
        written.append(name)
    return written


def render_summary_card(rubric: Rubric, report: SubmissionReport,
                        out_dir: Path) -> str:
    """A closing card: totals, per-question grades, and the tag legend."""
    title_font = _font(34)
    head_font = _font(26)
    body_font = _font(22, bold=False)
    small_font = _font(19, bold=False)

    criteria_by_id = {
        c.id: (q, c) for q in rubric.questions for c in q.criteria
    }

    # Build the line plan first so the card height fits the content.
    lines: list[tuple[str, ImageFont.ImageFont, tuple[int, int, int], int]] = []

    def add(text, font, color, indent=0, wrap=None):
        if wrap:
            for chunk in textwrap.wrap(text, wrap) or [""]:
                lines.append((chunk, font, color, indent))
        else:
            lines.append((text, font, color, indent))

    add("Result summary", title_font, INK)
    if report.student_identifier:
        add(report.student_identifier, body_font, MUTED)
    add(f"Total: {_fmt(report.total_awarded)} / {_fmt(report.total_available)}",
        head_font, INK)
    add("", body_font, INK)

    for result in report.results:
        ev = result.evaluation
        color = _color_for(result.final_marks, ev.max_marks)
        suffix = "  (adjusted by reviewer)" if result.override else ""
        add(f"Q{ev.question_id}  —  {_fmt(result.final_marks)}/{_fmt(ev.max_marks)}{suffix}",
            head_font, color)
        add(ev.overall_comment, small_font, MUTED, indent=24, wrap=88)
        for c in ev.criteria:
            if c.met == "fully":
                continue
            mark = "✗" if c.met == "not_met" else "~"
            _, criterion = criteria_by_id.get(c.criterion_id, (None, None))
            desc = criterion.description if criterion else ""
            add(f"{mark} [{c.criterion_id}] {desc}", body_font,
                RED if c.met == "not_met" else AMBER, indent=24, wrap=80)
            add(f"{_fmt(c.marks_awarded)} marks — {c.rationale}",
                small_font, MUTED, indent=48, wrap=80)
        add("", body_font, INK)

    add("Legend:  ✗ criterion not met    ~ partially met    "
        "green box = full marks, amber = partial, red = none",
        small_font, MUTED)

    width, margin, line_gap = 1080, 48, 10
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    height = margin * 2
    for text, font, _, _ in lines:
        box = probe.textbbox((0, 0), text or "x", font=font)
        height += (box[3] - box[1]) + line_gap

    card = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, width - 1, height - 1], outline=LINE, width=2)
    y = margin
    for text, font, color, indent in lines:
        draw.text((margin + indent, y), text, fill=color, font=font)
        box = draw.textbbox((0, 0), text or "x", font=font)
        y += (box[3] - box[1]) + line_gap

    out_dir.mkdir(parents=True, exist_ok=True)
    name = "summary_card.png"
    card.save(out_dir / name)
    return name


def annotate_submission(job: Job, rubric: Rubric, transcript: Transcript,
                        report: SubmissionReport, page_paths: list[Path],
                        out_dir: Path) -> list[str]:
    """Produce all annotation artifacts; returns filenames in display order."""
    files = annotate_pages(page_paths, transcript, report, out_dir)
    files.append(render_summary_card(rubric, report, out_dir))
    return files
