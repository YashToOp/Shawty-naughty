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
OCR_PROVIDER = os.environ.get("OCR_PROVIDER", "claude")

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

SUPPORTED_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
