"""Summarize attachments.csv metrics and write summary.json."""

from __future__ import annotations

import csv
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from pipeline_utils import ATTACHMENTS_CSV_PATH, SUMMARY_JSON_PATH, setup_logging, utc_now_iso


def is_relevant_decision(label: str) -> bool:
    """True when decision type label matches Art. 6(1)(b) or Art. 8(2)."""
    return "6(1)(b)" in label or "8(2)" in label


def summarize_results() -> int:
    """Build summary metrics from attachments.csv."""
    if not ATTACHMENTS_CSV_PATH.exists():
        logging.error("Missing CSV file: %s", ATTACHMENTS_CSV_PATH)
        return 1

    rows: list[dict[str, str]] = []
    with ATTACHMENTS_CSV_PATH.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    keyword_counter: Counter[str] = Counter()
    relevant_count = 0
    hits_in_relevant = 0

    for row in rows:
        label = row.get("decision_type_label", "")
        relevant = is_relevant_decision(label)
        if relevant:
            relevant_count += 1
            if row.get("has_keyword_hit") == "true":
                hits_in_relevant += 1

        if row.get("has_keyword_hit") == "true":
            matched = row.get("matchedKeywords", "").strip()
            if matched:
                keyword_counter[matched] += 1

    summary = {
        "total_attachments": len(rows),
        "active_attachments": sum(1 for row in rows if row.get("is_active") == "true"),
        "pdf_processed": sum(1 for row in rows if row.get("pdf_processed_at")),
        "pdf_errors": sum(1 for row in rows if row.get("pdf_processing_error")),
        "keyword_hits": sum(1 for row in rows if row.get("has_keyword_hit") == "true"),
        "relevant_art6_art8": relevant_count,
        "hits_in_relevant": hits_in_relevant,
        "top_matched_keywords": [
            {"keyword": keyword, "count": count}
            for keyword, count in keyword_counter.most_common(10)
        ],
        "generated_at": utc_now_iso(),
    }

    SUMMARY_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_JSON_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for key, value in summary.items():
        if key == "top_matched_keywords":
            continue
        print(f"{key}: {value}")

    print(f"Summary written to {SUMMARY_JSON_PATH}")
    return 0


def main() -> int:
    setup_logging()
    return summarize_results()


if __name__ == "__main__":
    sys.exit(main())
