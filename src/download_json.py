"""Download and validate EC merger case JSON."""

from __future__ import annotations

import json
import logging
import sys

from pipeline_utils import (
    JSON_URL,
    MIN_CASE_COUNT,
    RAW_JSON_PATH,
    RAW_JSON_TEMP_PATH,
    request_with_retries,
    setup_logging,
)


def download_json() -> int:
    """Fetch JSON, validate it, and save to the raw data path."""
    RAW_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

    try:
        logging.info("Downloading JSON from %s", JSON_URL)
        response = request_with_retries("GET", JSON_URL)
        RAW_JSON_TEMP_PATH.write_bytes(response.content)

        data = json.loads(RAW_JSON_TEMP_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON root must be an object")

        case_count = len(data)
        if case_count < MIN_CASE_COUNT:
            raise ValueError(
                f"Expected at least {MIN_CASE_COUNT} cases, got {case_count}"
            )

        RAW_JSON_TEMP_PATH.replace(RAW_JSON_PATH)
        logging.info("Saved %s cases to %s", case_count, RAW_JSON_PATH)
        return 0

    except Exception as exc:
        logging.error("JSON download failed: %s", exc)
        if RAW_JSON_TEMP_PATH.exists():
            RAW_JSON_TEMP_PATH.unlink()
        return 1


def main() -> int:
    setup_logging()
    return download_json()


if __name__ == "__main__":
    sys.exit(main())
