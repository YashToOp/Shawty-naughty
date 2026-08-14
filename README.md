# Answer Sheet Evaluator

Upload scanned answer sheets → OCR them into a faithful transcript → evaluate every answer against a marking scheme, with verbatim evidence, per-criterion rationale, confidence flags, and a human-override workflow.

The goal is grading that is **cheaper** than fully manual marking, **more consistent** than rushed human marking, and **fully transparent** — every mark can be traced to a rubric criterion and a quote from the student's own answer.

Three doors share one registry and grading pipeline:

- **Public paper contribution** (`/`) — the front door during the coverage phase. Anyone uploads a question paper with its board/class/subject/year; the platform extracts every question, dedupes against the subject-year bank, writes marking schemes only for genuinely new questions (on the server's own provider keys — contributors need no account), and registers the set. The page shows the extracted questions + schemes and live coverage.
- **Student self-service** (`/grade`) — a student uploads their own sheet and gets a graded, annotated result. The system reads the front page for exam metadata, finds the question paper in the registry, and writes the marking scheme itself when no institutional guidelines exist. Ships pre-seeded with a **CBSE Class 12 English Core 2026** paper.
- **Examiner tools** (`/examiner`) — an examiner picks a rubric, uploads sheets, reviews flagged questions, and applies audited overrides.

## Student self-service flow

```
 upload sheet ─▶ read front page ─▶ find paper in registry ─▶ marking scheme? ─▶ grade ─▶ result
                (board/class/       │ not found:                │ missing:
                 subject/year/      │  ask student to upload    │  AI writes the full
                 paper code)        │  the question paper,      │  scheme per question,
                                    │  or fix the paper code    │  saved to the registry
                                    ▼                           ▼
                              registered once            generated once
                              — every later student with the same paper code
                                goes straight to grading
```

The registry is the flywheel: the **first** student with a new paper code contributes the question paper (one upload), the system extracts the questions and writes a marking scheme following board conventions (CBSE English: Format / Content / Organisation / Accuracy for writing tasks; Content / Evidence / Organisation / Expression for literature), and **every subsequent student skips straight to grading**. AI-generated schemes are labeled as such in the result until an examiner verifies them.

## PYQ question banks & set variants

Board papers ship in many shuffled **sets** (Science 31/1/1, 31/2/1, 31/3/1 …) drawing on one question pool, so rubrics are stored once in an all-in-one **question bank** per board/class/subject/year (`sample_data/banks/`), and each set code is just a mapping of question numbers onto the bank. A sheet from any registered set materializes its paper (questions renumbered, criterion ids relabeled) from the bank on first lookup.

Uploads feed the same flywheel: when a student uploads an unregistered set, extracted questions are **deduped against the bank** (text similarity + exact marks guard — conservative, because a wrong merge would grade against the wrong rubric), rubrics are generated only for genuinely new questions, and the set is registered as a new variant. Coverage policy (`/api/coverage`): board-exam classes 10 and 12 are covered centrally via banks; classes 9 and below and class 11 use the upload path.

### Batch ingestion

`scripts/ingest_paper.py` pours a paper set into the banks from the command line (requires `ANTHROPIC_API_KEY`):

```bash
python scripts/ingest_paper.py pages/*.png \
  --board CBSE --class-level 10 --subject "Mathematics (Standard)" --year 2026
```

It extracts the questions, dedupes against the subject-year bank, generates rubrics only for new questions, registers the set variant, and reports reused-vs-new counts. `scripts/make_test_corpus.py` renders the original practice papers in `sample_data/test_corpus/` into page images (two Maths sets with overlapping questions, a Class 12 Physics set, and a Class 11 school paper) for exercising the pipeline end to end. Dedupe matching is deliberately conservative (high text similarity + exact marks): a missed match costs one redundant rubric, while a wrong merge would grade a student against the wrong scheme. Roadmap: adjudicate borderline pairs with a cheap model call during ingestion.

**Marking strictness** is a lever on upload — 0 Board standard (lenient, CBSE-style: spelling ignored outside language criteria, benefit of the doubt, error carried forward), 1 Balanced, 2 Strict (competitive-exam style, UPSC-like: only what is explicitly demonstrated earns marks). Strictness changes the judgment disposition only — never the rubric or its arithmetic — and the level used is disclosed on every result alongside the invariants (identity-blind, evidence-cited, totals computed in code).

## How it works

```
                ┌─────────────┐      ┌──────────────────┐      ┌───────────────────┐      ┌──────────────────┐
 scanned pages  │  1. Upload  │      │ 2. Transcription │      │  3. Evaluation    │      │  4. Annotation   │
 (PNG/JPG/PDF) ─▶  + rubric   ├─────▶│  (Claude vision) ├─────▶│  (per question,   ├─────▶│  boxes + grades  ├─▶ report + review UI
                │             │      │  verbatim OCR    │      │  against rubric)  │      │  on the sheet    │
                └─────────────┘      └──────────────────┘      └───────────────────┘      └──────────────────┘
```

1. **Upload** — an examiner defines a rubric once (questions, criteria, marks per criterion). Answer sheets are uploaded as images or PDFs.
2. **Transcription** — Claude's vision reads the pages and produces a structured transcript: verbatim answers keyed by question id, `[illegible]` markers instead of guesses, legibility ratings, notes about crossed-out work or diagrams, and **pixel bounding boxes** for each answer region.
3. **Evaluation** — each question is graded in a separate request against the rubric. The model must, for every criterion: award marks, state a rationale, and quote the exact words from the student's answer that earned the marks. Structured outputs guarantee the response always parses.
4. **Annotation** — the evaluation is drawn back onto the student's own sheet: each answer gets a color-coded box (green = full marks, amber = partial, red = none) stamped with the grade (`Q1 · 3/5`) and tags for the criteria that cost marks (`✗ 1-b  ~ 1-c`). A closing summary card totals the paper and spells out what every tag means, so students can see exactly why they didn't get full marks. PDF uploads are rasterized to page images at upload (`RASTER_DPI`, capped at `MAX_PDF_PAGES`), so they get annotated like any scan.
5. **Review** — the report shows totals, per-criterion breakdowns, evidence, and the annotated sheet. Questions with low confidence, illegible answers, or arithmetic corrections are flagged for human review. A reviewer can override any question's marks; overrides are stored with reviewer, reason, and timestamp, totals update, and the annotated sheet is redrawn with the reviewed marks.

## Design decisions

**Transparency**
- Transcription and grading are separate stages with separate stored artifacts — you can always see *what the model read* before judging *how it marked*.
- Every criterion decision carries a rationale and verbatim evidence quotes.
- Human overrides never erase the model's evaluation; both are kept side by side as an audit trail.

**Accuracy**
- Marks are proposed by the model but **clamped and recomputed in code**: criterion marks are bounded by the rubric, question totals are recomputed from criteria, and any correction flags the question for review.
- The transcriber is instructed never to guess at illegible writing; anything less than fully legible forces `needs_human_review`.
- One request per question keeps rationales focused and prevents one bad response from corrupting a whole paper.

**Cost**
- The grading instructions + full rubric sit behind a prompt-cache breakpoint, so for a multi-question paper (and across papers graded within the cache TTL) the shared prefix is served at ~10% of normal input cost.
- Human effort is spent only where it matters: reviewers are pointed at flagged questions instead of re-marking everything.
- Roadmap: the [Message Batches API](https://platform.claude.com/docs/en/build-with-claude/batch-processing) can cut token costs by a further 50% for overnight bulk grading.

## Model backends & the free stack

Every stage talks to models through one interface, so backends swap per stage
without touching prompts, rubric arithmetic, or the review workflow:

| | `EVAL_PROVIDER` (judges answers, writes schemes) | `OCR_PROVIDER` (reads the pages) |
|---|---|---|
| **Claude stack** (default, calibration baseline) | `anthropic` — Claude via the Anthropic SDK | `claude` — Claude vision reads pages directly (images + PDFs) |
| **Free stack** | `workers-ai` — open models on [Cloudflare Workers AI](https://developers.cloudflare.com/workers-ai/) (free tier: 10k neurons/day) | `google-vision` — [Google Cloud Vision OCR](https://cloud.google.com/vision) (free tier: 1000 pages/month) + one cheap text-only segmentation call (PDFs arrive pre-rasterized) |

On the free path, Vision returns raw text with per-word confidences and boxes;
a small text model only decides *which lines belong to which question* — answer
bounding boxes are computed in code as unions of Vision's own line boxes, and
legibility is floored in code from Vision's confidences (same "model judges,
code enforces" rule as the marking arithmetic). Low-confidence lines are
flagged in the segmentation prompt and cap the answer at `partial`, which
forces human review downstream.

`OCR_PROVIDER=cascade` puts a free local engine in front of Vision, so the
paid engine is bought only where the free one actually failed:

```
                    IMAGE
                      │
              Preprocessing              (EXIF rotation, grayscale, contrast)
                      │
              Page / region detection    (PaddleOCR's text detector)
                      │
                PaddleOCR                (free, self-hosted recognition)
                      │
             Confidence / quality        (mean confidence + recognised chars)
                 /          \
              HIGH           LOW
               │              │
            Accept       Google Vision   (that page only)
                              │
                       Compare results   (line-level match by geometry)
                              │
                       Resolve conflicts (agree → corroborate; disagree →
                              │           keep better read, flag the line)
                         Grading LLM     (shared segmentation → evaluation)
```

Printed pages (question papers, typed sheets) clear the gate at zero cost;
messy handwriting falls through page by page. Conflict resolution is
deterministic code, never another model call. PaddleOCR is an optional heavy
dependency (`pip install paddleocr paddlepaddle`); without it the cascade
degrades to the pure Vision path with a logged warning.

Mixed stacks work too (`EVAL_PROVIDER=anthropic` + `OCR_PROVIDER=google-vision`
keeps Claude's judgment while cutting vision-token cost). Compare stacks on the
same sheet before trusting a cheaper one:

```bash
python scripts/benchmark_models.py pages/*.png \
  --rubric sample_data/papers/cbse-12-english-core-2026.json \
  --stack anthropic --stack free
```

It prints per-question marks side by side plus total drift against the
baseline stack, with review flags marked.

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # add your ANTHROPIC_API_KEY
export $(grep -v '^#' .env | xargs)

uvicorn app.main:app --reload
```

Open http://localhost:8000 — the UI ships with an example rubric you can save and try immediately. A larger example lives in [`sample_data/rubric_example.json`](sample_data/rubric_example.json).

Run the tests (no API key needed — the Anthropic client is mocked):

```bash
pytest
```

## API

**Public contribution flow**

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/contribute` | Upload a question paper (`multipart`: `board`, `class_level`, `subject`, `exam_year`, optional `paper_code`, `files[]`) — extraction + bank dedupe + scheme generation run in the background |
| `GET` | `/api/contribute/{id}` | Status, outcome note (new vs reused questions), and the registered paper with its scheme |

Ingest job statuses: `queued → reading_paper → [generating_rubric] → completed` (or `failed`; already-registered sets complete straight after `reading_paper`).

**Student flow**

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/student/submissions` | Upload answer-sheet files; starts metadata extraction + registry lookup |
| `GET` | `/api/student/submissions/{id}` | Status + metadata + paper + transcript + report |
| `POST` | `/api/student/submissions/{id}/paper` | Upload the question paper for an `awaiting_paper` submission |
| `POST` | `/api/student/submissions/{id}/paper-code` | Correct a misread paper code and retry the registry lookup |
| `DELETE` | `/api/student/submissions/{id}` | Remove a submission and everything uploaded with it |
| `POST` | `/api/banks/{id}/verify` | Examiner vouches for a bank's schemes (`{reviewer}`) — propagates to its materialized papers |
| `POST` | `/api/papers/{id}/verify` | Examiner vouches for one paper's scheme (`{reviewer}`) |

Upload endpoints are throttled per client address and by a global daily cap
(429 / 503 with a plain-language `detail`); grading jobs run through a bounded
concurrency gate, and uploads are purged after `RETENTION_DAYS` (see
`.env.example` for all knobs).
| `GET` | `/api/papers` | Paper registry (board, subject, year, code, rubric status) |
| `GET` | `/api/papers/{id}` | Full paper: questions + marking scheme |

Student job statuses: `queued → extracting_metadata → [awaiting_paper → reading_paper] → [generating_rubric] → transcribing → evaluating → completed` (or `failed`).

**Examiner flow**

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/rubrics` | Create a rubric |
| `GET` | `/api/rubrics` | List rubrics |
| `GET` | `/api/rubrics/{id}` | Fetch one rubric |
| `POST` | `/api/submissions` | Upload answer-sheet files (`multipart`: `rubric_id`, `files[]`) — returns a job id, processing runs in the background |
| `GET` | `/api/submissions` | List jobs with statuses |
| `GET` | `/api/submissions/{id}` | Job status + transcript + report |
| `GET` | `/api/submissions/{id}/annotated/{file}` | Annotated sheet pages and summary card (PNG) |
| `POST` | `/api/submissions/{id}/questions/{qid}/override` | Record a human override (`marks_awarded`, `reason`, `reviewer`) |

Job statuses: `queued → transcribing → evaluating → completed` (or `failed` with an error message).

## Rubric format

```json
{
  "title": "Physics Quiz 1",
  "instructions": "No negative marking.",
  "questions": [
    {
      "id": "1",
      "question_text": "State Newton's second law and give its equation.",
      "max_marks": 4,
      "model_answer": "F = ma; net force is proportional to rate of change of momentum.",
      "criteria": [
        { "id": "1-a", "description": "States the law in words", "marks": 2 },
        { "id": "1-b", "description": "Gives the correct equation F = ma", "marks": 2 }
      ]
    }
  ]
}
```

Criterion marks must sum to the question's `max_marks` — the API rejects rubrics where they don't.

## Project layout

```
app/
  main.py             FastAPI routes (student flow, examiner tools, registry, UI)
  student_pipeline.py student flow: metadata → registry lookup → (ingest/generate) → grade
  pipeline.py         shared grading core: transcript → evaluation → annotation → report
  metadata.py         front-page metadata extraction (board/class/subject/year/code)
  paper_ingest.py     question-paper extraction + AI marking-scheme generation
  ocr.py              Claude vision transcription + answer bounding boxes
  evaluator.py        per-question rubric grading + mark clamping
  annotator.py        graded boxes/tags on the sheet + summary card
  models.py           Pydantic schemas (also the structured-output contracts)
  storage.py          filesystem persistence under DATA_DIR (incl. paper registry)
  static/             student wizard (/) and examiner UI (/examiner)
tests/                unit tests with a mocked Anthropic client
sample_data/          example rubric + seeded papers (CBSE 12 English Core 2026)
```

## Roadmap

- Batch grading of many sheets against one rubric (Message Batches API, −50% token cost)
- Optional-question groups ("answer any N of M") in report totals
- Transcript editing in the UI before evaluation runs
- Per-class analytics: criterion-level performance across a cohort
- Rubric builder UI (currently JSON)
- Authentication and multi-examiner roles
