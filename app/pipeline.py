"""Orchestrates a submission job: uploads -> transcript -> evaluation -> report.

grade_submission() is the shared core; the examiner flow (rubric chosen up
front) and the student flow (rubric resolved from the paper registry) both
end here.
"""

import logging

import anthropic

from . import annotator, evaluator, ocr, storage
from .models import Job, Rubric, SubmissionReport

logger = logging.getLogger(__name__)


def get_client() -> anthropic.Anthropic:
    """Single place tests monkeypatch to inject a fake client."""
    return anthropic.Anthropic()


def run_pipeline(job_id: str) -> None:
    job = storage.get_job(job_id)
    if job is None:
        logger.error("Pipeline started for unknown job %s", job_id)
        return

    try:
        _run(job)
    except Exception as exc:  # surface any failure on the job record
        logger.exception("Job %s failed", job_id)
        storage.update_job(job, status="failed", error=str(exc))


def _run(job: Job) -> None:
    rubric = storage.get_rubric(job.rubric_id) if job.rubric_id else None
    if rubric is None:
        raise RuntimeError(f"Rubric {job.rubric_id} not found")
    grade_submission(job, rubric, get_client())


def grade_submission(job: Job, rubric: Rubric,
                     client: anthropic.Anthropic) -> SubmissionReport:
    """Transcribe -> evaluate -> annotate -> report. Marks job completed."""
    storage.update_job(job, status="transcribing")
    transcript = ocr.transcribe(client, rubric, storage.job_upload_paths(job))
    storage.save_transcript(job.id, transcript)

    storage.update_job(job, status="evaluating")
    results = evaluator.evaluate_submission(client, rubric, transcript)

    report = SubmissionReport.build(rubric, transcript, results)
    storage.save_report(job.id, report)

    # Annotation is an overlay on top of a finished evaluation — a drawing
    # failure must not fail the job.
    try:
        annotated = annotator.annotate_submission(
            job, rubric, transcript, report,
            storage.job_upload_paths(job), storage.annotated_dir(job.id),
        )
        job.annotated_files = annotated
    except Exception:
        logger.exception("Job %s: annotation failed", job.id)

    storage.update_job(job, status="completed")
    return report
