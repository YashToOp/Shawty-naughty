import os
from pathlib import Path

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))

# Per-request output ceiling. 16K keeps non-streaming requests well under SDK
# HTTP timeouts while leaving room for long transcripts / detailed rationales.
MAX_OUTPUT_TOKENS = 16000

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per file

# ---------------------------------------------------------------------------
# Model backends. Defaults are the Claude stack (the calibration baseline);
# the free stack swaps providers per stage, never the prompts or the rubric
# arithmetic, so results stay comparable across backends.
# ---------------------------------------------------------------------------

# Which model judges answers (and writes marking schemes):
#   "anthropic"  - Claude via the Anthropic SDK (default)
#   "workers-ai" - open models on Cloudflare Workers AI (free tier)
EVAL_PROVIDER = os.environ.get("EVAL_PROVIDER", "anthropic")

# How scanned pages become a transcript:
#   "claude"        - Claude vision reads the pages directly (default)
#   "google-vision" - Google Cloud Vision OCR + a text-only segmentation pass
#   "cascade"       - PaddleOCR (free, local) first; Google Vision only for
#                     pages that fail the confidence gate (see ocr_cascade.py)
OCR_PROVIDER = os.environ.get("OCR_PROVIDER", "claude")

# Cascade gate: a page is accepted from PaddleOCR alone when it recognised at
# least CASCADE_MIN_CHARS characters at a mean confidence of at least
# CASCADE_ACCEPT_CONFIDENCE; otherwise Google Vision re-reads that page and
# the two reads are merged line by line (agreement above
# CASCADE_AGREE_SIMILARITY corroborates; disagreement flags the line).
CASCADE_ACCEPT_CONFIDENCE = float(os.environ.get("CASCADE_ACCEPT_CONFIDENCE", "0.88"))
CASCADE_MIN_CHARS = int(os.environ.get("CASCADE_MIN_CHARS", "30"))
CASCADE_AGREE_SIMILARITY = float(os.environ.get("CASCADE_AGREE_SIMILARITY", "0.85"))

# Cloudflare Workers AI (used when EVAL_PROVIDER="workers-ai")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
WORKERS_AI_EVAL_MODEL = os.environ.get(
    "WORKERS_AI_EVAL_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
WORKERS_AI_SEGMENT_MODEL = os.environ.get(
    "WORKERS_AI_SEGMENT_MODEL", "@cf/meta/llama-3.1-8b-instruct")

# Google Cloud Vision (used when OCR_PROVIDER="google-vision")
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")

# OCR lines whose mean word confidence falls below this are flagged in the
# segmentation prompt and cap the answer's legibility at "partial" in code.
OCR_CONFIDENCE_FLOOR = 0.6

# ---------------------------------------------------------------------------
# Public-exposure guardrails. Every upload triggers paid model calls, so the
# public endpoints are throttled per client and capped globally per day, and
# grading jobs run through a bounded gate instead of unbounded background
# tasks. All knobs are env-tunable; 0 disables the corresponding check.
# ---------------------------------------------------------------------------
SUBMISSIONS_PER_HOUR_PER_IP = int(os.environ.get("SUBMISSIONS_PER_HOUR_PER_IP", "6"))
CONTRIBUTIONS_PER_HOUR_PER_IP = int(os.environ.get("CONTRIBUTIONS_PER_HOUR_PER_IP", "10"))
GLOBAL_JOBS_PER_DAY = int(os.environ.get("GLOBAL_JOBS_PER_DAY", "300"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))

# Uploaded sheets and results are purged after this many days (0 = keep).
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))

# PDF uploads are rasterized to page images at this resolution; one upload
# may expand to at most MAX_PDF_PAGES pages (cost cap per submission).
RASTER_DPI = int(os.environ.get("RASTER_DPI", "200"))
MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "40"))

SUPPORTED_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
