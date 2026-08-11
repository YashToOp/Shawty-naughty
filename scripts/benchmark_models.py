#!/usr/bin/env python
"""Side-by-side model benchmark: same sheet, same rubric, every stack.

Runs transcription + evaluation for each requested provider stack and prints
per-question marks next to each other, so a free-stack candidate earns (or
loses) its place with numbers instead of vibes. The prompts, rubric, and
mark-clamping code are identical across stacks — only the backend changes.

    # Claude baseline vs the free stack on a mock Science sheet:
    python scripts/benchmark_models.py pages/*.png \
        --rubric sample_data/papers/cbse-12-english-core-2026.json \
        --stack anthropic --stack free --strictness 0

Stacks:
    anthropic  EVAL_PROVIDER=anthropic   OCR_PROVIDER=claude         (baseline)
    free       EVAL_PROVIDER=workers-ai  OCR_PROVIDER=google-vision
    hybrid     EVAL_PROVIDER=anthropic   OCR_PROVIDER=google-vision

Each stack needs its keys in the environment: ANTHROPIC_API_KEY, or
CF_ACCOUNT_ID + CF_API_TOKEN + GOOGLE_VISION_API_KEY (see .env.example).
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, evaluator, pipeline  # noqa: E402
from app.models import Rubric  # noqa: E402

STACKS = {
    "anthropic": {"EVAL_PROVIDER": "anthropic", "OCR_PROVIDER": "claude"},
    "free": {"EVAL_PROVIDER": "workers-ai", "OCR_PROVIDER": "google-vision"},
    "hybrid": {"EVAL_PROVIDER": "anthropic", "OCR_PROVIDER": "google-vision"},
}


def load_rubric(path: Path) -> Rubric:
    """Accepts a bare Rubric json or a registry Paper json with a rubric."""
    data = json.loads(path.read_text())
    if data.get("rubric"):
        return Rubric.model_validate(data["rubric"])
    return Rubric.model_validate(data)


def run_stack(name: str, rubric: Rubric, files: list[Path],
              strictness: int) -> dict:
    for key, value in STACKS[name].items():
        setattr(config, key, value)
    started = time.monotonic()
    client = pipeline.get_client()
    transcript = pipeline.transcriber().transcribe(client, rubric, files)
    results = evaluator.evaluate_submission(client, rubric, transcript,
                                            strictness=strictness)
    return {
        "seconds": time.monotonic() - started,
        "marks": {r.evaluation.question_id: r.evaluation.marks_awarded
                  for r in results},
        "flagged": {r.evaluation.question_id
                    for r in results if r.evaluation.needs_human_review},
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="answer-sheet page images, in order")
    ap.add_argument("--rubric", required=True,
                    help="rubric json, or a registry paper json containing one")
    ap.add_argument("--stack", action="append", choices=sorted(STACKS),
                    default=[], help="stack to run (repeatable)")
    ap.add_argument("--strictness", type=int, default=0, choices=[0, 1, 2])
    args = ap.parse_args()

    stacks = args.stack or ["anthropic", "free"]
    rubric = load_rubric(Path(args.rubric))
    files = [Path(f) for f in args.files]

    runs: dict[str, dict] = {}
    for name in stacks:
        print(f"running {name} ...", flush=True)
        try:
            runs[name] = run_stack(name, rubric, files, args.strictness)
        except Exception as exc:  # a broken stack shouldn't hide the others
            print(f"  {name} FAILED: {exc}")

    if not runs:
        sys.exit("No stack completed.")

    baseline = stacks[0] if stacks[0] in runs else next(iter(runs))
    width = max(len(n) for n in runs) + 2
    header = f"{'question':<10}{'max':>6}" + "".join(
        f"{n:>{width + 2}}" for n in runs)
    print("\n" + header)
    print("-" * len(header))
    for question in rubric.questions:
        row = f"{question.id:<10}{question.max_marks:>6g}"
        for name, run in runs.items():
            marks = run["marks"].get(question.id)
            cell = "-" if marks is None else f"{marks:g}"
            if question.id in run["flagged"]:
                cell += "*"
            row += f"{cell:>{width + 2}}"
        print(row)
    print("-" * len(header))
    totals = f"{'TOTAL':<10}{rubric.total_marks:>6g}"
    for name, run in runs.items():
        totals += f"{sum(run['marks'].values()):>{width + 2}g}"
    print(totals)

    print("\n* = flagged for human review")
    for name, run in runs.items():
        drift = sum(
            abs(run["marks"].get(q.id, 0.0)
                - runs[baseline]["marks"].get(q.id, 0.0))
            for q in rubric.questions)
        note = "" if name == baseline else \
            f" | total per-question drift vs {baseline}: {drift:g} marks"
        print(f"{name}: {run['seconds']:.1f}s{note}")


if __name__ == "__main__":
    main()
