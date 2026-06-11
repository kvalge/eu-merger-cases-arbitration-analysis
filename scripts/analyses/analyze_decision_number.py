"""Analyze value kinds and lengths in the dec_decisionNumber column."""

from __future__ import annotations

import re
import sys
from collections import Counter

from analysis_utils import ATTACHMENTS_CSV_PATH, load_attachments_csv, write_report

COLUMN = "dec_decisionNumber"
OUTPUT_FILE = "decision_number_values.txt"
GUID_PATTERN = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$"
)
NUMERIC_PATTERN = re.compile(r"^\d+$")


def classify_value(value: str) -> str:
    """Classify a decision number into a value kind."""
    if not value:
        return "empty"
    if GUID_PATTERN.match(value):
        return "guid"
    if NUMERIC_PATTERN.match(value):
        return "numeric"
    return "other"


def analyze_decision_number(rows: list[dict[str, str]]) -> dict:
    """Build analysis stats for dec_decisionNumber."""
    values = [row.get(COLUMN, "") for row in rows]
    kinds = Counter(classify_value(value) for value in values)
    lengths = Counter(len(value) for value in values)
    lengths_non_empty = Counter(len(value) for value in values if value)

    kind_lengths: dict[str, Counter[int]] = {}
    samples_by_kind: dict[str, list[str]] = {}
    unique_values = set(values)

    for value in values:
        kind = classify_value(value)
        kind_lengths.setdefault(kind, Counter())[len(value)] += 1

    for value in sorted(unique_values):
        kind = classify_value(value)
        samples = samples_by_kind.setdefault(kind, [])
        if len(samples) < 5:
            samples.append(value)

    return {
        "column": COLUMN,
        "total_rows": len(rows),
        "non_empty_rows": sum(1 for value in values if value),
        "unique_values": len(unique_values),
        "value_kinds": dict(kinds.most_common()),
        "lengths_all_rows": dict(lengths.most_common()),
        "lengths_non_empty": dict(lengths_non_empty.most_common()),
        "lengths_by_kind": {
            kind: dict(counter.most_common())
            for kind, counter in sorted(kind_lengths.items())
        },
        "samples_by_kind": samples_by_kind,
    }


def format_report(stats: dict) -> list[str]:
    """Build human-readable report lines."""
    lines = [
        f"Column: {stats['column']}",
        f"Total rows: {stats['total_rows']}",
        f"Non-empty rows: {stats['non_empty_rows']}",
        f"Unique values: {stats['unique_values']}",
        "",
        "Value kinds:",
    ]

    for kind, count in stats["value_kinds"].items():
        pct = 100 * count / stats["total_rows"]
        lines.append(f"  {kind}: {count} ({pct:.1f}%)")
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

    lines.append("Length by value kind:")
    for kind, lengths in stats["lengths_by_kind"].items():
        parts = ", ".join(f"len {n}={c}" for n, c in lengths.items())
        lines.append(f"  {kind}: {parts}")
    lines.append("")

    lines.append("Sample values by kind:")
    for kind, samples in stats["samples_by_kind"].items():
        lines.append(f"  {kind}:")
        for sample in samples:
            lines.append(f"    - {sample}")

    return lines


def main() -> int:
    if not ATTACHMENTS_CSV_PATH.exists():
        print(f"Missing CSV file: {ATTACHMENTS_CSV_PATH}", file=sys.stderr)
        return 1

    rows = load_attachments_csv()
    if not rows or COLUMN not in rows[0]:
        print(f"Column not found in CSV: {COLUMN}", file=sys.stderr)
        return 1

    output_path = write_report(OUTPUT_FILE, format_report(analyze_decision_number(rows)))
    print(f"Report written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
