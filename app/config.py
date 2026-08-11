import os
from pathlib import Path

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))

# Per-request output ceiling. 16K keeps non-streaming requests well under SDK
# HTTP timeouts while leaving room for long transcripts / detailed rationales.
MAX_OUTPUT_TOKENS = 16000

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB per file

SUPPORTED_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
