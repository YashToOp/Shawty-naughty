"""Filesystem-backed persistence for rubrics, jobs, transcripts and results.

Layout under DATA_DIR:
    rubrics/<rubric_id>.json
    jobs/<job_id>/job.json
    jobs/<job_id>/uploads/<original filenames>
    jobs/<job_id>/transcript.json
    jobs/<job_id>/report.json
"""

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import DATA_DIR
from .models import Job, Paper, Rubric, SheetMetadata, SubmissionReport, Transcript


def _rubrics_dir() -> Path:
    d = DATA_DIR / "rubrics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jobs_dir() -> Path:
    d = DATA_DIR / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_dir(job_id: str) -> Path:
    return _jobs_dir() / job_id


# ---------------------------------------------------------------------------
# Rubrics
# ---------------------------------------------------------------------------

def save_rubric(rubric: Rubric) -> Rubric:
    if not rubric.id:
        rubric.id = uuid.uuid4().hex[:12]
    path = _rubrics_dir() / f"{rubric.id}.json"
    path.write_text(rubric.model_dump_json(indent=2))
    return rubric


def get_rubric(rubric_id: str) -> Optional[Rubric]:
    path = _rubrics_dir() / f"{rubric_id}.json"
    if not path.exists():
        return None
    return Rubric.model_validate_json(path.read_text())


def list_rubrics() -> list[Rubric]:
    return sorted(
        (Rubric.model_validate_json(p.read_text()) for p in _rubrics_dir().glob("*.json")),
        key=lambda r: r.title,
    )


# ---------------------------------------------------------------------------
# Papers (question-paper + marking-scheme registry)
# ---------------------------------------------------------------------------

def _papers_dir() -> Path:
    d = DATA_DIR / "papers"
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_code(code: str) -> str:
    """'1/1/1' -> '1-1-1'; case/spacing/punctuation-insensitive key part."""
    return re.sub(r"[^a-z0-9]+", "-", code.strip().lower()).strip("-")


def paper_storage_id(paper: Paper) -> str:
    return f"{normalize_code(paper.paper_code)}_{paper.year}"


def save_paper(paper: Paper) -> Paper:
    paper.id = paper_storage_id(paper)
    (_papers_dir() / f"{paper.id}.json").write_text(paper.model_dump_json(indent=2))
    return paper


def get_paper(paper_id: str) -> Optional[Paper]:
    path = _papers_dir() / f"{Path(paper_id).name}.json"
    if not path.exists():
        return None
    return Paper.model_validate_json(path.read_text())


def list_papers() -> list[Paper]:
    return sorted(
        (Paper.model_validate_json(p.read_text()) for p in _papers_dir().glob("*.json")),
        key=lambda p: (p.year, p.subject),
        reverse=True,
    )


def find_paper(meta: SheetMetadata) -> Optional[Paper]:
    """Match by paper code + year first; fall back to board/class/subject/year."""
    papers = list_papers()

    if meta.paper_code and meta.exam_year:
        wanted = normalize_code(meta.paper_code)
        for paper in papers:
            if normalize_code(paper.paper_code) == wanted and paper.year == meta.exam_year:
                return paper

    def loose(s: Optional[str]) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    if meta.subject and meta.exam_year:
        for paper in papers:
            subject_match = (
                loose(meta.subject) in loose(paper.subject)
                or loose(paper.subject) in loose(meta.subject)
            )
            if (
                subject_match
                and paper.year == meta.exam_year
                and (not meta.board or loose(meta.board) == loose(paper.board))
                and (not meta.class_level
                     or loose(meta.class_level) == loose(paper.class_level))
            ):
                return paper
    return None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def create_job(rubric_id: Optional[str], files: list[tuple[str, bytes]],
               kind: str = "examiner") -> Job:
    job = Job(id=uuid.uuid4().hex[:12], rubric_id=rubric_id, kind=kind)
    uploads = _job_dir(job.id) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    for filename, data in files:
        safe_name = Path(filename).name
        (uploads / safe_name).write_bytes(data)
        job.uploaded_files.append(safe_name)
    _write_job(job)
    return job


def get_job(job_id: str) -> Optional[Job]:
    path = _job_dir(job_id) / "job.json"
    if not path.exists():
        return None
    return Job.model_validate_json(path.read_text())


def list_jobs() -> list[Job]:
    jobs = []
    for job_file in _jobs_dir().glob("*/job.json"):
        jobs.append(Job.model_validate_json(job_file.read_text()))
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)


def update_job(job: Job, **fields) -> Job:
    for key, value in fields.items():
        setattr(job, key, value)
    job.updated_at = datetime.now(timezone.utc).isoformat()
    _write_job(job)
    return job


def _write_job(job: Job) -> None:
    path = _job_dir(job.id) / "job.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(job.model_dump_json(indent=2))


def job_upload_paths(job: Job) -> list[Path]:
    uploads = _job_dir(job.id) / "uploads"
    return [uploads / name for name in job.uploaded_files]


def save_paper_uploads(job: Job, files: list[tuple[str, bytes]]) -> list[Path]:
    """Store question-paper files a student uploaded for an awaiting_paper job."""
    d = _job_dir(job.id) / "paper_uploads"
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for filename, data in files:
        path = d / Path(filename).name
        path.write_bytes(data)
        paths.append(path)
    return paths


def paper_upload_paths(job: Job) -> list[Path]:
    d = _job_dir(job.id) / "paper_uploads"
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.is_file())


def annotated_dir(job_id: str) -> Path:
    return _job_dir(job_id) / "annotated"


def annotated_path(job_id: str, filename: str) -> Optional[Path]:
    path = annotated_dir(job_id) / Path(filename).name
    return path if path.exists() else None


# ---------------------------------------------------------------------------
# Pipeline artifacts
# ---------------------------------------------------------------------------

def save_transcript(job_id: str, transcript: Transcript) -> None:
    (_job_dir(job_id) / "transcript.json").write_text(
        transcript.model_dump_json(indent=2)
    )


def get_transcript(job_id: str) -> Optional[Transcript]:
    path = _job_dir(job_id) / "transcript.json"
    if not path.exists():
        return None
    return Transcript.model_validate_json(path.read_text())


def save_report(job_id: str, report: SubmissionReport) -> None:
    (_job_dir(job_id) / "report.json").write_text(report.model_dump_json(indent=2))


def get_report(job_id: str) -> Optional[SubmissionReport]:
    path = _job_dir(job_id) / "report.json"
    if not path.exists():
        return None
    return SubmissionReport.model_validate_json(path.read_text())
