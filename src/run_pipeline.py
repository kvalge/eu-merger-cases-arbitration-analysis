"""Run the full merger-case arbitration analysis pipeline."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    """Parse pipeline flags forwarded to process_attachments.py."""
    parser = argparse.ArgumentParser(description="Run the full analysis pipeline.")
    parser.add_argument("--test-limit", type=int, default=None)
    parser.add_argument("--retry-downloads", action="store_true")
    return parser.parse_args()


def run_step(command: list[str]) -> int:
    """Run one pipeline script from the project root."""
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    return result.returncode


def main() -> int:
    args = parse_args()
    python = sys.executable

    steps = [
        [python, str(SCRIPT_DIR / "download_json.py")],
    ]

    process_cmd = [python, str(SCRIPT_DIR / "process_attachments.py")]
    if args.test_limit is not None:
        process_cmd.extend(["--test-limit", str(args.test_limit)])
    if args.retry_downloads:
        process_cmd.append("--retry-downloads")
    steps.append(process_cmd)
    steps.append([python, str(SCRIPT_DIR / "summarize_results.py")])

    for command in steps:
        exit_code = run_step(command)
        if exit_code != 0:
            return exit_code

    return 0


if __name__ == "__main__":
    sys.exit(main())
