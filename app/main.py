"""FastAPI application: rubric management, submission upload, results, overrides.

Run with:  uvicorn app.main:app --reload
"""

from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from . import pipeline, storage
from .config import MAX_UPLOAD_BYTES, SUPPORTED_MEDIA_TYPES
from .models import HumanOverride, Rubric

app = FastAPI(
    title="Answer Sheet Evaluator",
    description="OCR + rubric-based evaluation of scanned answer sheets.",
)

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Rubrics
# ---------------------------------------------------------------------------

@app.post("/api/rubrics", status_code=201)
def create_rubric(rubric: Rubric) -> Rubric:
    rubric.id = None  # ids are server-assigned
    return storage.save_rubric(rubric)


@app.get("/api/rubrics")
def get_rubrics() -> list[Rubric]:
    return storage.list_rubrics()


@app.get("/api/rubrics/{rubric_id}")
def get_rubric(rubric_id: str) -> Rubric:
    rubric = storage.get_rubric(rubric_id)
    if rubric is None:
        raise HTTPException(404, "Rubric not found")
    return rubric


# ---------------------------------------------------------------------------
# Submissions
# ---------------------------------------------------------------------------

@app.post("/api/submissions", status_code=202)
async def create_submission(
    background: BackgroundTasks,
    rubric_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    if storage.get_rubric(rubric_id) is None:
        raise HTTPException(404, "Rubric not found")
    if not files:
        raise HTTPException(400, "At least one answer-sheet file is required")

    contents: list[tuple[str, bytes]] = []
    for upload in files:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in SUPPORTED_MEDIA_TYPES:
            raise HTTPException(
                400,
                f"Unsupported file type {suffix!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_MEDIA_TYPES))}",
            )
        data = await upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{upload.filename} exceeds the upload size limit")
        contents.append((upload.filename or f"page{len(contents)}{suffix}", data))

    job = storage.create_job(rubric_id, contents)
    background.add_task(pipeline.run_pipeline, job.id)
    return {"job_id": job.id, "status": job.status}


@app.get("/api/submissions")
def list_submissions():
    return storage.list_jobs()


@app.get("/api/submissions/{job_id}")
def get_submission(job_id: str):
    job = storage.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Submission not found")
    return {
        "job": job,
        "transcript": storage.get_transcript(job_id),
        "report": storage.get_report(job_id),
    }


# ---------------------------------------------------------------------------
# Human review / overrides
# ---------------------------------------------------------------------------

class OverrideRequest(BaseModel):
    marks_awarded: float
    reason: str
    reviewer: str


@app.post("/api/submissions/{job_id}/questions/{question_id}/override")
def override_marks(job_id: str, question_id: str, body: OverrideRequest):
    report = storage.get_report(job_id)
    if report is None:
        raise HTTPException(404, "No completed report for this submission")

    for result in report.results:
        if result.evaluation.question_id == question_id:
            if not 0 <= body.marks_awarded <= result.evaluation.max_marks:
                raise HTTPException(
                    400,
                    f"marks_awarded must be between 0 and {result.evaluation.max_marks}",
                )
            result.override = HumanOverride(
                marks_awarded=body.marks_awarded,
                reason=body.reason,
                reviewer=body.reviewer,
            )
            report.total_awarded = sum(r.final_marks for r in report.results)
            report.questions_flagged_for_review = sum(
                1 for r in report.results
                if r.evaluation.needs_human_review and r.override is None
            )
            storage.save_report(job_id, report)
            return report

    raise HTTPException(404, f"Question {question_id} not found in report")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
