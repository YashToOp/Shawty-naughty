from app import storage
from app.models import Criterion, Question, Rubric


def make_rubric():
    return Rubric(
        title="Quiz",
        questions=[
            Question(
                id="1",
                question_text="Q1",
                max_marks=2,
                criteria=[Criterion(id="1-a", description="d", marks=2)],
            )
        ],
    )


def test_rubric_roundtrip():
    saved = storage.save_rubric(make_rubric())
    assert saved.id
    loaded = storage.get_rubric(saved.id)
    assert loaded is not None
    assert loaded.title == "Quiz"
    assert storage.get_rubric("nope") is None
    assert any(r.id == saved.id for r in storage.list_rubrics())


def test_job_lifecycle():
    rubric = storage.save_rubric(make_rubric())
    job = storage.create_job(rubric.id, [("page1.png", b"fake-bytes")])

    assert job.status == "queued"
    assert job.uploaded_files == ["page1.png"]
    paths = storage.job_upload_paths(job)
    assert paths[0].read_bytes() == b"fake-bytes"

    storage.update_job(job, status="completed")
    reloaded = storage.get_job(job.id)
    assert reloaded.status == "completed"
    assert reloaded.updated_at >= reloaded.created_at

    assert any(j.id == job.id for j in storage.list_jobs())


def test_upload_filenames_are_sanitized():
    rubric = storage.save_rubric(make_rubric())
    job = storage.create_job(rubric.id, [("../../evil.png", b"x")])
    assert job.uploaded_files == ["evil.png"]
    assert storage.job_upload_paths(job)[0].read_bytes() == b"x"
