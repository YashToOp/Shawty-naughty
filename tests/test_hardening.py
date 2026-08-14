"""Public-exposure guardrails: rate limits, daily cap, purge, delete, verify."""

from fastapi.testclient import TestClient

from app import config, guard, pipeline, storage
from app.guard import SlidingWindow
from app.main import app
from app.models import BankQuestion, Criterion, QuestionBank, SetVariant
from tests.conftest import FakeClient


def contribute(http):
    return http.post(
        "/api/contribute",
        data={"board": "CBSE", "class_level": "12", "subject": "Physics",
              "exam_year": "2027"},
        files=[("files", ("qp.png", b"img", "image/png"))],
    )


# ---------------------------------------------------------------------------
# Sliding window unit behaviour
# ---------------------------------------------------------------------------

def test_sliding_window_limits_and_expires():
    now = [0.0]
    window = SlidingWindow(lambda: 2, per_seconds=3600, clock=lambda: now[0])
    assert window.allow("ip") and window.allow("ip")
    assert not window.allow("ip")            # third hit inside the hour
    assert window.allow("other-ip")          # independent per key
    now[0] += 3601
    assert window.allow("ip")                # window rolled over


def test_zero_limit_disables_check():
    window = SlidingWindow(lambda: 0, per_seconds=3600)
    assert all(window.allow("ip") for _ in range(50))


# ---------------------------------------------------------------------------
# Endpoint throttling
# ---------------------------------------------------------------------------

def test_contribution_rate_limit_returns_429(monkeypatch):
    monkeypatch.setattr(config, "CONTRIBUTIONS_PER_HOUR_PER_IP", 2)
    monkeypatch.setattr(pipeline, "get_client", lambda: FakeClient([]))
    with TestClient(app) as http:
        assert contribute(http).status_code == 202
        assert contribute(http).status_code == 202
        response = contribute(http)
    assert response.status_code == 429
    assert "wait" in response.json()["detail"].lower()


def test_daily_capacity_cap_returns_503(monkeypatch):
    monkeypatch.setattr(config, "GLOBAL_JOBS_PER_DAY", 1)
    monkeypatch.setattr(pipeline, "get_client", lambda: FakeClient([]))
    with TestClient(app) as http:
        assert contribute(http).status_code == 202
        response = contribute(http)
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Retention + deletion
# ---------------------------------------------------------------------------

def test_delete_student_submission_removes_everything():
    job = storage.create_job(None, [("sheet.png", b"img")], kind="student")
    with TestClient(app) as http:
        assert http.delete(f"/api/student/submissions/{job.id}").status_code == 200
        assert http.get(f"/api/student/submissions/{job.id}").status_code == 404
        assert http.delete(f"/api/student/submissions/{job.id}").status_code == 404


def test_purge_expired_removes_only_old_jobs():
    old = storage.create_job(None, [("a.png", b"x")], kind="student")
    fresh = storage.create_job(None, [("b.png", b"x")], kind="student")
    storage.update_job(old)
    old.updated_at = "2020-01-01T00:00:00+00:00"
    storage._write_job(old)

    assert storage.purge_expired(30) == 1
    assert storage.get_job(old.id) is None
    assert storage.get_job(fresh.id) is not None
    assert storage.purge_expired(0) == 0     # retention disabled


# ---------------------------------------------------------------------------
# Scheme verification
# ---------------------------------------------------------------------------

def seed_bank():
    return storage.save_bank(QuestionBank(
        board="CBSE", class_level="12", subject="Physics", year=2027,
        title="Physics 2027", rubric_status="ai_generated",
        questions=[BankQuestion(
            id="B01", question_text="Define flux.", max_marks=2,
            criteria=[Criterion(id="B01-a", description="Definition", marks=2)],
        )],
        variants=[SetVariant(paper_code="55/9/9", question_map={"1": "B01"})],
    ))


def test_verify_bank_propagates_to_materialized_papers():
    bank = seed_bank()
    paper = storage.materialize_variant(bank, bank.variants[0])
    assert paper.rubric_status == "ai_generated"

    with TestClient(app) as http:
        response = http.post(f"/api/banks/{bank.id}/verify",
                             json={"reviewer": "AK"})
    assert response.status_code == 200
    assert response.json()["rubric_status"] == "verified"
    assert response.json()["verified_by"] == "AK"

    refreshed = storage.get_paper(paper.id)
    assert refreshed.rubric_status == "verified"
    assert refreshed.verified_by == "AK"


def test_verify_paper_requires_a_scheme():
    bank = seed_bank()
    paper = storage.materialize_variant(bank, bank.variants[0])
    with TestClient(app) as http:
        ok = http.post(f"/api/papers/{paper.id}/verify", json={"reviewer": "AK"})
        missing = http.post("/api/papers/nope/verify", json={"reviewer": "AK"})
    assert ok.status_code == 200 and ok.json()["rubric_status"] == "verified"
    assert missing.status_code == 404
