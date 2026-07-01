# EU Merger Cases — Arbitration Analysis

Python pipeline that downloads European Commission merger-case data, scans decision PDFs for arbitration-related keywords, and writes results to CSV.  


**Specification:** [specs/SPEC.md](specs/SPEC.md)

**Requirements:** Python 3.10+, dependencies in [requirements.txt](requirements.txt)

## Data 
Merger case publications: https://data.europa.eu/data/datasets/cc7e224e-6569-40f0-8037-d3389aa0fae7?locale=en  

## Project layout

```
eu-merger-cases-arbitration-analysis/
  config/
    keywords.txt            # arbitration keyword rules by language
  data/
    analysis/               # reports from scripts/analyses/*.py
      column_value_types.txt
      decision_number_values.txt
      empty_decision_number_patterns.txt
      metadata_reference_uniqueness.txt
      pdf_processed_at_formats.txt
    processed/
      attachments.csv
      attachments_summary.csv
      case_sectors.csv
      summary.json
    raw/
      case-data-M.json      # downloaded EC merger case JSON
  scripts/
    analyses/
      analysis_utils.py
      analyze_column_value_types.py
      analyze_decision_number.py
      analyze_metadata_reference.py
      analyze_pdf_processed_at.py
    pipeline/
      download_json.py      # fetch case-data-M.json
      pdf_processing.py       # PyMuPDF extract + page-by-page keyword scan
      pipeline_utils.py       # shared flattening, CSV paths, keywords
      process_attachments.py
      run_pipeline.py         # run all pipeline steps in order
      summarize_results.py
  specs/
    SPEC.md                   # full pipeline specification
  README.md
  requirements.txt
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
2. `scripts/pipeline/process_attachments.py` — downloads JSON if missing, flattens metadata, writes `case_sectors.csv`, download PDFs, keyword scan → `attachments.csv` and `attachments_summary.csv`
3. `scripts/pipeline/summarize_results.py` — print stats, write `data/processed/summary.json`

**All steps in order** (`run_pipeline.py` forwards `--test-limit`, `--retry-downloads`, `--skip-download`, `--workers`, and `--save-every` to step 2):

```bash
python scripts/pipeline/run_pipeline.py
python scripts/pipeline/run_pipeline.py --test-limit 10           # smoke test
python scripts/pipeline/run_pipeline.py --retry-downloads         # retry failed downloads
python scripts/pipeline/run_pipeline.py --skip-download           # skip JSON fetch if file exists
```

Full PDF pass uses optimized defaults (6 parallel workers, save every 100 PDFs) — typically **~2-3 hours** for ~11k PDFs on a 16 GB machine.

### PDF processing performance

| Component | Implementation |
|-----------|----------------|
| Text extraction | **PyMuPDF** (fast), **pdfplumber** fallback on errors |
| Keyword scan | Page-by-page with early exit on hit |
| Parallelism | **Process pool** when `--workers` > 1 (true multi-core CPU use) |

| Flag / env | Default | Purpose |
|------------|---------|---------|
| `--workers N` | `6` | Parallel PDF workers (`PDF_WORKERS`); process pool when > 1 |
| `--save-every N` | `100` | Write CSV exports after every N PDFs (`PDF_SAVE_EVERY`) |
| `REQUEST_DELAY_SECONDS` | `0` | Pause after each PDF **per worker** (politeness) |

**Conservative / interrupt-safe** (slower, saves after every PDF):

```bash
python scripts/pipeline/process_attachments.py --workers 1 --save-every 1
```

**Low-memory machine (8 GB):**

```bash
python scripts/pipeline/process_attachments.py --workers 4 --save-every 100
```

Notes:

- Defaults target a **16 GB RAM** desktop.
- Larger `--save-every` reduces CSV rewrite time; up to `N-1` PDFs may be unsaved if killed (Ctrl+C still attempts a final save).
- With `--workers` > 1, progress logs every **100** PDFs instead of per-PDF lines.

## Output files

| File | Description |
|------|-------------|
| `data/processed/attachments.csv` | One row per decision PDF; full metadata + keyword hit/no-hit results |
| `data/processed/attachments_summary.csv` | Same rows, 18 fixed columns for analysis (case/decision fields + `has_keyword_hit`) |
| `data/processed/case_sectors.csv` | One row per case sector; code levels 1–3 + case-level `has_keyword_hit` (`1`/`0`) |
| `data/processed/summary.json` | Run statistics |

**`attachments_summary.csv` columns:** `att_metadataReference`, `has_keyword_hit`, `decision_type_label`, `case_caseCompanies`, `case_caseInitiationDate`, `case_caseLastDecisionDate`, `case_caseInstrument`, `case_caseNumber`, `case_caseRegulation`, `case_caseSimplified`, `case_caseTitle`, `dec_decisionAdoptionDate`, `dec_decisionNumber`, `dec_decisionOfficialJournalPublicationsPublishedDates`, `dec_decisionTypes_code`, `dec_decisionTypes_label`, `dec_language`, `dec_metadataReference`.

**`case_sectors.csv` columns:** `case_caseNumber`, `case_caseSectors_code`, `case_caseSectors_label`, `case_caseSectors_code_level1`, `case_caseSectors_code_level2`, `case_caseSectors_code_level3`, `has_keyword_hit`.

Join attachments to sectors on `case_caseNumber`. Sector columns are not duplicated on attachment rows.

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
