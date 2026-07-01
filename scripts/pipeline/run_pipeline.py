"""Run the full merger-case arbitration analysis pipeline."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from pipeline_utils import RAW_JSON_PATH

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    """Parse pipeline flags forwarded to process_attachments.py."""
    parser = argparse.ArgumentParser(description="Run the full analysis pipeline.")
    parser.add_argument("--test-limit", type=int, default=None)
    parser.add_argument("--retry-downloads", action="store_true")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--save-every", type=int, default=None)
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip download_json.py when data/raw/case-data-M.json already exists",
    )
    return parser.parse_args()


def run_step(command: list[str]) -> int:
    """Run one pipeline script from the project root."""
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    return result.returncode


def main() -> int:
    args = parse_args()
    python = sys.executable

    steps: list[list[str]] = []
    if not args.skip_download:
        steps.append([python, str(PIPELINE_DIR / "download_json.py")])
    elif not RAW_JSON_PATH.exists():
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        logging.error(
            "--skip-download set but %s is missing — run download_json.py first",
            RAW_JSON_PATH,
        )
        return 1

    process_cmd = [python, str(PIPELINE_DIR / "process_attachments.py")]
    if args.test_limit is not None:
        process_cmd.extend(["--test-limit", str(args.test_limit)])
    if args.retry_downloads:
        process_cmd.append("--retry-downloads")
    if args.workers is not None:
        process_cmd.extend(["--workers", str(args.workers)])
    if args.save_every is not None:
        process_cmd.extend(["--save-every", str(args.save_every)])
    steps.append(process_cmd)
    steps.append([python, str(PIPELINE_DIR / "summarize_results.py")])

    for command in steps:
        exit_code = run_step(command)
        if exit_code != 0:
            return exit_code

    return 0


if __name__ == "__main__":
    sys.exit(main())
