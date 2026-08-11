"""Student self-service flow: sheet in -> result out, resolving the question
paper and marking scheme from the registry along the way.

    upload sheet
        -> extract front-page metadata
        -> find paper in registry
              found + scheme  -> grade
              found, no scheme -> generate scheme once, save -> grade
              not found        -> pause (awaiting_paper); student uploads the
                                  question paper or corrects the paper code,
                                  then the flow resumes

Everything learned for a paper (questions, marking scheme) is written to the
registry, so the next student with the same paper code goes straight to
grading.
"""

import logging

import anthropic

from . import metadata, paper_ingest, pipeline, storage
from .models import Job, Paper

logger = logging.getLogger(__name__)


def run_student_pipeline(job_id: str) -> None:
    job = storage.get_job(job_id)
    if job is None:
        logger.error("Student pipeline started for unknown job %s", job_id)
        return
    try:
        _start(job)
    except Exception as exc:
        logger.exception("Student job %s failed", job_id)
        storage.update_job(job, status="failed", error=str(exc))


def _start(job: Job) -> None:
    client = pipeline.get_client()

    storage.update_job(job, status="extracting_metadata")
    pages = storage.job_upload_paths(job)
    job.metadata = metadata.extract(client, pages[0])
    storage.update_job(job)

    paper = storage.find_paper(job.metadata)
    if paper is None:
        storage.update_job(job, status="awaiting_paper")
        return
    _continue_with_paper(job, paper, client)


def resume_with_uploaded_paper(job_id: str) -> None:
    """Student uploaded the question paper for an awaiting_paper job."""
    job = storage.get_job(job_id)
    if job is None:
        return
    try:
        client = pipeline.get_client()
        storage.update_job(job, status="reading_paper")
        extracted = paper_ingest.extract_questions(
            client, storage.paper_upload_paths(job)
        )
        paper = paper_ingest.build_paper(job.metadata, extracted)

        existing = storage.get_paper(storage.paper_storage_id(paper))
        if existing is not None:  # someone registered it while we were reading
            paper = existing
        else:
            paper = storage.save_paper(paper)

        _continue_with_paper(job, paper, client)
    except Exception as exc:
        logger.exception("Student job %s failed while reading the paper", job_id)
        storage.update_job(job, status="failed", error=str(exc))


def resume_with_existing_paper(job_id: str, paper_id: str) -> None:
    """Student corrected the paper code and a registry match was found."""
    job = storage.get_job(job_id)
    paper = storage.get_paper(paper_id)
    if job is None or paper is None:
        return
    try:
        _continue_with_paper(job, paper, pipeline.get_client())
    except Exception as exc:
        logger.exception("Student job %s failed", job_id)
        storage.update_job(job, status="failed", error=str(exc))


def _continue_with_paper(job: Job, paper: Paper,
                         client: anthropic.Anthropic) -> None:
    if paper.rubric is None or paper.rubric_status == "missing":
        storage.update_job(job, status="generating_rubric")
        paper.rubric = paper_ingest.generate_rubric(client, paper)
        paper.rubric_status = "ai_generated"
        storage.save_paper(paper)

    if paper.rubric.id is None:
        paper.rubric.id = paper.id

    job.paper_id = paper.id
    storage.update_job(job)
    pipeline.grade_submission(job, paper.rubric, client)
