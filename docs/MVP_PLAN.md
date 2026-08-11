# MVP Plan — Answer Sheet Evaluator

**Status:** Proof of concept complete (single-sheet pipeline: upload → OCR transcript → rubric evaluation → annotated sheet → human override).
**Target:** Enterprise MVP for schools, colleges, and coaching institutes, plus a public student-facing showcase.
**Timeline:** ~14 weeks from M1 kickoff to launch.

---

## 1. Product definition

| | |
|---|---|
| **Buyer** | Exam cells, department heads, principals — people accountable for grading quality, cost, and turnaround |
| **Users** | Teachers/examiners (run grading), reviewers/moderators (quality control), admins (org + policy), students (read-only results) |
| **Core promise** | Grade a whole class set overnight, at a fraction of manual cost, with every mark traceable to a rubric criterion and a quote from the student's own answer — and a human always in control |
| **Showcase** | A public sandbox where anyone (students, parents, prospects) can watch a sample sheet get graded end-to-end — doubles as the sales demo |

### What the MVP must prove to an enterprise buyer

1. **Trust** — measurable agreement with human graders, per exam (calibration reports)
2. **Throughput** — a 100–500 sheet cohort in one run, overnight
3. **Control** — review queue, overrides, audit trail; nothing auto-publishes
4. **Cost** — predictable per-sheet cost, shown *before* a run starts
5. **Safety** — tenant isolation, student PII handling, retention policy

### Explicitly out of MVP scope (post-launch roadmap)

LMS/SIS integrations (Google Classroom, Moodle), scanner hardware integration, on-prem deployment, non-English handwriting, auto-generating rubric drafts from question papers, mobile apps, fine-grained departmental billing.

---

## 2. Pipeline v2 (MVP)

The PoC's three stages grow to seven, connected by a worker queue. Every stage is idempotent and resumable; a failure re-runs the stage, never the whole job.

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ 1. INTAKE    │  │ 2. PREPROCESS│  │ 3. TRANSCRIBE│  │ 4. EVALUATE  │
│ bulk upload  │─▶│ PDF→PNG      │─▶│ Claude vision│─▶│ per-question │
│ zip/multi    │  │ page order   │  │ verbatim OCR │  │ vs rubric    │
│ sheet↔student│  │ quality check│  │ + bboxes     │  │ cached prefix│
│ mapping      │  │ (blur/DPI)   │  │              │  │ instant/batch│
└──────────────┘  └──────────────┘  └──────────────┘  └──────┬───────┘
                                                             │
┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│ 7. PUBLISH   │  │ 6. ANNOTATE  │  │ 5. QA GATE   │         │
│ marks export │◀─│ boxed sheets │◀─│ flag rules + │◀────────┘
│ analytics    │  │ summary card │  │ sampling →   │
│ share links  │  │ student PDF  │  │ review queue │
└──────────────┘  └──────────────┘  └──────────────┘
```

**New vs PoC:**

- **Intake & mapping** — upload a zip / folder / multi-page PDF for a whole class; auto-detect roll numbers from the sheets; manual fix-up screen for unmatched sheets. Sheet↔student mapping errors are the #1 operational failure mode in grading workflows, so this gets its own stage and UI.
- **Preprocess** — rasterize PDFs to images (so annotation works on every input), enforce page order, warn on blurry/low-DPI scans *before* spending tokens on them.
- **Evaluate: batch mode** — instant (interactive) or overnight via the Anthropic Message Batches API (−50% token cost). Overnight is the default for full cohorts.
- **QA gate** — configurable rules decide what enters the review queue: everything flagged by the model (low confidence, illegibility, clamped marks) + a random N% sample for ongoing calibration.
- **Publish** — nothing reaches a student until a teacher explicitly finalizes. Exports: marks register (CSV/XLSX), per-student annotated PDF, class analytics, expiring signed share links.

## 3. Architecture changes PoC → MVP

| Concern | PoC | MVP |
|---|---|---|
| Data | JSON files on disk | Postgres (orgs, users, exams, jobs, evaluations, audit log) |
| Files | Local `data/` dir | S3-compatible object storage (scans, annotated PDFs) |
| Processing | FastAPI BackgroundTasks | Worker queue (Redis + Celery/Arq), per-stage retries, concurrency caps |
| Tenancy | None | Orgs with RBAC: admin / teacher / reviewer; student share tokens (no student accounts in MVP) |
| Model calls | Interactive only | Interactive + Message Batches mode; per-job token & cost metering from API usage fields |
| UI | Single static page | React SPA (Vite) on the same FastAPI backend: exam dashboard, mapping screen, rubric builder, review queue |
| Ops | None | Structured logs, error tracking, per-tenant usage dashboard, nightly backups |

Model strategy: **claude-opus-5 for both transcription and evaluation** at MVP (one model, fewer variables while we calibrate). Cost lever to test in M5: Sonnet-tier transcription with Opus-tier evaluation.

---

## 4. Workflow by role

**Admin** — creates the org, invites members, sets retention policy and share-link expiry, sees usage/cost dashboard.

**Teacher / examiner**
1. Create exam → build rubric in the rubric builder (or import JSON; linting enforces criteria sums, warns on vague criteria)
2. Upload the class set → resolve the mapping screen (auto-matched roll numbers pre-filled)
3. See the **cost preview** (sheets × estimated tokens → ₹/$ figure) → choose instant or overnight batch → run
4. Monitor progress dashboard; when grading completes, work through anything the QA gate raised (or delegate to a reviewer)
5. **Finalize → publish** → export marks register, download annotated PDFs, send share links

**Reviewer / moderator** — works the review queue: flagged questions first (model's evaluation + evidence + original scan side by side), then the calibration sample. Every override records reviewer, reason, timestamp. The queue is the *only* place marks change after grading.

**Student** — opens an expiring signed link: their own annotated sheet, the summary card, per-criterion explanations. No login in MVP.

---

## 5. Calibration — the trust feature

This is what converts a skeptical exam cell into a customer, and it gates both pilot and launch.

- **Calibration run:** upload N sheets already graded by humans → AI grades them blind → agreement report: % of questions within ±1 mark, exact criterion-decision match rate, mean absolute error, per-question breakdown, drill-down into every disagreement.
- **Tuning loop:** disagreements almost always trace to ambiguous rubric wording; the report links each disagreement to the criterion involved so the rubric can be tightened and re-run.
- **Ongoing drift check:** every real exam's random review sample feeds the same agreement metrics, charted over time.

**Quality gates:**
- End of M3 (go/no-go): ≥ 90% of questions within ±1 mark on our internal test set
- Pilot exit (go/no-go): ≥ 95% within ±1 mark and ≥ 85% exact criterion decisions on each pilot institution's own calibration set

## 6. Cost model (planning estimate — measure for real in M5)

Per 3-page sheet, ~10 questions, claude-opus-5, rubric prompt-cached:

| Mode | Est. cost/sheet | 500-sheet cohort |
|---|---|---|
| Interactive | ~$0.15–0.30 | ~$75–150 |
| Overnight batch (−50%) | ~$0.08–0.15 | ~$40–75 |

Levers if needed: Sonnet-tier transcription, image downscaling at intake, per-exam budget caps (hard stop, not advisory). Every run shows the estimate up front and the actual after; both are metered per tenant.

---

## 7. Checkpoints to launch

Each milestone ends with a demo and explicit exit criteria. Two are go/no-go gates.

| # | Milestone | Weeks | Scope | Exit criteria |
|---|---|---|---|---|
| **M0** | Proof of concept | done ✅ | Single-sheet pipeline, annotation, overrides, mocked-client tests | Working end-to-end demo |
| **M1** | Foundations | 1–2 | Postgres + S3 + worker queue; orgs/auth/RBAC; PoC pipeline migrated | Two users in one org run the PoC flow on the new stack; data survives restart |
| **M2** | Batch grading core | 3–4 | Exam entity, bulk upload + zip, sheet↔student mapping UI, PDF rasterization, parallel workers, progress dashboard | A 100-sheet class set graded in one run with correct per-student attribution |
| **M3** | Review & calibration | 5–6 | Review queue, QA-gate rules, sampling, calibration harness + agreement report, rubric builder with linting | **Go/no-go:** ≥ 90% within ±1 mark on internal test set; reviewer clears a full queue in the UI |
| **M4** | Student-facing outputs | 7–8 | Per-student annotated PDF, marks register CSV/XLSX, class analytics (criterion-level cohort stats), expiring share links | Full artifact set exported for a class; a student link renders on mobile |
| **M5** | Enterprise hardening | 9–10 | Audit log, retention policy, cost preview + budget caps, Batch API mode, usage metering, backups, observability, load test | Security checklist passes; real cost/sheet measured; 500-sheet overnight run completes unattended |
| **M6** | Pilot | 11–12 | 2–3 institutions grade real exams; weekly feedback loop; fixes | **Go/no-go:** pilot calibration gates met; each pilot finalizes ≥ 1 real exam; reviewer time < 25% of their manual baseline |
| **M7** | Launch | 13–14 | Public student showcase sandbox (demo tenant, canned + rate-limited live mode), pricing, onboarding docs, marketing site | Sandbox live; first paying institution onboarded |

### Launch KPIs

- Grading agreement ≥ 95% within ±1 mark (per pilot calibration sets)
- Flag-for-review rate in the 10–20% band (lower = suspicious, higher = too costly)
- Reviewer time < 25% of fully-manual grading time
- 500-sheet cohort turned around overnight
- Measured cost/sheet within the M5 target
- Zero cross-tenant data incidents

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Poor scans tank OCR accuracy | Intake quality gate (blur/DPI warnings), scanning guidelines doc, per-answer legibility flags already force human review |
| Teachers don't trust AI marks | Calibration-first onboarding — every institution sees agreement numbers on *their own* graded sheets before any live exam; nothing publishes without human finalize |
| Cost overruns on big cohorts | Pre-run cost preview, hard per-exam budget caps, batch-mode default |
| Garbage rubrics → garbage grading | Rubric linting (sums, vague-wording warnings), template library, calibration loop points at weak criteria |
| Sheet↔student mismatch | Dedicated mapping stage + fix-up UI; publish blocked while any sheet is unmatched |
| Student PII exposure | Tenant isolation tests in CI, expiring signed links, retention policy with auto-purge, audit log on every access to student artifacts |
| Model/API changes | Model pinned per exam run; calibration re-run required before switching models |

---

## 9. Immediate next actions (M1 kickoff)

1. Choose managed Postgres + S3 provider and set up staging environment
2. Introduce the worker queue and port the PoC pipeline stages onto it
3. Schema design: org / user / exam / rubric / sheet / job / evaluation / override / audit entities
4. Auth: email+password with org invites (defer SSO to post-MVP)
5. Start recruiting 2–3 pilot institutions now — pilots gate launch, and school calendars book up
