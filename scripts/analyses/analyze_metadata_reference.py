"""Analyze uniqueness of dec_metadataReference in attachments.csv."""

from __future__ import annotations

import sys
from collections import Counter

from analysis_utils import ATTACHMENTS_CSV_PATH, load_attachments_csv, write_report

COLUMN = "dec_metadataReference"
OUTPUT_FILE = "metadata_reference_uniqueness.txt"
MAX_DUPLICATE_EXAMPLES = 20


def analyze_metadata_reference(rows: list[dict[str, str]]) -> dict:
    """Build uniqueness stats for dec_metadataReference."""
    values = [row.get(COLUMN, "") for row in rows]
    value_counts = Counter(values)
    empty_count = value_counts.get("", 0)
    non_empty_counts = {
        value: count for value, count in value_counts.items() if value
    }

    duplicate_values = {
        value: count for value, count in non_empty_counts.items() if count > 1
    }
    rows_in_duplicates = sum(duplicate_values.values())
    extra_rows_from_duplication = rows_in_duplicates - len(duplicate_values)

    duplicate_count_distribution = Counter(duplicate_values.values())

    top_duplicates = sorted(
        duplicate_values.items(),
        key=lambda item: (-item[1], item[0]),
    )[:MAX_DUPLICATE_EXAMPLES]

    return {
        "column": COLUMN,
        "total_rows": len(rows),
        "empty_rows": empty_count,
        "non_empty_rows": len(rows) - empty_count,
        "unique_non_empty_values": len(non_empty_counts),
        "all_unique": not duplicate_values and empty_count <= 1,
        "duplicate_value_count": len(duplicate_values),
        "rows_with_duplicate_values": rows_in_duplicates,
        "extra_rows_from_duplication": extra_rows_from_duplication,
        "duplicate_count_distribution": dict(
            sorted(duplicate_count_distribution.items())
        ),
        "top_duplicates": top_duplicates,
    }


def format_report(stats: dict) -> list[str]:
    """Build human-readable report lines."""
    all_unique = stats["all_unique"]
    verdict = "YES — every non-empty value appears exactly once" if all_unique else (
        "NO — some values appear on more than one row"
    )

    lines = [
        f"Column: {stats['column']}",
        f"Total rows: {stats['total_rows']}",
        f"Non-empty rows: {stats['non_empty_rows']}",
        f"Empty rows: {stats['empty_rows']}",
        f"Unique non-empty values: {stats['unique_non_empty_values']}",
        "",
        f"All values unique? {verdict}",
        "",
    ]

    if not all_unique:
        lines.extend(
            [
                "Duplicate summary:",
                f"  Values that repeat: {stats['duplicate_value_count']}",
                f"  Rows using those values: {stats['rows_with_duplicate_values']}",
                f"  Extra rows beyond one per value: {stats['extra_rows_from_duplication']}",
                "",
                "How many rows share the same value:",
            ]
        )
        for repeat_count, value_count in stats["duplicate_count_distribution"].items():
            lines.append(f"  appears {repeat_count} times: {value_count} value(s)")
        lines.append("")
        lines.append(
            "Note: duplicates are expected when one decision has multiple PDF attachments; "
            "each attachment row shares the same dec_metadataReference."
        )
        lines.append("")
        lines.append(f"Top duplicate values (up to {MAX_DUPLICATE_EXAMPLES}):")
        for value, count in stats["top_duplicates"]:
            lines.append(f"  {count}x  {value}")
    else:
        lines.append(
            "Each row has a distinct dec_metadataReference (or at most one empty value)."
        )

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
        format_report(analyze_metadata_reference(rows)),
    )
    print(f"Report written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
