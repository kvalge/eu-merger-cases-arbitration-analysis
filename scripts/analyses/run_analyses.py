"""Run all attachments.csv analysis scripts in order.

How to run (from project root)
------------------------------
    python scripts/analyses/run_analyses.py

Reads data/processed/attachments.csv and writes reports to data/analysis/*.txt.
Stops on the first script that exits with a non-zero status.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANALYSES_DIR = Path(__file__).resolve().parent

ANALYSIS_SCRIPTS = [
    "analyze_decision_number.py",
    "analyze_metadata_reference.py",
    "analyze_pdf_processed_at.py",
    "analyze_column_value_types.py",
]


def run_step(command: list[str]) -> int:
    """Run one analysis script from the project root."""
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    return result.returncode


def main() -> int:
    python = sys.executable

    for script_name in ANALYSIS_SCRIPTS:
        exit_code = run_step([python, str(ANALYSES_DIR / script_name)])
        if exit_code != 0:
            return exit_code

    return 0


if __name__ == "__main__":
    sys.exit(main())
