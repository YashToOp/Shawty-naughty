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

from . import config, metadata, paper_ingest, pipeline, storage, vision_ocr
from .models import Job, Paper

logger = logging.getLogger(__name__)


def _extract_metadata(client, first_page):
    if config.OCR_PROVIDER == "google-vision":
        return vision_ocr.extract_metadata(client, first_page)
    return metadata.extract(client, first_page)


def _extract_questions(client, paper_files):
    if config.OCR_PROVIDER == "google-vision":
        return vision_ocr.extract_questions(client, paper_files)
    return paper_ingest.extract_questions(client, paper_files)


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
    job.metadata = _extract_metadata(client, pages[0])
    storage.update_job(job)

    paper = storage.find_paper(job.metadata)
    if paper is None:
        storage.update_job(job, status="awaiting_paper")
        return
    _continue_with_paper(job, paper, client)


def resume_with_uploaded_paper(job_id: str) -> None:
    """Student uploaded the question paper for an awaiting_paper job.

    The paper is folded into the subject-year question bank: questions seen in
    other set variants are reused with their existing rubrics; only new
    questions get schemes generated. The set code becomes a bank variant, so
    every later student with this code (or any registered shuffle of it)
    resolves instantly."""
    job = storage.get_job(job_id)
    if job is None:
        return
    try:
        client = pipeline.get_client()
        storage.update_job(job, status="reading_paper")
        extracted = _extract_questions(client, storage.paper_upload_paths(job))

        code = extracted.paper_code or (job.metadata.paper_code if job.metadata else None)
        year = job.metadata.exam_year if job.metadata else None
        if code and year:
            hit = storage.find_bank_variant(code, year)
            if hit is not None:  # registered while we were reading
                paper = storage.materialize_variant(*hit)
                _continue_with_paper(job, paper, client)
                return

        storage.update_job(job, status="generating_rubric")
        bank = storage.find_bank_for(job.metadata) if job.metadata else None
        bank, variant = paper_ingest.ingest_into_bank(
            client, job.metadata, extracted, bank
        )
        storage.save_bank(bank)
        paper = storage.materialize_variant(bank, variant)
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


def _continue_with_paper(job: Job, paper: Paper, client) -> None:
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
