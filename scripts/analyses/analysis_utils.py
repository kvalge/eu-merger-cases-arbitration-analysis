"""Shared helpers for attachments.csv analysis scripts."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ATTACHMENTS_CSV_PATH = PROJECT_ROOT / "data/processed/attachments.csv"
ANALYSIS_OUTPUT_DIR = PROJECT_ROOT / "data/analysis"


def load_attachments_csv(path: Path = ATTACHMENTS_CSV_PATH) -> list[dict[str, str]]:
    """Load all rows from attachments.csv."""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def write_report(output_name: str, lines: list[str]) -> Path:
    """Write analysis report lines to a UTF-8 text file."""
    ANALYSIS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = ANALYSIS_OUTPUT_DIR / output_name
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    content = "\n".join([f"Generated at: {generated_at}", ""] + lines) + "\n"
    output_path.write_text(content, encoding="utf-8")
    return output_path
