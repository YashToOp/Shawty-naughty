from app import paper_ingest, pipeline, storage, student_pipeline
from app.models import (
    Criterion,
    CriterionEvaluation,
    ExtractedQuestionPaper,
    GeneratedCriteria,
    Paper,
    PaperQuestion,
    Question,
    QuestionEvaluation,
    Rubric,
    SheetMetadata,
    TranscribedAnswer,
    Transcript,
)
from tests.conftest import FakeClient, FakeParseResponse


def meta(code="1/1/1", year=2026):
    return SheetMetadata(board="CBSE", class_level="12", subject="English Core",
                         exam_year=year, paper_code=code)


def registered_paper(with_rubric=True):
    paper = Paper(
        board="CBSE", class_level="12", subject="English Core", year=2026,
        paper_code="1/1/1", title="English Core 2026",
        questions=[PaperQuestion(id="1", question_text="Write a notice.", max_marks=4)],
    )
    if with_rubric:
        paper.rubric = Rubric(
            title="Scheme",
            questions=[Question(
                id="1", question_text="Write a notice.", max_marks=4,
                criteria=[
                    Criterion(id="1-a", description="Format", marks=1),
                    Criterion(id="1-b", description="Content", marks=2),
                    Criterion(id="1-c", description="Accuracy", marks=1),
                ],
            )],
        )
        paper.rubric_status = "verified"
    return storage.save_paper(paper)


def transcript_response():
    return FakeParseResponse(Transcript(answers=[
        TranscribedAnswer(question_id="1", answer_text="NOTICE ...", legibility="clear"),
    ]))


def evaluation_response():
    return FakeParseResponse(QuestionEvaluation(
        question_id="1", marks_awarded=3.0, max_marks=4.0,
        criteria=[
            CriterionEvaluation(criterion_id="1-a", met="fully", marks_awarded=1.0,
                                rationale="Correct format.", evidence=["NOTICE"]),
            CriterionEvaluation(criterion_id="1-b", met="partially", marks_awarded=1.0,
                                rationale="Venue missing.", evidence=["book fair"]),
            CriterionEvaluation(criterion_id="1-c", met="fully", marks_awarded=1.0,
                                rationale="Accurate language.", evidence=["..."]),
        ],
        overall_comment="Good notice.", confidence="high", needs_human_review=False,
    ))


def start_student_job():
    return storage.create_job(None, [("sheet.png", b"img")], kind="student")


def patch_client(monkeypatch, responses):
    client = FakeClient(responses)
    monkeypatch.setattr(pipeline, "get_client", lambda: client)
    return client


# ---------------------------------------------------------------------------

def test_paper_found_with_verified_scheme_grades_directly(monkeypatch):
    registered_paper(with_rubric=True)
    job = start_student_job()
    client = patch_client(monkeypatch, [
        FakeParseResponse(meta()), transcript_response(), evaluation_response(),
    ])

    student_pipeline.run_student_pipeline(job.id)

    job = storage.get_job(job.id)
    assert job.status == "completed"
    assert job.paper_id == "1-1-1_2026"
    report = storage.get_report(job.id)
    assert report.total_awarded == 3.0
    assert client.messages._responses == []  # exactly 3 calls, no rubric generation


def test_unknown_paper_pauses_awaiting_paper(monkeypatch):
    job = start_student_job()
    patch_client(monkeypatch, [FakeParseResponse(meta(code="9/9/9"))])

    student_pipeline.run_student_pipeline(job.id)

    job = storage.get_job(job.id)
    assert job.status == "awaiting_paper"
    assert job.metadata.paper_code == "9/9/9"


def test_uploaded_paper_registers_and_generates_scheme_once(monkeypatch):
    # first student: unknown paper -> uploads it -> extraction + generation run
    job = start_student_job()
    patch_client(monkeypatch, [FakeParseResponse(meta(code="2/2/2"))])
    student_pipeline.run_student_pipeline(job.id)
    assert storage.get_job(job.id).status == "awaiting_paper"

    storage.save_paper_uploads(storage.get_job(job.id), [("qp.png", b"img")])
    patch_client(monkeypatch, [
        FakeParseResponse(ExtractedQuestionPaper(
            title="English Core 2026 Set 2", paper_code="2/2/2",
            questions=[PaperQuestion(id="1", question_text="Write a notice.", max_marks=4)],
        )),
        FakeParseResponse(GeneratedCriteria(criteria=[
            Criterion(id="1-a", description="Format", marks=1),
            Criterion(id="1-b", description="Content", marks=2),
            Criterion(id="1-c", description="Accuracy", marks=1),
        ])),
        transcript_response(), evaluation_response(),
    ])
    student_pipeline.resume_with_uploaded_paper(job.id)

    job = storage.get_job(job.id)
    assert job.status == "completed"
    paper = storage.get_paper("2-2-2_2026")
    assert paper is not None
    assert paper.rubric_status == "ai_generated"
    assert paper.rubric.questions[0].criteria[1].marks == 2

    # second student, same paper code: no extraction, no generation
    job2 = start_student_job()
    client2 = patch_client(monkeypatch, [
        FakeParseResponse(meta(code="2/2/2")), transcript_response(), evaluation_response(),
    ])
    student_pipeline.run_student_pipeline(job2.id)
    assert storage.get_job(job2.id).status == "completed"
    assert client2.messages._responses == []


def test_corrected_paper_code_resumes(monkeypatch):
    registered_paper(with_rubric=True)
    job = start_student_job()
    # smudged code AND unreadable subject: neither lookup path can match
    smudged = SheetMetadata(board="CBSE", class_level="12", subject=None,
                            exam_year=2026, paper_code="smudged")
    patch_client(monkeypatch, [FakeParseResponse(smudged)])
    student_pipeline.run_student_pipeline(job.id)
    assert storage.get_job(job.id).status == "awaiting_paper"

    job = storage.get_job(job.id)
    job.metadata.paper_code = "1-1-1"  # student corrects; normalization matches 1/1/1
    storage.update_job(job)
    assert storage.find_paper(job.metadata).id == "1-1-1_2026"

    patch_client(monkeypatch, [transcript_response(), evaluation_response()])
    student_pipeline.resume_with_existing_paper(job.id, "1-1-1_2026")
    assert storage.get_job(job.id).status == "completed"


def test_find_paper_subject_fallback():
    registered_paper()
    found = storage.find_paper(SheetMetadata(
        board="cbse", class_level="12", subject="ENGLISH", exam_year=2026,
        paper_code="totally-wrong",
    ))
    assert found is not None and found.paper_code == "1/1/1"


def test_fit_criteria_repairs_bad_sums():
    crits = paper_ingest.fit_criteria("5", [
        Criterion(id="x", description="Format", marks=2),
        Criterion(id="5-b", description="Content", marks=4),
    ], max_marks=5)
    assert abs(sum(c.marks for c in crits) - 5) < 1e-6
    assert all(c.id.startswith("5-") for c in crits)
    assert all(c.marks > 0 for c in crits)

    # empty generation falls back to a single overall criterion
    fallback = paper_ingest.fit_criteria("7", [], max_marks=6)
    assert len(fallback) == 1 and fallback[0].marks == 6
