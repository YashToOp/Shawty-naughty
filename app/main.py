"""FastAPI application: student self-service flow, examiner tools, overrides.

Run with:  uvicorn app.main:app --reload
"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form,
                     HTTPException, UploadFile)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError

from . import (annotator, config, guard, ingest_pipeline, pipeline, rasterize,
               storage, student_pipeline)
from .config import MAX_UPLOAD_BYTES, SUPPORTED_MEDIA_TYPES
from .models import HumanOverride, Paper, QuestionBank, Rubric, SheetMetadata

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SAMPLE_DATA_DIR = Path(__file__).parent.parent / "sample_data"
SAMPLE_PAPERS_DIR = SAMPLE_DATA_DIR / "papers"
SAMPLE_BANKS_DIR = SAMPLE_DATA_DIR / "banks"
COVERAGE_FILE = SAMPLE_DATA_DIR / "coverage.json"


def seed_sample_papers() -> None:
    """Load bundled papers and question banks into the registry so the
    student flow works out of the box."""
    if SAMPLE_PAPERS_DIR.exists():
        for path in sorted(SAMPLE_PAPERS_DIR.glob("*.json")):
            try:
                paper = Paper.model_validate_json(path.read_text())
                if storage.get_paper(storage.paper_storage_id(paper)) is None:
                    storage.save_paper(paper)
                    logger.info("Seeded paper %s (%s)", paper.paper_code, paper.title)
            except Exception:
                logger.exception("Failed to seed sample paper %s", path.name)
    if SAMPLE_BANKS_DIR.exists():
        for path in sorted(SAMPLE_BANKS_DIR.glob("*.json")):
            try:
                bank = QuestionBank.model_validate_json(path.read_text())
                if storage.get_bank(storage.bank_storage_id(bank)) is None:
                    storage.save_bank(bank)
                    logger.info("Seeded bank %s (%d questions, %d sets)",
                                bank.title, len(bank.questions), len(bank.variants))
            except Exception:
                logger.exception("Failed to seed question bank %s", path.name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_sample_papers()
    purged = storage.purge_expired(config.RETENTION_DAYS)
    if purged:
        logger.info("Purged %d job(s) past the %d-day retention window",
                    purged, config.RETENTION_DAYS)
    yield


app = FastAPI(
    title="Answer Sheet Evaluator",
    description="OCR + rubric-based evaluation of scanned answer sheets.",
    lifespan=lifespan,
)


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
# Shared upload validation
# ---------------------------------------------------------------------------

async def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    if not files:
        raise HTTPException(400, "At least one file is required")
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
    try:
        # PDFs become page images here, once - every downstream stage
        # (vision, OCR, annotation) then works on pixels.
        return rasterize.expand_uploads(contents)
    except rasterize.RasterizeError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Public paper contribution (the coverage-phase front door)
# ---------------------------------------------------------------------------

@app.post("/api/contribute", status_code=202,
          dependencies=[Depends(guard.limit_contributions)])
async def contribute_paper(
    background: BackgroundTasks,
    board: str = Form(...),
    class_level: str = Form(...),
    subject: str = Form(...),
    exam_year: int = Form(...),
    paper_code: Optional[str] = Form(None),
    files: list[UploadFile] = File(...),
):
    """Upload a question paper; the platform extracts the questions, dedupes
    them against the subject-year bank, and writes marking schemes for the
    new ones. Model calls run on the server's own provider keys."""
    if not (1990 <= exam_year <= 2100):
        raise HTTPException(400, "exam_year looks wrong")
    contents = await _read_uploads(files)
    job = storage.create_job(None, contents, kind="ingest")
    job.metadata = SheetMetadata(
        board=board.strip(), class_level=class_level.strip(),
        subject=subject.strip(), exam_year=exam_year,
        paper_code=paper_code.strip() if paper_code else None,
    )
    storage.update_job(job)
    background.add_task(guard.run_gated, ingest_pipeline.run_ingest_pipeline, job.id)
    return {"job_id": job.id, "status": job.status}


@app.get("/api/contribute/{job_id}")
def get_contribution(job_id: str):
    job = storage.get_job(job_id)
    if job is None or job.kind != "ingest":
        raise HTTPException(404, "Contribution not found")
    paper = storage.get_paper(job.paper_id) if job.paper_id else None
    return {"job": job, "paper": paper}


# ---------------------------------------------------------------------------
# Student self-service flow
# ---------------------------------------------------------------------------

@app.post("/api/student/submissions", status_code=202,
          dependencies=[Depends(guard.limit_submissions)])
async def create_student_submission(
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    strictness: int = Form(0),
):
    if strictness not in (0, 1, 2):
        raise HTTPException(400, "strictness must be 0 (lenient), 1 (balanced) or 2 (strict)")
    contents = await _read_uploads(files)
    job = storage.create_job(None, contents, kind="student")
    storage.update_job(job, strictness=strictness)
    background.add_task(guard.run_gated,
                        student_pipeline.run_student_pipeline, job.id)
    return {"job_id": job.id, "status": job.status}


@app.delete("/api/student/submissions/{job_id}")
def delete_student_submission(job_id: str):
    """Remove a submission and everything uploaded with it (student privacy)."""
    job = storage.get_job(job_id)
    if job is None or job.kind != "student":
        raise HTTPException(404, "Submission not found")
    storage.delete_job(job_id)
    return {"deleted": job_id}


@app.get("/api/student/submissions/{job_id}")
def get_student_submission(job_id: str):
    job = storage.get_job(job_id)
    if job is None or job.kind != "student":
        raise HTTPException(404, "Submission not found")
    paper = storage.get_paper(job.paper_id) if job.paper_id else None
    return {
        "job": job,
        "paper": None if paper is None else {
            "id": paper.id,
            "title": paper.title,
            "board": paper.board,
            "class_level": paper.class_level,
            "subject": paper.subject,
            "year": paper.year,
            "paper_code": paper.paper_code,
            "rubric_status": paper.rubric_status,
            "question_count": len(paper.questions),
        },
        "transcript": storage.get_transcript(job_id),
        "report": storage.get_report(job_id),
    }


@app.post("/api/student/submissions/{job_id}/paper", status_code=202)
async def upload_question_paper(
    job_id: str,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    job = storage.get_job(job_id)
    if job is None or job.kind != "student":
        raise HTTPException(404, "Submission not found")
    if job.status != "awaiting_paper":
        raise HTTPException(409, "This submission is not waiting for a question paper")

    contents = await _read_uploads(files)
    storage.save_paper_uploads(job, contents)
    background.add_task(guard.run_gated,
                        student_pipeline.resume_with_uploaded_paper, job.id)
    return {"job_id": job.id, "status": "reading_paper"}


class PaperCodeRequest(BaseModel):
    paper_code: str
    exam_year: Optional[int] = None


@app.post("/api/student/submissions/{job_id}/paper-code")
def correct_paper_code(job_id: str, body: PaperCodeRequest,
                       background: BackgroundTasks):
    job = storage.get_job(job_id)
    if job is None or job.kind != "student":
        raise HTTPException(404, "Submission not found")
    if job.status != "awaiting_paper":
        raise HTTPException(409, "This submission is not waiting for a question paper")
    if job.metadata is None:
        raise HTTPException(409, "No metadata extracted for this submission")

    job.metadata.paper_code = body.paper_code
    if body.exam_year:
        job.metadata.exam_year = body.exam_year
    storage.update_job(job)

    paper = storage.find_paper(job.metadata)
    if paper is None:
        return {"found": False}
    background.add_task(
        student_pipeline.resume_with_existing_paper, job.id, paper.id
    )
    return {"found": True, "paper_title": paper.title}


# ---------------------------------------------------------------------------
# Paper registry & coverage
# ---------------------------------------------------------------------------

@app.get("/api/coverage")
def coverage():
    """What's centrally covered vs the upload path, plus live registry state."""
    manifest = {}
    if COVERAGE_FILE.exists():
        manifest = json.loads(COVERAGE_FILE.read_text())
    available = [
        {
            "kind": "bank", "id": b.id, "board": b.board,
            "class_level": b.class_level, "subject": b.subject, "year": b.year,
            "question_count": len(b.questions),
            "set_variants": [v.paper_code for v in b.variants],
            "rubric_status": b.rubric_status,
        }
        for b in storage.list_banks()
    ] + [
        {
            "kind": "paper", "id": p.id, "board": p.board,
            "class_level": p.class_level, "subject": p.subject, "year": p.year,
            "question_count": len(p.questions),
            "set_variants": [p.paper_code],
            "rubric_status": p.rubric_status,
        }
        for p in storage.list_papers() if p.source_bank is None
    ]
    return {**manifest, "available": available}


@app.get("/api/banks")
def list_banks():
    return [
        {
            "id": b.id, "board": b.board, "class_level": b.class_level,
            "subject": b.subject, "year": b.year, "title": b.title,
            "question_count": len(b.questions),
            "set_variants": [v.paper_code for v in b.variants],
            "rubric_status": b.rubric_status,
        }
        for b in storage.list_banks()
    ]


@app.get("/api/banks/{bank_id}")
def get_bank(bank_id: str) -> QuestionBank:
    bank = storage.get_bank(bank_id)
    if bank is None:
        raise HTTPException(404, "Question bank not found")
    return bank


@app.get("/api/papers")
def list_papers():
    return [
        {
            "id": p.id, "board": p.board, "class_level": p.class_level,
            "subject": p.subject, "year": p.year, "paper_code": p.paper_code,
            "title": p.title, "rubric_status": p.rubric_status,
            "question_count": len(p.questions),
        }
        for p in storage.list_papers()
    ]


@app.get("/api/papers/{paper_id}")
def get_paper(paper_id: str) -> Paper:
    paper = storage.get_paper(paper_id)
    if paper is None:
        raise HTTPException(404, "Paper not found")
    return paper


class VerifyRequest(BaseModel):
    reviewer: str


@app.post("/api/banks/{bank_id}/verify")
def verify_bank(bank_id: str, body: VerifyRequest) -> QuestionBank:
    """An examiner vouches for a bank's marking schemes: ai_generated ->
    verified, propagated to every paper already materialized from it."""
    bank = storage.get_bank(bank_id)
    if bank is None:
        raise HTTPException(404, "Question bank not found")
    bank.rubric_status = "verified"
    bank.verified_by = body.reviewer
    bank.verified_at = datetime.now(timezone.utc).isoformat()
    storage.save_bank(bank)
    for paper in storage.list_papers():
        if paper.source_bank == bank.id and paper.rubric_status != "verified":
            paper.rubric_status = "verified"
            paper.verified_by = body.reviewer
            paper.verified_at = bank.verified_at
            storage.save_paper(paper)
    return bank


@app.post("/api/papers/{paper_id}/verify")
def verify_paper(paper_id: str, body: VerifyRequest) -> Paper:
    paper = storage.get_paper(paper_id)
    if paper is None:
        raise HTTPException(404, "Paper not found")
    if paper.rubric is None:
        raise HTTPException(409, "This paper has no marking scheme to verify")
    paper.rubric_status = "verified"
    paper.verified_by = body.reviewer
    paper.verified_at = datetime.now(timezone.utc).isoformat()
    storage.save_paper(paper)
    return paper


# ---------------------------------------------------------------------------
# Examiner submissions
# ---------------------------------------------------------------------------

@app.post("/api/submissions", status_code=202,
          dependencies=[Depends(guard.limit_submissions)])
async def create_submission(
    background: BackgroundTasks,
    rubric_id: str = Form(...),
    files: list[UploadFile] = File(...),
):
    if storage.get_rubric(rubric_id) is None:
        raise HTTPException(404, "Rubric not found")
    contents = await _read_uploads(files)
    job = storage.create_job(rubric_id, contents)
    background.add_task(guard.run_gated, pipeline.run_pipeline, job.id)
    return {"job_id": job.id, "status": job.status}


@app.delete("/api/submissions/{job_id}")
def delete_submission(job_id: str):
    if storage.get_job(job_id) is None:
        raise HTTPException(404, "Submission not found")
    storage.delete_job(job_id)
    return {"deleted": job_id}


@app.get("/api/submissions")
def list_submissions():
    # Paper contributions are jobs too, but they aren't gradable submissions —
    # the examiner's table should only show sheets that produce a report.
    return [j for j in storage.list_jobs() if j.kind != "ingest"]


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


@app.get("/api/submissions/{job_id}/annotated/{filename}")
def get_annotated_file(job_id: str, filename: str):
    if storage.get_job(job_id) is None:
        raise HTTPException(404, "Submission not found")
    path = storage.annotated_path(job_id, filename)
    if path is None:
        raise HTTPException(404, "Annotated file not found")
    return FileResponse(path)


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
            _refresh_annotations(job_id, report)
            return report

    raise HTTPException(404, f"Question {question_id} not found in report")


def _refresh_annotations(job_id: str, report) -> None:
    """Redraw annotated sheets so grade stamps reflect reviewed marks."""
    job = storage.get_job(job_id)
    rubric = storage.get_rubric(job.rubric_id) if job else None
    transcript = storage.get_transcript(job_id)
    if not (job and rubric and transcript):
        return
    try:
        job.annotated_files = annotator.annotate_submission(
            job, rubric, transcript, report,
            storage.job_upload_paths(job), storage.annotated_dir(job_id),
        )
        storage.update_job(job)
    except Exception:  # annotation is an overlay; never fail the override
        import logging
        logging.getLogger(__name__).exception(
            "Job %s: annotation refresh failed", job_id
        )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def landing_page():
    """Scroll-driven story of the pipeline; the doors are at the end of it."""
    return FileResponse(STATIC_DIR / "landing.html")


@app.get("/contribute", include_in_schema=False)
def contribute_page():
    """Public paper contribution: the coverage-phase workhorse."""
    return FileResponse(STATIC_DIR / "contribute.html")


@app.get("/grade", include_in_schema=False)
def student_page():
    return FileResponse(STATIC_DIR / "student.html")


@app.get("/examiner", include_in_schema=False)
def examiner_page():
    return FileResponse(STATIC_DIR / "index.html")
