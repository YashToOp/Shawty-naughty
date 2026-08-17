"""Public paper-contribution flow: upload -> extract -> dedupe -> schemes."""

from fastapi.testclient import TestClient

from app import pipeline, storage
from app.main import app
from app.models import (
    Criterion,
    ExtractedQuestionPaper,
    GeneratedCriteria,
    PaperQuestion,
)
from tests.conftest import FakeClient, FakeParseResponse


def extraction(code="55/1/1"):
    return FakeParseResponse(ExtractedQuestionPaper(
        title="Physics Theory", paper_code=code,
        questions=[
            PaperQuestion(id="1", question_text="Define electric flux.",
                          max_marks=2),
            PaperQuestion(id="2", question_text="State Gauss's law.",
                          max_marks=3),
        ],
    ))


def criteria(qid, marks):
    return FakeParseResponse(GeneratedCriteria(criteria=[
        Criterion(id=f"{qid}-a", description="Statement", marks=marks - 1),
        Criterion(id=f"{qid}-b", description="Precision", marks=1),
    ]))


def contribute(client_http, code=None, subject="Physics Theory"):
    data = {"board": "CBSE", "class_level": "12", "subject": subject,
            "exam_year": "2027"}
    if code:
        data["paper_code"] = code
    return client_http.post(
        "/api/contribute", data=data,
        files=[("files", ("qp.png", b"img", "image/png"))],
    )


def test_contribution_registers_bank_and_returns_schemes(monkeypatch):
    fake = FakeClient([extraction(), criteria("B01", 2), criteria("B02", 3)])
    monkeypatch.setattr(pipeline, "get_client", lambda: fake)

    with TestClient(app) as http:
        response = contribute(http)
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        result = http.get(f"/api/contribute/{job_id}").json()

    job, paper = result["job"], result["paper"]
    assert job["status"] == "completed"
    assert "2 new question(s)" in job["note"]
    assert paper["paper_code"] == "55/1/1"
    assert paper["rubric_status"] == "ai_generated"
    assert len(paper["rubric"]["questions"][0]["criteria"]) == 2

    bank = storage.find_bank_variant("55/1/1", 2027)
    assert bank is not None


def test_duplicate_set_is_detected_not_regenerated(monkeypatch):
    fake = FakeClient([extraction(), criteria("B01", 2), criteria("B02", 3)])
    monkeypatch.setattr(pipeline, "get_client", lambda: fake)
    with TestClient(app) as http:
        first = contribute(http)
        job_id = http.get(
            f"/api/contribute/{first.json()['job_id']}").json()["job"]["id"]
        assert job_id

        # Same set again: extraction runs, but no scheme generation calls left
        # in the fake — the flow must short-circuit on the registered variant.
        fake2 = FakeClient([extraction()])
        monkeypatch.setattr(pipeline, "get_client", lambda: fake2)
        second = contribute(http)
        result = http.get(f"/api/contribute/{second.json()['job_id']}").json()

    assert result["job"]["status"] == "completed"
    assert "already registered" in result["job"]["note"]
    assert fake2.messages._responses == []


def test_overlapping_set_reuses_bank_schemes(monkeypatch):
    fake = FakeClient([extraction("55/1/1"), criteria("B01", 2), criteria("B02", 3)])
    monkeypatch.setattr(pipeline, "get_client", lambda: fake)
    with TestClient(app) as http:
        contribute(http, code="55/1/1")

        # A shuffled set sharing one question: only the new one gets a scheme.
        shuffled = FakeParseResponse(ExtractedQuestionPaper(
            title="Physics Theory", paper_code="55/2/1",
            questions=[
                PaperQuestion(id="1", question_text="State Gauss's law.",
                              max_marks=3),                       # known
                PaperQuestion(id="2", question_text="Derive the field of a "
                              "dipole on its axis.", max_marks=5),  # new
            ],
        ))
        fake2 = FakeClient([shuffled, criteria("B03", 5)])
        monkeypatch.setattr(pipeline, "get_client", lambda: fake2)
        second = contribute(http, code="55/2/1")
        result = http.get(f"/api/contribute/{second.json()['job_id']}").json()

    assert result["job"]["status"] == "completed"
    assert "1 new question(s), 1 reused" in result["job"]["note"]
    assert fake2.messages._responses == []


def test_contribution_validation():
    with TestClient(app) as http:
        # missing files
        response = http.post("/api/contribute", data={
            "board": "CBSE", "class_level": "12", "subject": "Physics",
            "exam_year": "2027"})
        assert response.status_code == 422

        # nonsense year
        response = http.post(
            "/api/contribute",
            data={"board": "CBSE", "class_level": "12", "subject": "Physics",
                  "exam_year": "12"},
            files=[("files", ("qp.png", b"img", "image/png"))],
        )
        assert response.status_code == 400

        # unsupported extension
        response = http.post(
            "/api/contribute",
            data={"board": "CBSE", "class_level": "12", "subject": "Physics",
                  "exam_year": "2027"},
            files=[("files", ("qp.exe", b"x", "application/octet-stream"))],
        )
        assert response.status_code == 400


def test_public_pages_routed():
    with TestClient(app) as http:
        assert "Reliable checking" in http.get("/").text     # landing story
        assert "Contribute" in http.get("/contribute").text  # coverage page
        assert "answer sheet" in http.get("/grade").text.lower()
