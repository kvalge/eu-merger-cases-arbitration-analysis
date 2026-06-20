# EU Merger Cases — Arbitration Analysis

Python pipeline that downloads European Commission merger-case data, scans decision PDFs for arbitration-related keywords, and writes results to CSV.

**Specification:** [specs/SPEC.md](specs/SPEC.md)

**Requirements:** Python 3.10+, dependencies in [requirements.txt](requirements.txt)

## Project layout

```
scripts/
  pipeline/     # download, process PDFs, summarize
  analyses/     # explore attachments.csv
config/         # keyword rules
data/
  raw/          # downloaded JSON
  processed/    # attachments.csv, case_sectors.csv, summary.json
  analysis/     # analysis report .txt files
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1   # Windows PowerShell
# source .venv/bin/activate  # Linux / macOS
pip install -r requirements.txt
```

Add keyword rules to `config/keywords.txt` (see spec).

## Pipeline (run from project root)

1. `scripts/pipeline/download_json.py` — fetch and validate case JSON
2. `scripts/pipeline/process_attachments.py` — flatten metadata, write `case_sectors.csv`, download PDFs, keyword scan → `attachments.csv`
3. `scripts/pipeline/summarize_results.py` — print stats, write `data/processed/summary.json`

**All steps in order:**

```bash
python scripts/pipeline/run_pipeline.py
python scripts/pipeline/run_pipeline.py --test-limit 10      # smoke test
python scripts/pipeline/run_pipeline.py --retry-downloads    # retry failed PDF downloads
```

## Output files

| File | Description |
|------|-------------|
| `data/processed/attachments.csv` | One row per decision PDF; metadata + keyword hit/no-hit results |
| `data/processed/case_sectors.csv` | One row per case sector (`case_caseNumber`, `case_caseSectors_code`, `case_caseSectors_label`) |
| `data/processed/summary.json` | Run statistics |

Join attachments to sectors on `case_caseNumber`. Sector columns are not duplicated on attachment rows (no pipe-joined `case_caseSectors_*` or `sector_*` columns).

CSV values are sanitized on write (embedded line breaks → spaces) so Excel opens one row per attachment.

## Analysis scripts

Scripts in [`scripts/analyses/`](scripts/analyses/) read `data/processed/attachments.csv` and write reports to `data/analysis/`:

```bash
python scripts/analyses/analyze_decision_number.py
python scripts/analyses/analyze_metadata_reference.py
python scripts/analyses/analyze_pdf_processed_at.py
python scripts/analyses/analyze_column_value_types.py
```

Outputs:

- `data/analysis/decision_number_values.txt`
- `data/analysis/metadata_reference_uniqueness.txt`
- `data/analysis/pdf_processed_at_formats.txt`
- `data/analysis/column_value_types.txt` — value types and literal values for every column
