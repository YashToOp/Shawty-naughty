# Answer Sheet Evaluator

Upload scanned answer sheets → OCR them into a faithful transcript → evaluate every answer against a **human-authored rubric**, with verbatim evidence, per-criterion rationale, confidence flags, and a human-override workflow.

The goal is grading that is **cheaper** than fully manual marking, **more consistent** than rushed human marking, and **fully transparent** — every mark can be traced to a rubric criterion and a quote from the student's own answer.

## How it works

```
                ┌─────────────┐      ┌──────────────────┐      ┌───────────────────┐
 scanned pages  │  1. Upload  │      │ 2. Transcription │      │  3. Evaluation    │
 (PNG/JPG/PDF) ─▶  + rubric   ├─────▶│  (Claude vision) ├─────▶│  (per question,   ├─▶ report + review UI
                │             │      │  verbatim OCR    │      │  against rubric)  │
                └─────────────┘      └──────────────────┘      └───────────────────┘
```

1. **Upload** — an examiner defines a rubric once (questions, criteria, marks per criterion). Answer sheets are uploaded as images or PDFs.
2. **Transcription** — Claude's vision reads the pages and produces a structured transcript: verbatim answers keyed by question id, `[illegible]` markers instead of guesses, legibility ratings, and notes about crossed-out work or diagrams. The transcript is stored as its own artifact so it can be audited independently of the grading.
3. **Evaluation** — each question is graded in a separate request against the rubric. The model must, for every criterion: award marks, state a rationale, and quote the exact words from the student's answer that earned the marks. Structured outputs guarantee the response always parses.
4. **Review** — the report shows totals, per-criterion breakdowns, and evidence. Questions with low confidence, illegible answers, or arithmetic corrections are flagged for human review. A reviewer can override any question's marks; overrides are stored with reviewer, reason, and timestamp, and totals update accordingly.

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

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/rubrics` | Create a rubric |
| `GET` | `/api/rubrics` | List rubrics |
| `GET` | `/api/rubrics/{id}` | Fetch one rubric |
| `POST` | `/api/submissions` | Upload answer-sheet files (`multipart`: `rubric_id`, `files[]`) — returns a job id, processing runs in the background |
| `GET` | `/api/submissions` | List jobs with statuses |
| `GET` | `/api/submissions/{id}` | Job status + transcript + report |
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
  main.py        FastAPI routes (rubrics, submissions, overrides, UI)
  pipeline.py    background job: transcript → evaluation → report
  ocr.py         stage 1 — Claude vision transcription (structured output)
  evaluator.py   stage 2 — per-question rubric grading + mark clamping
  models.py      Pydantic schemas (also the structured-output contracts)
  storage.py     filesystem persistence under DATA_DIR
  static/        single-page review UI
tests/           unit tests with a mocked Anthropic client
sample_data/     example rubric
```

## Roadmap

- Batch grading of many sheets against one rubric (Message Batches API, −50% token cost)
- Transcript editing in the UI before evaluation runs
- Per-class analytics: criterion-level performance across a cohort
- Rubric builder UI (currently JSON)
- Authentication and multi-examiner roles
