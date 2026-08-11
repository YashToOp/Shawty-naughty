from pathlib import Path

from PIL import Image

from app import annotator, storage
from app.models import (
    AnswerRegion,
    Criterion,
    CriterionEvaluation,
    HumanOverride,
    Question,
    QuestionEvaluation,
    QuestionResult,
    Rubric,
    SubmissionReport,
    TranscribedAnswer,
    Transcript,
)


def make_rubric():
    return Rubric(
        id="r1",
        title="Quiz",
        questions=[
            Question(
                id="1", question_text="Q1", max_marks=4,
                criteria=[
                    Criterion(id="1-a", description="States the law", marks=2),
                    Criterion(id="1-b", description="Gives F = ma", marks=2),
                ],
            ),
            Question(
                id="2", question_text="Q2", max_marks=3,
                criteria=[Criterion(id="2-a", description="Correct answer", marks=3)],
            ),
        ],
    )


def make_result(qid, max_marks, crits, override=None):
    ev = QuestionEvaluation(
        question_id=qid,
        marks_awarded=sum(m for _, m, _ in crits),
        max_marks=max_marks,
        criteria=[
            CriterionEvaluation(criterion_id=c, marks_awarded=m, met=met,
                                rationale="because", evidence=["quote"])
            for c, m, met in crits
        ],
        overall_comment="comment",
        confidence="high",
        needs_human_review=False,
    )
    return QuestionResult(evaluation=ev, override=override)


def make_artifacts(tmp_path):
    page = tmp_path / "page0.png"
    Image.new("RGB", (800, 1000), "white").save(page)
    transcript = Transcript(
        answers=[
            TranscribedAnswer(
                question_id="1", answer_text="F = ma", legibility="clear",
                regions=[AnswerRegion(page_index=0, x1=50, y1=100, x2=700, y2=300)],
            ),
            TranscribedAnswer(
                # out-of-bounds coords must be clamped, not crash
                question_id="2", answer_text="2000 N", legibility="clear",
                regions=[AnswerRegion(page_index=0, x1=-20, y1=900, x2=2000, y2=5000)],
            ),
        ],
    )
    rubric = make_rubric()
    results = [
        make_result("1", 4, [("1-a", 2.0, "fully"), ("1-b", 1.0, "partially")]),
        make_result("2", 3, [("2-a", 0.0, "not_met")]),
    ]
    report = SubmissionReport.build(rubric, transcript, results)
    return page, rubric, transcript, report


def test_annotate_submission_writes_pages_and_summary(tmp_path):
    page, rubric, transcript, report = make_artifacts(tmp_path)
    job = storage.create_job("r1", [("page0.png", page.read_bytes())])
    out = tmp_path / "annotated"

    files = annotator.annotate_submission(
        job, rubric, transcript, report, [page], out
    )

    assert files == ["page_0_annotated.png", "summary_card.png"]
    for name in files:
        img = Image.open(out / name)
        assert img.size[0] > 0
    # annotated page keeps original height, gains the stamp gutter
    assert Image.open(out / "page_0_annotated.png").size == (
        800 + annotator.GUTTER, 1000)


def test_pdf_pages_get_summary_only(tmp_path):
    _, rubric, transcript, report = make_artifacts(tmp_path)
    for answer in transcript.answers:
        answer.regions = []
    pdf = tmp_path / "sheet.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    job = storage.create_job("r1", [("sheet.pdf", pdf.read_bytes())])

    files = annotator.annotate_submission(
        job, rubric, transcript, report, [pdf], tmp_path / "annotated"
    )
    assert files == ["summary_card.png"]


def test_override_marks_change_stamp_color_input(tmp_path):
    page, rubric, transcript, report = make_artifacts(tmp_path)
    report.results[1].override = HumanOverride(
        marks_awarded=3.0, reason="regrade", reviewer="t"
    )
    out = tmp_path / "annotated"
    files = annotator.annotate_pages([page], transcript, report, out)
    assert files == ["page_0_annotated.png"]
    # the override drove final marks to full: sanity-check via _color_for
    assert annotator._color_for(report.results[1].final_marks, 3.0) == annotator.GREEN
