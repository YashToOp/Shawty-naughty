#!/usr/bin/env python
"""Inventory the incoming/ drop zone before ingestion.

For every PDF (and image) dropped in incoming/, report page count, whether
it carries a text layer (digital board PDFs ingest much faster than scans),
and the metadata guessable from its filename — so ingestion can be planned
and batched instead of opening files blind.

    python scripts/inventory_incoming.py            # table + manifest.json
    python scripts/inventory_incoming.py path/to/dir
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Reuse the filename-metadata guesser from the official-papers fetcher.
_spec = importlib.util.spec_from_file_location(
    "fetch_official_papers", ROOT / "scripts" / "fetch_official_papers.py")
_fetcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fetcher)
guess_metadata = _fetcher.guess_metadata

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def pdf_pages(path: Path) -> int:
    try:
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True,
                             text=True, timeout=30).stdout
        for line in out.splitlines():
            if line.startswith("Pages:"):
                return int(line.split()[-1])
    except Exception:
        pass
    return 0


def has_text_layer(path: Path) -> bool:
    """A digital PDF yields real text; a scan yields almost nothing."""
    try:
        out = subprocess.run(
            ["pdftotext", "-l", "3", str(path), "-"],
            capture_output=True, text=True, timeout=60).stdout
        return len(out.strip()) > 200
    except Exception:
        return False


def inventory(directory: Path) -> list[dict]:
    entries = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            entries.append({
                "file": str(path.relative_to(directory)),
                "kind": "pdf",
                "pages": pdf_pages(path),
                "text_layer": has_text_layer(path),
                **guess_metadata(path.name),
            })
        elif suffix in IMAGE_SUFFIXES:
            entries.append({
                "file": str(path.relative_to(directory)),
                "kind": "image",
                "pages": 1,
                "text_layer": False,
                **guess_metadata(path.name),
            })
    return entries


def main() -> None:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "incoming"
    if not directory.exists():
        sys.exit(f"{directory} does not exist")
    entries = inventory(directory)
    if not entries:
        sys.exit(f"No papers found in {directory}")

    # Digital PDFs first - they ingest fastest and most accurately.
    entries.sort(key=lambda e: (not e["text_layer"], e["file"]))

    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(entries, indent=2))

    filled = lambda e: sum(1 for k in ("class_level", "subject", "year")
                           if e[k] != "FILL_ME")
    print(f"{'file':<52}{'pages':>6}  {'text':<5}"
          f"{'class':<7}{'subject':<22}{'year':<6}")
    print("-" * 100)
    for e in entries:
        print(f"{e['file'][:50]:<52}{e['pages']:>6}  "
              f"{'yes' if e['text_layer'] else 'SCAN':<5}"
              f"{e['class_level']:<7}{e['subject'][:20]:<22}{e['year']:<6}")
    total_pages = sum(e["pages"] for e in entries)
    scans = sum(1 for e in entries if not e["text_layer"])
    incomplete = sum(1 for e in entries if filled(e) < 3)
    print("-" * 100)
    print(f"{len(entries)} paper file(s), {total_pages} pages total; "
          f"{scans} scan(s) without text layer; "
          f"{incomplete} with metadata to fill from front pages.")
    print(f"Manifest written to {manifest}")


if __name__ == "__main__":
    main()
