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
from .models import (
    Criterion,
    Job,
    Paper,
    PaperQuestion,
    Question,
    QuestionBank,
    Rubric,
    SetVariant,
    SheetMetadata,
    SubmissionReport,
    Transcript,
)


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


def _loose(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def find_paper(meta: SheetMetadata) -> Optional[Paper]:
    """Resolution order: registered paper by code+year -> question-bank set
    variant by code+year (materialized on demand) -> registered paper by
    board/class/subject/year fallback for misread codes."""
    papers = list_papers()

    if meta.paper_code and meta.exam_year:
        wanted = normalize_code(meta.paper_code)
        for paper in papers:
            if normalize_code(paper.paper_code) == wanted and paper.year == meta.exam_year:
                return paper

        hit = find_bank_variant(meta.paper_code, meta.exam_year)
        if hit is not None:
            bank, variant = hit
            return materialize_variant(bank, variant)

    if meta.subject and meta.exam_year:
        for paper in papers:
            subject_match = (
                _loose(meta.subject) in _loose(paper.subject)
                or _loose(paper.subject) in _loose(meta.subject)
            )
            if (
                subject_match
                and paper.year == meta.exam_year
                and (not meta.board or _loose(meta.board) == _loose(paper.board))
                and (not meta.class_level
                     or _loose(meta.class_level) == _loose(paper.class_level))
            ):
                return paper
    return None


# ---------------------------------------------------------------------------
# Question banks (all-in-one PYQ store; one bank per board/class/subject/year)
# ---------------------------------------------------------------------------

def _banks_dir() -> Path:
    d = DATA_DIR / "banks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bank_storage_id(bank: QuestionBank) -> str:
    return (f"{_loose(bank.board)}-{_loose(bank.class_level)}-"
            f"{_loose(bank.subject)}_{bank.year}")


def save_bank(bank: QuestionBank) -> QuestionBank:
    bank.id = bank_storage_id(bank)
    (_banks_dir() / f"{bank.id}.json").write_text(bank.model_dump_json(indent=2))
    return bank


def get_bank(bank_id: str) -> Optional[QuestionBank]:
    path = _banks_dir() / f"{Path(bank_id).name}.json"
    if not path.exists():
        return None
    return QuestionBank.model_validate_json(path.read_text())


def list_banks() -> list[QuestionBank]:
    return sorted(
        (QuestionBank.model_validate_json(p.read_text())
         for p in _banks_dir().glob("*.json")),
        key=lambda b: (b.year, b.subject), reverse=True,
    )


def find_bank_variant(paper_code: str,
                      year: int) -> Optional[tuple[QuestionBank, SetVariant]]:
    wanted = normalize_code(paper_code)
    for bank in list_banks():
        if bank.year != year:
            continue
        for variant in bank.variants:
            if normalize_code(variant.paper_code) == wanted:
                return bank, variant
    return None


def find_bank_for(meta: SheetMetadata) -> Optional[QuestionBank]:
    """Bank for this sheet's board/class/subject/year (for upload dedupe)."""
    if not (meta.subject and meta.exam_year):
        return None
    for bank in list_banks():
        subject_match = (
            _loose(meta.subject) in _loose(bank.subject)
            or _loose(bank.subject) in _loose(meta.subject)
        )
        if (
            subject_match and bank.year == meta.exam_year
            and (not meta.board or _loose(meta.board) == _loose(bank.board))
            and (not meta.class_level
                 or _loose(meta.class_level) == _loose(bank.class_level))
        ):
            return bank
    return None


def _numeric_sort_key(qnum: str):
    return (0, int(qnum)) if qnum.isdigit() else (1, qnum)


def materialize_variant(bank: QuestionBank, variant: SetVariant) -> Paper:
    """Build (and persist) the Paper for one set code from the bank.

    Bank questions carry the master rubric; materialization renumbers them to
    the set's question numbers and relabels criterion ids to match."""
    by_id = {q.id: q for q in bank.questions}
    paper_questions: list[PaperQuestion] = []
    rubric_questions: list[Question] = []

    for qnum in sorted(variant.question_map, key=_numeric_sort_key):
        bq = by_id[variant.question_map[qnum]]
        paper_questions.append(PaperQuestion(
            id=qnum, section=bq.section,
            question_text=bq.question_text, max_marks=bq.max_marks,
        ))
        rubric_questions.append(Question(
            id=qnum, question_text=bq.question_text, max_marks=bq.max_marks,
            criteria=[
                Criterion(id=f"{qnum}-{chr(ord('a') + i)}",
                          description=c.description, marks=c.marks)
                for i, c in enumerate(bq.criteria)
            ],
        ))

    paper = Paper(
        board=bank.board, class_level=bank.class_level, subject=bank.subject,
        year=bank.year, paper_code=variant.paper_code,
        title=f"{bank.title} — Set {variant.paper_code}",
        questions=paper_questions,
        rubric=Rubric(title=f"{bank.title} — Marking Scheme",
                      instructions=bank.instructions,
                      questions=rubric_questions),
        rubric_status=bank.rubric_status,
        source_bank=bank.id,
    )
    saved = save_paper(paper)
    saved.rubric.id = saved.id
    return save_paper(saved)


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


def delete_job(job_id: str) -> bool:
    """Remove a job and everything under it (uploads, transcripts, results).

    The registry (papers, banks) is untouched - it holds no student data."""
    import shutil

    directory = _job_dir(job_id)
    if not directory.exists():
        return False
    shutil.rmtree(directory)
    return True


def purge_expired(retention_days: int) -> int:
    """Delete job directories older than the retention window (0 = keep all).

    Age is judged by the job record's updated_at so an actively-polled job is
    never purged mid-flight; unreadable records fall back to directory mtime."""
    import shutil
    from datetime import timedelta

    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for job_file in _jobs_dir().glob("*/job.json"):
        try:
            stamp = Job.model_validate_json(job_file.read_text()).updated_at
            age_ref = datetime.fromisoformat(stamp)
        except Exception:
            age_ref = datetime.fromtimestamp(job_file.stat().st_mtime, timezone.utc)
        if age_ref < cutoff:
            shutil.rmtree(job_file.parent, ignore_errors=True)
            removed += 1
    return removed


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
