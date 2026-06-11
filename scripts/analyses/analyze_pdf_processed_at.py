"""Analyze value formats in the pdf_processed_at column."""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime

from analysis_utils import ATTACHMENTS_CSV_PATH, load_attachments_csv, write_report

COLUMN = "pdf_processed_at"
OUTPUT_FILE = "pdf_processed_at_formats.txt"

ISO_UTC_OFFSET_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
ISO_UTC_Z_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ISO_OFFSET_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$"
)
DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TOP_N = 10


def classify_value(value: str) -> str:
    """Classify a timestamp string into a format kind."""
    if not value:
        return "empty"
    if ISO_UTC_OFFSET_PATTERN.match(value):
        return "iso_datetime_utc_offset"
    if ISO_UTC_Z_PATTERN.match(value):
        return "iso_datetime_utc_z"
    if ISO_OFFSET_PATTERN.match(value):
        return "iso_datetime_other_offset"
    if DATE_ONLY_PATTERN.match(value):
        return "date_only"
    return "other"


def parse_timestamp(value: str) -> datetime | None:
    """Parse supported timestamp strings for min/max stats."""
    if ISO_UTC_OFFSET_PATTERN.match(value) or ISO_OFFSET_PATTERN.match(value):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    if ISO_UTC_Z_PATTERN.match(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def analyze_pdf_processed_at(rows: list[dict[str, str]]) -> dict:
    """Build format stats for pdf_processed_at."""
    values = [row.get(COLUMN, "") for row in rows]
    kinds = Counter(classify_value(value) for value in values)
    lengths = Counter(len(value) for value in values)
    lengths_non_empty = Counter(len(value) for value in values if value)

    kind_lengths: dict[str, Counter[int]] = {}
    samples_by_kind: dict[str, list[str]] = {}
    date_counts = Counter(value[:10] for value in values if len(value) >= 10)
    parsed_times = [parsed for value in values if (parsed := parse_timestamp(value))]

    for value in values:
        kind = classify_value(value)
        kind_lengths.setdefault(kind, Counter())[len(value)] += 1

    for value in sorted(set(values)):
        kind = classify_value(value)
        samples = samples_by_kind.setdefault(kind, [])
        if len(samples) < 5:
            samples.append(value)

    other_samples = [value for value in sorted(set(values)) if classify_value(value) == "other"][:5]

    return {
        "column": COLUMN,
        "total_rows": len(rows),
        "non_empty_rows": sum(1 for value in values if value),
        "unique_values": len(set(values)),
        "value_kinds": dict(kinds.most_common()),
        "lengths_all_rows": dict(lengths.most_common()),
        "lengths_non_empty": dict(lengths_non_empty.most_common()),
        "lengths_by_kind": {
            kind: dict(counter.most_common())
            for kind, counter in sorted(kind_lengths.items())
        },
        "samples_by_kind": samples_by_kind,
        "other_samples": other_samples,
        "earliest": min(parsed_times).isoformat() if parsed_times else "",
        "latest": max(parsed_times).isoformat() if parsed_times else "",
        "top_dates": date_counts.most_common(TOP_N),
    }


def format_report(stats: dict) -> list[str]:
    """Build human-readable report lines."""
    lines = [
        f"Column: {stats['column']}",
        f"Total rows: {stats['total_rows']}",
        f"Non-empty rows: {stats['non_empty_rows']}",
        f"Unique values: {stats['unique_values']}",
        "",
        "Expected format (from pipeline): ISO-8601 UTC text, e.g. 2026-06-11T10:47:41+00:00",
        "",
        "Value format kinds:",
    ]

    for kind, count in stats["value_kinds"].items():
        pct = 100 * count / stats["total_rows"]
        lines.append(f"  {kind}: {count} ({pct:.1f}%)")
    lines.append("")

    if stats["earliest"] and stats["latest"]:
        lines.extend(
            [
                f"Earliest timestamp: {stats['earliest']}",
                f"Latest timestamp: {stats['latest']}",
                "",
            ]
        )

    lines.append("Rows per processing date (YYYY-MM-DD):")
    for date_value, count in stats["top_dates"]:
        pct = 100 * count / stats["total_rows"]
        lines.append(f"  {date_value}: {count} ({pct:.1f}%)")
    lines.append("")

    lines.append("String length (all rows, including empty):")
    for length, count in stats["lengths_all_rows"].items():
        label = "empty" if length == 0 else str(length)
        lines.append(f"  length {label}: {count}")
    lines.append("")

    lines.append("String length (non-empty only):")
    for length, count in stats["lengths_non_empty"].items():
        lines.append(f"  length {length}: {count}")
    lines.append("")

    lines.append("Length by format kind:")
    for kind, lengths in stats["lengths_by_kind"].items():
        parts = ", ".join(f"len {n}={c}" for n, c in lengths.items())
        lines.append(f"  {kind}: {parts}")
    lines.append("")

    lines.append("Sample values by format kind:")
    for kind, samples in stats["samples_by_kind"].items():
        lines.append(f"  {kind}:")
        for sample in samples:
            lines.append(f"    - {sample}")

    if stats["other_samples"]:
        lines.extend(["", "Other format samples:"])
        for sample in stats["other_samples"]:
            lines.append(f"  - {sample}")

    return lines


def main() -> int:
    if not ATTACHMENTS_CSV_PATH.exists():
        print(f"Missing CSV file: {ATTACHMENTS_CSV_PATH}", file=sys.stderr)
        return 1

    rows = load_attachments_csv()
    if not rows or COLUMN not in rows[0]:
        print(f"Column not found in CSV: {COLUMN}", file=sys.stderr)
        return 1

    output_path = write_report(
        OUTPUT_FILE,
        format_report(analyze_pdf_processed_at(rows)),
    )
    print(f"Report written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
