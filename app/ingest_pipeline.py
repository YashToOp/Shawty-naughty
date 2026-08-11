"""Public paper-contribution flow: question paper in -> bank entry + scheme out.

This is the public front door during the coverage phase: anyone uploads a
question paper with its board/class/subject/year, and the platform extracts
the question list, dedupes it against the subject-year question bank, writes
marking schemes only for genuinely new questions, and registers the set code
as a bank variant. Grading is deliberately not part of this flow — coverage
first, features once the banks are full.

All model calls run on the server's own configured providers (see
EVAL_PROVIDER / OCR_PROVIDER in config.py); contributors never supply keys.
"""

import logging

from . import paper_ingest, pipeline, storage
from .models import Job

logger = logging.getLogger(__name__)


def run_ingest_pipeline(job_id: str) -> None:
    job = storage.get_job(job_id)
    if job is None:
        logger.error("Ingest pipeline started for unknown job %s", job_id)
        return
    try:
        _ingest(job)
    except Exception as exc:
        logger.exception("Ingest job %s failed", job_id)
        storage.update_job(job, status="failed", error=str(exc))


def _ingest(job: Job) -> None:
    client = pipeline.get_client()
    meta = job.metadata

    storage.update_job(job, status="reading_paper")
    extracted = pipeline.paper_reader().extract_questions(
        client, storage.job_upload_paths(job)
    )

    # Already-registered sets short-circuit: coverage exists, nothing to add.
    code = extracted.paper_code or (meta.paper_code if meta else None)
    year = meta.exam_year if meta else None
    if code and year:
        hit = storage.find_bank_variant(code, year)
        if hit is not None:
            paper = storage.materialize_variant(*hit)
            job.paper_id = paper.id
            storage.update_job(
                job, status="completed",
                note=f"Set {code} is already registered — no new schemes needed.",
            )
            return

    storage.update_job(job, status="generating_rubric")
    bank = storage.find_bank_for(meta) if meta else None
    known_before = {q.id for q in bank.questions} if bank else set()
    bank, variant = paper_ingest.ingest_into_bank(client, meta, extracted, bank)
    storage.save_bank(bank)
    paper = storage.materialize_variant(bank, variant)

    new = sum(1 for qid in set(variant.question_map.values())
              if qid not in known_before)
    reused = len(variant.question_map) - new
    job.paper_id = paper.id
    storage.update_job(
        job, status="completed",
        note=(f"Registered set {variant.paper_code}: schemes written for "
              f"{new} new question(s), {reused} reused from the bank."),
    )
