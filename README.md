# EU Merger Cases — Arbitration Analysis

Python pipeline that downloads European Commission merger-case data, scans decision PDFs for arbitration-related keywords, and writes results to CSV.

**Specification:** [specs/SPEC.md](specs/SPEC.md)

**Requirements:** Python 3.10+, dependencies in [requirements.txt](requirements.txt)

## Setup

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows PowerShell
# source .venv/bin/activate  # Linux / macOS
pip install -r requirements.txt
```

Add keyword rules to `config/keywords.txt` (see spec).

## Run order

All Python scripts live in [`src/`](src/).

1. `src/download_json.py` — fetch and validate case JSON
2. `src/process_attachments.py` — flatten metadata, download PDFs, keyword scan → `data/processed/attachments.csv`
3. `src/summarize_results.py` — print stats, write `data/processed/summary.json`

**All steps in order:**

```bash
python src/run_pipeline.py
python src/run_pipeline.py --test-limit 10      # smoke test
python src/run_pipeline.py --retry-downloads    # retry failed PDF downloads
```

Or run each script manually in the order above (from the project root).
