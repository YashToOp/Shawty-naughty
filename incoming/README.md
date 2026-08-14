# Incoming papers — drop zone

Put contributed question papers here (GitHub web UI: *Add file → Upload
files* on this branch). They get ingested into `sample_data/banks/` as
condensed question entries + marking schemes, then **deleted from the repo**
— original paper files never ship with the app.

Format that ingests fastest:

- **One PDF per paper**, pages in order, front page included (that's where
  the Q.P. code and set number live).
- Name files with whatever metadata you know:
  `cbse-10-maths-standard-2024-set-430-1-1.pdf` beats `scan0007.pdf`.
  Unknown parts can be left out — the front page usually fills them in.
- If a contributor supplied extra context (year, set, "this is the
  compartment paper"), drop it in a `notes.txt` alongside.

Run `python scripts/inventory_incoming.py` to see what's here, what has a
text layer (fast to ingest) vs scans (slower), and what metadata is still
missing.
