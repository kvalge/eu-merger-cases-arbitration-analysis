"""Analyze value types and literal values for every column in attachments.csv."""

from __future__ import annotations

import re
import sys
from collections import Counter

from analysis_utils import ATTACHMENTS_CSV_PATH, load_attachments_csv, write_report

OUTPUT_FILE = "column_value_types.txt"

MAX_LITERAL_VALUES = 30
TOP_LITERAL_VALUES = 15
MAX_SAMPLE_LEN = 120

GUID_PATTERN = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
INTEGER_PATTERN = re.compile(r"^-?\d+$")
DECIMAL_PATTERN = re.compile(r"^-?\d+\.\d+$")
ISO_UTC_OFFSET_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
ISO_UTC_Z_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ISO_OFFSET_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$"
)
DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)
ERROR_PREFIX_PATTERN = re.compile(r"^(download|processing):", re.IGNORECASE)
BOOLEAN_VALUES = {"true", "false"}


def truncate(value: str, limit: int = MAX_SAMPLE_LEN) -> str:
    """Shorten long values for readable reports."""
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def classify_value_type(value: str) -> str:
    """Classify one cell into a structural value type."""
    if value == "":
        return "empty"
    if value.strip() == "":
        return "whitespace_only"
    lowered = value.lower()
    if lowered in {"null", "none", "nan"}:
        return "literal_null"
    if lowered in BOOLEAN_VALUES:
        return "boolean_string"
    if GUID_PATTERN.match(value):
        return "uuid"
    if ISO_UTC_OFFSET_PATTERN.match(value):
        return "iso_datetime_utc_offset"
    if ISO_UTC_Z_PATTERN.match(value):
        return "iso_datetime_utc_z"
    if ISO_OFFSET_PATTERN.match(value):
        return "iso_datetime_other_offset"
    if DATE_ONLY_PATTERN.match(value):
        return "date_only"
    if INTEGER_PATTERN.match(value):
        return "integer"
    if DECIMAL_PATTERN.match(value):
        return "decimal"
    if URL_PATTERN.match(value):
        return "url"
    if ERROR_PREFIX_PATTERN.match(value):
        return "error_message"
    if " | " in value:
        return "pipe_separated"
    return "other_string"


def analyze_column(column: str, rows: list[dict[str, str]]) -> dict:
    """Build stats for one CSV column."""
    values = [row.get(column, "") for row in rows]
    total = len(values)
    empty_count = sum(1 for value in values if value == "")
    filled_count = total - empty_count

    value_types = Counter(classify_value_type(value) for value in values)
    literal_values = Counter(values)

    samples_by_type: dict[str, list[str]] = {}
    for value in sorted(set(values), key=lambda item: (item == "", item.lower(), item)):
        value_type = classify_value_type(value)
        samples = samples_by_type.setdefault(value_type, [])
        if len(samples) < 5:
            samples.append(truncate(value))

    return {
        "column": column,
        "total_rows": total,
        "empty_count": empty_count,
        "filled_count": filled_count,
        "unique_values": len(literal_values),
        "value_types": value_types,
        "literal_values": literal_values,
        "samples_by_type": samples_by_type,
    }


def analyze_all_columns(rows: list[dict[str, str]]) -> list[dict]:
    """Analyze every column present in the CSV header."""
    if not rows:
        return []
    return [analyze_column(column, rows) for column in rows[0].keys()]


def format_column_section(stats: dict) -> list[str]:
    """Format one column block for the report."""
    total = stats["total_rows"]
    empty = stats["empty_count"]
    filled = stats["filled_count"]
    empty_pct = 100 * empty / total if total else 0
    filled_pct = 100 * filled / total if total else 0

    lines = [
        "=" * 80,
        f"Column: {stats['column']}",
        "-" * 80,
        f"Total rows: {total}",
        f"Empty: {empty} ({empty_pct:.1f}%)",
        f"Filled: {filled} ({filled_pct:.1f}%)",
        f"Unique literal values: {stats['unique_values']}",
        "",
        "Value types (shape / format):",
    ]

    for value_type, count in stats["value_types"].most_common():
        pct = 100 * count / total if total else 0
        lines.append(f"  {value_type}: {count} ({pct:.1f}%)")
    lines.append("")

    unique = stats["unique_values"]
    literal_counts = stats["literal_values"]

    if unique == 0:
        lines.append("Distinct literal values: (none)")
    elif unique <= MAX_LITERAL_VALUES:
        lines.append("Distinct literal values:")
        for value, count in literal_counts.most_common():
            label = "(empty)" if value == "" else truncate(value)
            pct = 100 * count / total if total else 0
            lines.append(f"  {label}: {count} ({pct:.1f}%)")
    else:
        lines.append(
            f"Distinct literal values: {unique} total "
            f"(showing top {TOP_LITERAL_VALUES} by frequency)"
        )
        for value, count in literal_counts.most_common(TOP_LITERAL_VALUES):
            label = "(empty)" if value == "" else truncate(value)
            pct = 100 * count / total if total else 0
            lines.append(f"  {label}: {count} ({pct:.1f}%)")

    if stats["samples_by_type"]:
        lines.append("")
        lines.append("Sample values by value type:")
        for value_type in sorted(stats["samples_by_type"]):
            lines.append(f"  {value_type}:")
            for sample in stats["samples_by_type"][value_type]:
                display = "(empty)" if sample == "" else sample
                lines.append(f"    - {display}")

    lines.append("")
    return lines


def format_report(column_stats: list[dict]) -> list[str]:
    """Build the full report."""
    if not column_stats:
        return ["No columns found in attachments.csv."]

    total_rows = column_stats[0]["total_rows"]
    lines = [
        "attachments.csv column value type analysis",
        f"Columns analyzed: {len(column_stats)}",
        f"Rows analyzed: {total_rows}",
        "",
        "Value types describe the shape of each cell (empty, boolean_string, url, etc.).",
        "Distinct literal values list the exact text stored in the CSV when cardinality is low.",
        "",
    ]

    for stats in column_stats:
        lines.extend(format_column_section(stats))

    return lines


def main() -> int:
    if not ATTACHMENTS_CSV_PATH.exists():
        print(f"Missing CSV file: {ATTACHMENTS_CSV_PATH}", file=sys.stderr)
        return 1

    rows = load_attachments_csv()
    if not rows:
        print("CSV has no data rows.", file=sys.stderr)
        return 1

    output_path = write_report(OUTPUT_FILE, format_report(analyze_all_columns(rows)))
    print(f"Report written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
