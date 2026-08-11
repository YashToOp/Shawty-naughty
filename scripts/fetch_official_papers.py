#!/usr/bin/env python
"""Download question papers from CBSE's OFFICIAL distribution pages.

CBSE publishes previous-year question papers and sample papers (with marking
schemes) on its own websites for students and schools to download. This tool
automates that intended download path - nothing else:

  * It only crawls the official domains (cbse.gov.in, cbseacademic.nic.in by
    default). Aggregator/coaching mirrors are deliberately not supported.
  * It is polite: rate-limited, identifies itself, skips already-downloaded
    files, and hard-caps the number of requests per run.
  * It downloads to YOUR machine. Ingesting what you download into the
    platform is a separate, deliberate step (see the generated manifest).

Usage (run on your machine, not in CI):

    # Crawl an official index page for PDFs, class 10 and 12 papers
    python scripts/fetch_official_papers.py \
        --index-url "https://www.cbse.gov.in/cbsenew/question-paper.html" \
        --match "class-x" --match "class-xii" --match 2026

    # Then review downloads/manifest.json and downloads/ingest_commands.sh,
    # fill in the metadata placeholders, and run the ingest commands you want.

The exact URL of CBSE's current question-paper index changes from time to
time - open cbse.gov.in, find the Question Papers / Academic section, and
pass that page via --index-url.
"""

import argparse
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

OFFICIAL_DOMAINS = {"cbse.gov.in", "cbseacademic.nic.in", "cbse.nic.in"}
USER_AGENT = ("AnswerSheetEvaluator-fetcher/0.1 "
              "(downloads official CBSE publications for local use)")

CLASS_HINTS = {
    "10": ["class-x", "classx", "class10", "class-10", "x_", "sqp_x"],
    "12": ["class-xii", "classxii", "class12", "class-12", "xii"],
}
SUBJECT_HINTS = {
    "Science": ["science"],
    "Mathematics": ["math"],
    "English": ["english", "eng"],
    "Social Science": ["social", "sst"],
    "Hindi": ["hindi"],
    "Physics": ["physics", "phy"],
    "Chemistry": ["chem"],
    "Biology": ["bio"],
    "Economics": ["eco"],
    "Accountancy": ["account"],
    "Business Studies": ["business", "bst"],
    "Computer Science": ["computer", "cs_"],
    "History": ["history"],
    "Geography": ["geog"],
    "Political Science": ["political", "polsci"],
}


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value)


def extract_links(html: str, base_url: str) -> tuple[list[str], list[str]]:
    """Return (pdf_links, page_links) resolved absolute, official-domain only."""
    parser = LinkExtractor()
    parser.feed(html)
    pdfs, pages = [], []
    for href in parser.links:
        absolute = urljoin(base_url, href.strip())
        host = urlparse(absolute).netloc.lower().removeprefix("www.")
        if host not in OFFICIAL_DOMAINS:
            continue
        if absolute.lower().split("?")[0].endswith(".pdf"):
            pdfs.append(absolute)
        elif absolute.lower().startswith("http"):
            pages.append(absolute)
    return pdfs, pages


def matches(url: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    low = url.lower()
    return any(p.lower() in low for p in patterns)


def guess_metadata(url: str) -> dict:
    """Best-effort class/subject/year inference from the URL/filename.
    Anything not inferable stays a placeholder for the human to fill in."""
    low = url.lower()
    guessed = {"board": "CBSE", "class_level": "FILL_ME",
               "subject": "FILL_ME", "year": "FILL_ME"}
    for cls, hints in CLASS_HINTS.items():
        if any(h in low for h in hints):
            guessed["class_level"] = cls
            break
    for subject, hints in SUBJECT_HINTS.items():
        if any(h in low for h in hints):
            guessed["subject"] = subject
            break
    year = re.search(r"20\d{2}", low)
    if year:
        guessed["year"] = year.group(0)
    return guessed


def ingest_command(pdf_path: Path, meta: dict) -> str:
    incomplete = any(v == "FILL_ME" for v in meta.values())
    cmd = (f"python scripts/ingest_paper.py '{pdf_path}' "
           f"--board {meta['board']} --class-level {meta['class_level']} "
           f"--subject \"{meta['subject']}\" --year {meta['year']}")
    return ("# FILL METADATA, then uncomment:\n# " + cmd) if incomplete else cmd


def fetch(url: str, client, binary: bool = False):
    response = client.get(url, headers={"User-Agent": USER_AGENT},
                          follow_redirects=True, timeout=30)
    response.raise_for_status()
    return response.content if binary else response.text


def crawl(index_urls: list[str], match_patterns: list[str], out_dir: Path,
          delay: float, max_requests: int, depth: int) -> list[dict]:
    import httpx

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    seen_pages, seen_pdfs = set(), set()
    queue: list[tuple[str, int]] = [(u, 0) for u in index_urls]
    requests_made = 0

    with httpx.Client() as client:
        while queue and requests_made < max_requests:
            url, level = queue.pop(0)
            if url in seen_pages:
                continue
            seen_pages.add(url)
            try:
                html = fetch(url, client)
            except Exception as exc:
                print(f"  skip page {url}: {exc}")
                continue
            requests_made += 1
            time.sleep(delay)

            pdfs, pages = extract_links(html, url)
            for pdf in pdfs:
                if pdf in seen_pdfs or not matches(pdf, match_patterns):
                    continue
                seen_pdfs.add(pdf)
                name = Path(urlparse(pdf).path).name or "paper.pdf"
                target = out_dir / name
                if target.exists():
                    print(f"  have    {name}")
                else:
                    if requests_made >= max_requests:
                        print("  request cap reached - rerun to continue")
                        break
                    try:
                        target.write_bytes(fetch(pdf, client, binary=True))
                        requests_made += 1
                        print(f"  saved   {name}")
                        time.sleep(delay)
                    except Exception as exc:
                        print(f"  failed  {name}: {exc}")
                        continue
                meta = guess_metadata(pdf)
                manifest.append({"url": pdf, "file": str(target), **meta})

            if level < depth:
                for page in pages:
                    if page not in seen_pages and matches(page, match_patterns):
                        queue.append((page, level + 1))

    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index-url", action="append", required=True,
                    help="official CBSE page to start from (repeatable)")
    ap.add_argument("--match", action="append", default=[],
                    help="only follow/download URLs containing this text (repeatable)")
    ap.add_argument("--out", default="downloads", help="output directory")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between requests (politeness; min 1.0)")
    ap.add_argument("--max-requests", type=int, default=200,
                    help="hard cap on HTTP requests per run")
    ap.add_argument("--depth", type=int, default=2,
                    help="how many link levels to follow from the index page")
    args = ap.parse_args()

    for url in args.index_url:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if host not in OFFICIAL_DOMAINS:
            sys.exit(f"{url}: not an official CBSE domain "
                     f"({', '.join(sorted(OFFICIAL_DOMAINS))}). "
                     "This tool only automates CBSE's own download pages.")

    out_dir = Path(args.out)
    manifest = crawl(args.index_url, args.match, out_dir,
                     max(args.delay, 1.0), args.max_requests, args.depth)

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    commands = "\n\n".join(
        ingest_command(Path(entry["file"]), entry) for entry in manifest)
    (out_dir / "ingest_commands.sh").write_text(
        "#!/bin/bash\n# Review each command, fill FILL_ME metadata, then run.\n"
        "# One command per paper set. Requires ANTHROPIC_API_KEY.\n\n"
        + commands + "\n")

    print(f"\n{len(manifest)} PDF(s) in {out_dir}/ - see manifest.json")
    print(f"Ingest manifest written to {out_dir}/ingest_commands.sh "
          "(review + fill metadata before running)")


if __name__ == "__main__":
    main()
