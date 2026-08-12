"""Stage 3: draw the evaluation back onto the student's own sheet.

For every answer with known bounding boxes, the original page gets a
translucent colour wash + rounded outline (green = full marks, amber =
partial, red = zero). The grade lives on a card in a white gutter appended
to the right of the page — score pill, one dot per criterion (● earned,
◐ partial, ○ missed) and tags for the criteria that cost marks — linked to
the answer by a curved connector, so annotations never cover the student's
writing. A closing summary card totals the paper with a score donut,
per-question mark bars and the tag legend.

Everything here is deterministic Pillow drawing on CPU: no model calls, no
per-paper cost. Small vector art (pills, dots, donut) is rendered at 2x and
downscaled for antialiasing.

PDF uploads carry no pixel regions, so they get the summary card only.
"""

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .evaluator import DISPOSITIONS
from .models import Job, QuestionResult, Rubric, SubmissionReport, Transcript

GREEN = (30, 125, 70)
AMBER = (179, 86, 11)
RED = (179, 38, 30)
INK = (28, 36, 48)
MUTED = (102, 112, 125)
LINE = (227, 230, 234)
SOFT = (238, 241, 245)
ACCENT = (37, 87, 214)

GUTTER = 340   # white margin added to the right of each page for grade cards
CARD_W = GUTTER - 30
SS = 2         # supersample factor for antialiased small shapes

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


def _pct_color(pct: float) -> tuple[int, int, int]:
    return GREEN if pct >= 0.75 else AMBER if pct >= 0.35 else RED


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


# ---------------------------------------------------------------------------
# Antialiased building blocks (rendered at SS×, downscaled)
# ---------------------------------------------------------------------------

def _measure(text: str, font: ImageFont.ImageFont) -> tuple[int, int, tuple]:
    box = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1], box


def _pill(text: str, fill: tuple[int, int, int], size: int = 19) -> Image.Image:
    """Rounded score pill with white bold text."""
    font = _font(size * SS)
    tw, th, box = _measure(text, font)
    pad_x, pad_y = 11 * SS, 6 * SS
    w, h = tw + pad_x * 2, th + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=fill + (255,))
    draw.text((pad_x - box[0], pad_y - box[1]), text, font=font, fill="white")
    return img.resize((w // SS, h // SS), Image.LANCZOS)


def _criterion_dots(criteria) -> Image.Image | None:
    """One dot per criterion: ● earned, ◐ partially, ○ missed."""
    if not criteria:
        return None
    d, gap = 15, 7
    w = len(criteria) * d + (len(criteria) - 1) * gap
    img = Image.new("RGBA", (w * SS, d * SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x = 0
    for c in criteria:
        box = [x * SS + SS, SS, (x + d) * SS - SS, d * SS - SS]
        if c.met == "fully":
            draw.ellipse(box, fill=GREEN + (255,))
        elif c.met == "partially":
            draw.pieslice(box, 90, 270, fill=AMBER + (255,))
            draw.ellipse(box, outline=AMBER + (255,), width=2 * SS)
        else:
            draw.ellipse(box, outline=RED + (255,), width=2 * SS)
        x += d + gap
    return img.resize((w, d), Image.LANCZOS)


def _donut(pct: float, size: int = 118) -> Image.Image:
    """Score ring with the percentage in the middle."""
    img = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    box = [5 * SS, 5 * SS, (size - 5) * SS, (size - 5) * SS]
    ring = 13 * SS
    draw.arc(box, 0, 360, fill=LINE + (255,), width=ring)
    if pct > 0:
        draw.arc(box, -90, -90 + 360 * min(pct, 1.0),
                 fill=_pct_color(pct) + (255,), width=ring)
    out = img.resize((size, size), Image.LANCZOS)
    label = f"{round(pct * 100)}%"
    font = _font(24)
    tw, th, tbox = _measure(label, font)
    ImageDraw.Draw(out).text(((size - tw) / 2 - tbox[0], (size - th) / 2 - tbox[1]),
                             label, font=font, fill=INK)
    return out


def _curve(draw: ImageDraw.ImageDraw, p0: tuple, p1: tuple,
           color: tuple[int, int, int], width: int = 3) -> None:
    """Smooth S-curve connector with a dot at the answer end."""
    (x0, y0), (x1, y1) = p0, p1
    c0 = (x0 + (x1 - x0) * 0.45, y0)
    c1 = (x0 + (x1 - x0) * 0.55, y1)
    points = []
    for i in range(25):
        t = i / 24
        mt = 1 - t
        points.append((
            mt ** 3 * x0 + 3 * mt ** 2 * t * c0[0] + 3 * mt * t ** 2 * c1[0] + t ** 3 * x1,
            mt ** 3 * y0 + 3 * mt ** 2 * t * c0[1] + 3 * mt * t ** 2 * c1[1] + t ** 3 * y1,
        ))
    draw.line(points, fill=color + (215,), width=width, joint="curve")
    r = 4
    draw.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=color + (255,))


def _grade_card(result: QuestionResult) -> Image.Image:
    """The gutter card for one question: Q-number, score pill, criterion
    dots, lost-criterion tags, and a reviewed chip after an override."""
    ev = result.evaluation
    color = _color_for(result.final_marks, ev.max_marks)
    q_font, tag_font = _font(23), _font(15)

    pill = _pill(f"{_fmt(result.final_marks)}/{_fmt(ev.max_marks)}", color)
    dots = _criterion_dots(ev.criteria)
    lost = [(("✗ " if c.met == "not_met" else "~ ") + c.criterion_id,
             RED if c.met == "not_met" else AMBER)
            for c in ev.criteria if c.met != "fully"]
    reviewed = _pill("✓ reviewed", ACCENT, size=14) if result.override else None

    pad, text_x = 13, 24
    _, q_h, _ = _measure("Qx", q_font)
    height = pad + max(q_h, pill.height)
    if dots is not None:
        height += 12 + dots.height
    if lost:
        height += 9 + _measure("✗", tag_font)[1]
    if reviewed is not None:
        height += 10 + reviewed.height
    height += pad

    card = Image.new("RGBA", (CARD_W, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([0, 0, CARD_W - 1, height - 1], radius=10,
                           fill=(255, 255, 255, 255), outline=LINE + (255,))
    # colour capsule down the left edge
    draw.rounded_rectangle([7, 9, 12, height - 10], radius=3, fill=color + (255,))

    y = pad
    draw.text((text_x, y + (max(q_h, pill.height) - q_h) // 2 - 2),
              f"Q{ev.question_id}", font=q_font, fill=INK)
    card.alpha_composite(pill, (CARD_W - pad - pill.width, y))
    y += max(q_h, pill.height)

    if dots is not None:
        y += 12
        card.alpha_composite(dots, (text_x, y))
        y += dots.height
    if lost:
        y += 9
        x = text_x
        for text, tag_color in lost:
            draw.text((x, y), text, font=tag_font, fill=tag_color)
            x += _measure(text, tag_font)[0] + 14
        y += _measure("✗", tag_font)[1]
    if reviewed is not None:
        y += 10
        card.alpha_composite(reviewed, (text_x, y))
    return card


def _paste_card(layer: Image.Image, card: Image.Image, x: int, y: int) -> None:
    """Card with a soft drop shadow, composited onto an RGBA layer."""
    m = 14
    shadow = Image.new("RGBA", (card.width + m * 2, card.height + m * 2), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [m, m + 3, m + card.width - 1, m + card.height + 2],
        radius=10, fill=(28, 36, 48, 70),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))
    layer.alpha_composite(shadow, (x - m, y - m))
    layer.alpha_composite(card, (x, y))


# ---------------------------------------------------------------------------
# Page annotation
# ---------------------------------------------------------------------------

def annotate_pages(page_paths: list[Path], transcript: Transcript,
                   report: SubmissionReport, out_dir: Path) -> list[str]:
    """Draw graded washes + gutter cards on each image page.

    Returns written filenames. All grade UI lives in a white gutter appended
    to the right of the page so it never obscures the student's writing.
    """
    results_by_id = {r.evaluation.question_id: r for r in report.results}
    written: list[str] = []

    pages: list[Image.Image | None] = []
    layers: list[Image.Image | None] = []   # RGBA overlay per page
    base_widths: list[int] = []
    for path in page_paths:
        if path.suffix.lower() == ".pdf":
            pages.append(None)              # no pixel space to draw on
            layers.append(None)
            base_widths.append(0)
            continue
        original = Image.open(path).convert("RGB")
        canvas = Image.new("RGB", (original.width + GUTTER, original.height), "white")
        canvas.paste(original, (0, 0))
        ImageDraw.Draw(canvas).line(
            [(original.width + 4, 0), (original.width + 4, original.height)],
            fill=LINE, width=2,
        )
        pages.append(canvas.convert("RGBA"))
        layers.append(Image.new("RGBA", canvas.size, (0, 0, 0, 0)))
        base_widths.append(original.width)

    # Track how far down each page's gutter is occupied to avoid overlaps.
    gutter_floor = [12] * len(pages)

    for answer in transcript.answers:
        result = results_by_id.get(answer.question_id)
        if result is None:
            continue
        ev = result.evaluation
        color = _color_for(result.final_marks, ev.max_marks)

        for i, region in enumerate(answer.regions):
            if not 0 <= region.page_index < len(pages):
                continue
            page = pages[region.page_index]
            layer = layers[region.page_index]
            if page is None:
                continue
            base_w = base_widths[region.page_index]
            draw = ImageDraw.Draw(layer)
            x1 = max(0, min(region.x1, base_w - 1))
            y1 = max(0, min(region.y1, page.height - 1))
            x2 = max(x1 + 1, min(region.x2, base_w))
            y2 = max(y1 + 1, min(region.y2, page.height))
            radius = max(2, min(12, (x2 - x1) // 4, (y2 - y1) // 4))
            # translucent wash + rounded outline: highlighter, not a fence
            draw.rounded_rectangle([x1, y1, x2, y2], radius=radius,
                                   fill=color + (24,))
            draw.rounded_rectangle([x1, y1, x2, y2], radius=radius,
                                   outline=color + (255,), width=3)

            if i == 0:  # one grade card per question, beside its first region
                card = _grade_card(result)
                cx = base_w + 18
                cy = max(y1, gutter_floor[region.page_index])
                cy = max(12, min(cy, page.height - card.height - 12))
                _paste_card(layer, card, cx, cy)
                gutter_floor[region.page_index] = cy + card.height + 16
                _curve(draw, (x2 - 2, min(y1 + 12, y2)), (cx + 2, cy + 22), color)

    out_dir.mkdir(parents=True, exist_ok=True)
    for index, page in enumerate(pages):
        if page is None:
            continue
        final = Image.alpha_composite(page, layers[index]).convert("RGB")
        name = f"page_{index}_annotated.png"
        final.save(out_dir / name)
        written.append(name)
    return written


# ---------------------------------------------------------------------------
# Summary card
# ---------------------------------------------------------------------------

def render_summary_card(rubric: Rubric, report: SubmissionReport,
                        out_dir: Path) -> str:
    """A closing card: header, score donut, per-question mark bars with the
    lost criteria spelled out, and the tag legend."""
    W, M = 1080, 48
    title_font = _font(31)
    sub_font = _font(17, bold=False)
    q_font = _font(23)
    body_font = _font(18, bold=False)
    small_font = _font(16, bold=False)
    tag_font = _font(17)

    criteria_by_id = {c.id: c for q in rubric.questions for c in q.criteria}
    pct = (report.total_awarded / report.total_available
           if report.total_available > 0 else 0.0)
    adjusted = sum(1 for r in report.results if r.override is not None)

    # Layout as measured blocks: (height, draw(canvas, draw, y)).
    blocks: list[tuple[int, object]] = []

    def block(height):
        def register(fn):
            blocks.append((height, fn))
            return fn
        return register

    @block(108)
    def header(card, draw, y):
        draw.rectangle([0, y, W, y + 108], fill=INK)
        draw.text((M, y + 22), "Result summary", font=title_font, fill="white")
        disposition, _ = DISPOSITIONS.get(report.strictness, DISPOSITIONS[0])
        who = f"{report.student_identifier} · " if report.student_identifier else ""
        draw.text((M, y + 68), f"{who}{rubric.title} · Marked: {disposition}",
                  font=sub_font, fill=(203, 210, 220))

    @block(158)
    def hero(card, draw, y):
        donut = _donut(pct)
        card.alpha_composite(donut, (M, y + 20))
        tx = M + donut.width + 36
        draw.text((tx, y + 26), "TOTAL", font=_font(15), fill=MUTED)
        draw.text((tx, y + 48),
                  f"{_fmt(report.total_awarded)} / {_fmt(report.total_available)}",
                  font=_font(42), fill=INK)
        facts = []
        if report.questions_flagged_for_review:
            facts.append(f"{report.questions_flagged_for_review} question(s) "
                         "flagged for human review")
        if adjusted:
            facts.append(f"{adjusted} adjusted by a reviewer")
        if facts:
            draw.text((tx, y + 106), " · ".join(facts), font=small_font, fill=MUTED)

    bar_x, bar_w = M + 92, 300

    def question_block(result: QuestionResult):
        ev = result.evaluation
        color = _color_for(result.final_marks, ev.max_marks)
        comment_lines = textwrap.wrap(ev.overall_comment or "", 96)
        lost = [c for c in ev.criteria if c.met != "fully"]
        lost_lines = []
        for c in lost:
            criterion = criteria_by_id.get(c.criterion_id)
            desc = criterion.description if criterion else ""
            head = ("✗" if c.met == "not_met" else "~") + f" [{c.criterion_id}] "
            lost_lines.append((head, RED if c.met == "not_met" else AMBER,
                               textwrap.wrap(desc, 78),
                               textwrap.wrap(f"{_fmt(c.marks_awarded)} marks — "
                                             f"{c.rationale}", 86)))
        height = (26 + 8 + len(comment_lines) * 24
                  + sum(6 + len(d) * 24 + len(r) * 21 for _, _, d, r in lost_lines)
                  + 22)

        def draw_it(card, draw, y):
            draw.text((M, y), f"Q{ev.question_id}", font=q_font, fill=INK)
            by = y + 7
            draw.rounded_rectangle([bar_x, by, bar_x + bar_w, by + 12],
                                   radius=6, fill=SOFT)
            ratio = (result.final_marks / ev.max_marks) if ev.max_marks > 0 else 0
            if ratio > 0:
                draw.rounded_rectangle(
                    [bar_x, by, bar_x + max(12, int(bar_w * min(ratio, 1))), by + 12],
                    radius=6, fill=color)
            score = f"{_fmt(result.final_marks)}/{_fmt(ev.max_marks)}"
            draw.text((bar_x + bar_w + 18, y), score, font=q_font, fill=color)
            if result.override:
                sx = bar_x + bar_w + 18 + _measure(score, q_font)[0] + 14
                draw.text((sx, y + 3), "✓ reviewed", font=tag_font, fill=ACCENT)
            yy = y + 26 + 8
            for line in comment_lines:
                draw.text((M + 24, yy), line, font=body_font, fill=MUTED)
                yy += 24
            for head, tag_color, desc_lines, rat_lines in lost_lines:
                yy += 6
                draw.text((M + 24, yy), head, font=tag_font, fill=tag_color)
                indent = M + 24 + _measure(head, tag_font)[0] + 4
                for j, line in enumerate(desc_lines or [""]):
                    draw.text((indent if j == 0 else M + 52, yy), line,
                              font=body_font, fill=INK)
                    yy += 24
                for line in rat_lines:
                    draw.text((M + 52, yy), line, font=small_font, fill=MUTED)
                    yy += 21
            draw.line([(M, y + height - 12), (W - M, y + height - 12)],
                      fill=LINE, width=1)

        blocks.append((height, draw_it))

    for result in report.results:
        question_block(result)

    @block(66)
    def legend(card, draw, y):
        draw.rectangle([0, y + 10, W, y + 66], fill=SOFT)
        draw.text((M, y + 28),
                  "● criterion earned    ◐ partially met    ○ missed        "
                  "wash colours:  green full marks · amber partial · red none",
                  font=small_font, fill=MUTED)

    height = sum(h for h, _ in blocks) + 20
    card = Image.new("RGBA", (W, height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(card)
    y = 0
    for h, fn in blocks:
        fn(card, draw, y)
        y += h
    draw.rectangle([0, 0, W - 1, height - 1], outline=LINE, width=2)

    out_dir.mkdir(parents=True, exist_ok=True)
    name = "summary_card.png"
    card.convert("RGB").save(out_dir / name)
    return name


def annotate_submission(job: Job, rubric: Rubric, transcript: Transcript,
                        report: SubmissionReport, page_paths: list[Path],
                        out_dir: Path) -> list[str]:
    """Produce all annotation artifacts; returns filenames in display order."""
    files = annotate_pages(page_paths, transcript, report, out_dir)
    files.append(render_summary_card(rubric, report, out_dir))
    return files
