"""Flatten merger case JSON, scan decision PDFs, and write attachments.csv."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

from pipeline_utils import (
    ATT_DYNAMIC_EXCLUDE,
    ATTACHMENTS_CSV_PATH,
    ATTACHMENTS_EXCLUDED_COLUMNS,
    CASE_METADATA_EXCLUDE,
    CASE_SECTORS_COLUMNS,
    CASE_SECTORS_CSV_PATH,
    FIXED_COLUMNS,
    KEYWORDS_PATH,
    RAW_JSON_PATH,
    build_case_sector_rows,
    empty_pdf_columns,
    extract_pdf_text,
    first_list_value,
    flatten_metadata,
    is_successfully_processed,
    load_keyword_rules,
    match_keywords,
    normalize_decision_number,
    parse_code_label_items,
    preserve_pdf_columns,
    request_with_retries,
    sanitize_csv_cell,
    setup_logging,
    should_process_pdf,
    strip_attachment_excluded_columns,
    utc_now_iso,
)

CSV_SAVE_RETRIES = 5
CSV_SAVE_RETRY_DELAYS = (0.5, 1.0, 2.0, 3.0, 5.0)


def parse_args() -> argparse.Namespace:
    """Parse CLI flags and environment overrides."""
    parser = argparse.ArgumentParser(description="Process decision PDF attachments.")
    parser.add_argument(
        "--test-limit",
        type=int,
        default=None,
        help="Process at most N PDFs this run",
    )
    parser.add_argument(
        "--retry-downloads",
        action="store_true",
        help="Retry rows with download errors",
    )
    args = parser.parse_args()

    if args.test_limit is None:
        env_limit = os.environ.get("TEST_LIMIT")
        if env_limit:
            args.test_limit = int(env_limit)

    if not args.retry_downloads and os.environ.get("RETRY_DOWNLOAD_ERRORS") == "1":
        args.retry_downloads = True

    return args


def load_existing_csv(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """Load existing CSV rows indexed by attachment key."""
    if not path.exists():
        return {}

    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            (row["att_attachmentLink"], row["att_metadataReference"]): dict(row)
            for row in reader
        }


def collect_dynamic_columns(rows: list[dict[str, str]]) -> set[str]:
    """Collect non-fixed columns that have at least one non-empty value."""
    columns: set[str] = set()
    for row in rows:
        columns.update(
            key
            for key in row
            if key not in FIXED_COLUMNS and key not in ATTACHMENTS_EXCLUDED_COLUMNS
        )
    return {
        column
        for column in columns
        if any(row.get(column) for row in rows)
    }


def build_rows_from_json(data: dict) -> tuple[list[dict[str, str]], set[str]]:
    """Flatten JSON into attachment rows and collect dynamic column names."""
    rows: list[dict[str, str]] = []
    dynamic_columns: set[str] = set()

    for case in data.values():
        case_meta = case.get("metadata") or {}
        case_flat = flatten_metadata(
            case_meta,
            "case_",
            exclude=CASE_METADATA_EXCLUDE,
        )
        dynamic_columns.update(case_flat.keys())

        for decision in case.get("decisions") or []:
            dec_meta = decision.get("metadata") or {}
            decision_code, decision_label = parse_code_label_items(
                dec_meta.get("decisionTypes")
            )
            dec_flat = flatten_metadata(dec_meta, "dec_")
            if "dec_decisionNumber" in dec_flat:
                dec_flat["dec_decisionNumber"] = normalize_decision_number(
                    dec_flat["dec_decisionNumber"]
                )
            dynamic_columns.update(dec_flat.keys())

            for attachment in decision.get("decisionAttachments") or []:
                att_meta = attachment.get("metadata") or {}
                link = first_list_value(att_meta, "attachmentLink")
                ref = first_list_value(att_meta, "metadataReference")
                if not link or not ref:
                    continue

                att_flat = flatten_metadata(
                    att_meta,
                    "att_",
                    exclude=ATT_DYNAMIC_EXCLUDE,
                )
                dynamic_columns.update(att_flat.keys())

                row = {
                    "att_attachmentLink": link,
                    "att_metadataReference": ref,
                    **empty_pdf_columns(),
                    "decision_type_code": decision_code,
                    "decision_type_label": decision_label,
                    "is_active": "true",
                    **case_flat,
                    **dec_flat,
                    **att_flat,
                }
                strip_attachment_excluded_columns(row)
                rows.append(row)

    return rows, dynamic_columns


def merge_rows(
    new_rows: list[dict[str, str]],
    existing_by_key: dict[tuple[str, str], dict[str, str]],
    retry_downloads: bool,
) -> list[dict[str, str]]:
    """Merge JSON rows with existing CSV state."""
    merged: list[dict[str, str]] = []
    new_keys: set[tuple[str, str]] = set()

    for row in new_rows:
        key = (row["att_attachmentLink"], row["att_metadataReference"])
        new_keys.add(key)

        if key in existing_by_key:
            old = existing_by_key[key]
            if is_successfully_processed(old) or not should_process_pdf(
                old, retry_downloads
            ):
                preserve_pdf_columns(row, old)

        merged.append(row)

    for key, old_row in existing_by_key.items():
        if key not in new_keys:
            inactive = dict(old_row)
            inactive["is_active"] = "false"
            strip_attachment_excluded_columns(inactive)
            merged.append(inactive)

    return merged


def format_duration(seconds: float) -> str:
    """Format elapsed seconds as a human-readable duration."""
    total_seconds = int(seconds)
    if total_seconds < 60:
        return f"{seconds:.1f}s"

    minutes, secs = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


def count_pdfs_to_process(
    rows: list[dict[str, str]],
    retry_downloads: bool,
    test_limit: int | None,
) -> int:
    """Count how many PDFs will be processed in this run."""
    pending = sum(1 for row in rows if should_process_pdf(row, retry_downloads))
    if test_limit is None:
        return pending
    return min(pending, test_limit)


def count_overall_pdf_progress(rows: list[dict[str, str]]) -> tuple[int, int]:
    """Return (processed_count, total_count) across all attachment rows."""
    total = len(rows)
    processed = sum(1 for row in rows if row.get("pdf_processed_at"))
    return processed, total


def resolve_attachment_language(row: dict[str, str]) -> str:
    """Read attachment language from flattened metadata columns."""
    language = row.get("att_attachmentLanguage") or row.get("att_language") or ""
    return language.strip().upper()


def process_pdf_row(
    row: dict[str, str],
    session: requests.Session,
    rules_by_language: dict,
    request_delay: float,
) -> None:
    """Download, extract, and keyword-scan one attachment row."""
    link = row["att_attachmentLink"]
    ref = row["att_metadataReference"]
    language = resolve_attachment_language(row)

    if not language:
        logging.warning("Missing attachment language for %s (%s)", ref, link)
        row.update(
            {
                "has_keyword_hit": "false",
                "matchedKeywords": "",
                "matchedLanguage": "",
                "matchContext": "",
                "pdf_processed_at": utc_now_iso(),
                "pdf_processing_error": "",
            }
        )
        return

    try:
        response = request_with_retries("GET", link, session=session)
        pdf_bytes = response.content
        text = extract_pdf_text(pdf_bytes)

        if not rules_by_language.get(language):
            logging.warning("No keyword rules for language %s (%s)", language, ref)
            row.update(
                {
                    "has_keyword_hit": "false",
                    "matchedKeywords": "",
                    "matchedLanguage": language,
                    "matchContext": "",
                    "pdf_processed_at": utc_now_iso(),
                    "pdf_processing_error": "",
                }
            )
            return

        hit, matched_keywords, matched_language, match_context = match_keywords(
            text, language, rules_by_language
        )

        row.update(
            {
                "has_keyword_hit": "true" if hit else "false",
                "matchedKeywords": matched_keywords,
                "matchedLanguage": matched_language,
                "matchContext": match_context,
                "pdf_processed_at": utc_now_iso(),
                "pdf_processing_error": "",
            }
        )

    except requests.RequestException as exc:
        logging.error("Download failed for %s (%s): %s", ref, link, exc)
        row.update(
            {
                "has_keyword_hit": "false",
                "matchedKeywords": "",
                "matchedLanguage": "",
                "matchContext": "",
                "pdf_processed_at": utc_now_iso(),
                "pdf_processing_error": f"download: {exc}",
            }
        )
    except Exception as exc:
        logging.error("Processing failed for %s (%s): %s", ref, link, exc)
        row.update(
            {
                "has_keyword_hit": "false",
                "matchedKeywords": "",
                "matchedLanguage": "",
                "matchContext": "",
                "pdf_processed_at": utc_now_iso(),
                "pdf_processing_error": f"processing: {exc}",
            }
        )
    finally:
        if request_delay > 0:
            time.sleep(request_delay)


def write_csv(
    rows: list[dict[str, str]],
    dynamic_columns: set[str],
    path: Path,
) -> None:
    """Atomically write rows to CSV with fixed columns first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = FIXED_COLUMNS + sorted(
        column for column in dynamic_columns if column not in ATTACHMENTS_EXCLUDED_COLUMNS
    )
    temp_path = path.with_name(path.name + ".tmp")

    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    column: sanitize_csv_cell(row.get(column, ""))
                    for column in fieldnames
                }
            )

    replace_csv_atomically(temp_path, path)


def write_case_sectors_csv(rows: list[dict[str, str]], path: Path) -> None:
    """Atomically write the normalized case-sector lookup CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")

    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CASE_SECTORS_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    column: sanitize_csv_cell(row.get(column, ""))
                    for column in CASE_SECTORS_COLUMNS
                }
            )

    replace_csv_atomically(temp_path, path)


def replace_csv_atomically(temp_path: Path, path: Path) -> None:
    """Replace a CSV file using the same retry logic as attachment saves."""
    last_error: OSError | None = None
    for attempt in range(CSV_SAVE_RETRIES):
        try:
            if path.exists():
                path.unlink()
            temp_path.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt < CSV_SAVE_RETRIES - 1:
                delay = CSV_SAVE_RETRY_DELAYS[attempt]
                logging.warning(
                    "Could not save %s (file may be open in Excel or another program) — "
                    "retrying in %ss (%s/%s)",
                    path,
                    delay,
                    attempt + 1,
                    CSV_SAVE_RETRIES,
                )
                time.sleep(delay)

    if temp_path.exists():
        fallback_path = path.with_name(path.name + ".partial")
        try:
            temp_path.replace(fallback_path)
            logging.error(
                "Saved progress to %s because %s is locked. Close programs using the CSV, "
                "then rename the partial file or re-run the pipeline.",
                fallback_path,
                path,
            )
        except OSError:
            pass

    raise PermissionError(
        f"Could not save {path}. Close Excel, editors, or other programs that have "
        f"attachments.csv open, then re-run. Progress up to the previous PDF is saved."
    ) from last_error


def log_run_summary(
    processed_count: int,
    hit_count: int,
    error_count: int,
    pdf_processing_started_at: float | None,
    *,
    interrupted: bool = False,
    overall_processed: int | None = None,
    overall_total: int | None = None,
) -> None:
    """Log end-of-run or interrupted summary."""
    overall_suffix = ""
    if overall_processed is not None and overall_total is not None:
        overall_suffix = f" | Overall: {overall_processed}/{overall_total} PDFs"

    prefix = "Interrupted after" if interrupted else "Processed PDFs:"
    if pdf_processing_started_at is not None:
        elapsed = time.monotonic() - pdf_processing_started_at
        logging.info(
            "%s %s | Hits: %s | Errors: %s | Time: %s%s",
            prefix,
            processed_count,
            hit_count,
            error_count,
            format_duration(elapsed),
            overall_suffix,
        )
    else:
        logging.info(
            "%s %s | Hits: %s | Errors: %s%s",
            prefix,
            processed_count,
            hit_count,
            error_count,
            overall_suffix,
        )


def process_attachments(args: argparse.Namespace) -> int:
    """Run flattening, incremental PDF processing, and CSV export."""
    if not RAW_JSON_PATH.exists():
        logging.error("Missing JSON file: %s (run download_json.py first)", RAW_JSON_PATH)
        return 1

    if not KEYWORDS_PATH.exists():
        logging.error("Missing keyword file: %s", KEYWORDS_PATH)
        return 1

    data = json.loads(RAW_JSON_PATH.read_text(encoding="utf-8"))
    new_rows, _ = build_rows_from_json(data)
    existing_by_key = load_existing_csv(ATTACHMENTS_CSV_PATH)

    rows = merge_rows(new_rows, existing_by_key, args.retry_downloads)
    dynamic_columns = collect_dynamic_columns(rows)

    case_sector_rows = build_case_sector_rows(data)
    write_case_sectors_csv(case_sector_rows, CASE_SECTORS_CSV_PATH)
    logging.info(
        "Wrote %s case-sector row(s) to %s",
        len(case_sector_rows),
        CASE_SECTORS_CSV_PATH,
    )

    empty_decision_numbers = sum(1 for row in rows if not row.get("dec_decisionNumber"))
    if empty_decision_numbers:
        logging.warning(
            "dec_decisionNumber empty for %s row(s) (missing in source JSON)",
            empty_decision_numbers,
        )

    write_csv(rows, dynamic_columns, ATTACHMENTS_CSV_PATH)

    rules_by_language = load_keyword_rules(KEYWORDS_PATH)
    request_delay = float(os.environ.get("REQUEST_DELAY_SECONDS", "0"))

    processed_count = 0
    hit_count = 0
    error_count = 0
    pdfs_to_process = count_pdfs_to_process(rows, args.retry_downloads, args.test_limit)
    pdf_processing_started_at: float | None = None
    exit_code = 0

    try:
        with requests.Session() as session:
            for row in rows:
                if args.test_limit is not None and processed_count >= args.test_limit:
                    break
                if not should_process_pdf(row, args.retry_downloads):
                    continue

                if pdf_processing_started_at is None:
                    pdf_processing_started_at = time.monotonic()
                    logging.info(
                        "Starting PDF processing: %s PDF(s) to process",
                        pdfs_to_process,
                    )

                processed_count += 1
                ref = row["att_metadataReference"]
                logging.info(
                    "Processing PDF %s/%s: %s",
                    processed_count,
                    pdfs_to_process,
                    ref,
                )

                process_pdf_row(row, session, rules_by_language, request_delay)
                write_csv(rows, dynamic_columns, ATTACHMENTS_CSV_PATH)

                if row.get("has_keyword_hit") == "true":
                    hit_count += 1
                if row.get("pdf_processing_error"):
                    error_count += 1

                if processed_count % 100 == 0:
                    elapsed = time.monotonic() - pdf_processing_started_at
                    logging.info(
                        "Progress: processed=%s/%s hits=%s errors=%s elapsed=%s",
                        processed_count,
                        pdfs_to_process,
                        hit_count,
                        error_count,
                        format_duration(elapsed),
                    )

    except KeyboardInterrupt:
        exit_code = 130
        overall_processed, overall_total = count_overall_pdf_progress(rows)
        logging.warning(
            "Interrupted — %s/%s PDFs processed overall (%s this run) — progress saved to %s",
            overall_processed,
            overall_total,
            processed_count,
            ATTACHMENTS_CSV_PATH,
        )
        log_run_summary(
            processed_count,
            hit_count,
            error_count,
            pdf_processing_started_at,
            interrupted=True,
            overall_processed=overall_processed,
            overall_total=overall_total,
        )
        return exit_code

    except PermissionError as exc:
        overall_processed, overall_total = count_overall_pdf_progress(rows)
        logging.error("%s", exc)
        logging.warning(
            "Stopped — %s/%s PDFs processed overall (%s completed before the failed save)",
            overall_processed,
            overall_total,
            max(0, processed_count - 1),
        )
        return 1

    overall_processed, overall_total = count_overall_pdf_progress(rows)
    log_run_summary(
        processed_count,
        hit_count,
        error_count,
        pdf_processing_started_at,
        overall_processed=overall_processed,
        overall_total=overall_total,
    )
    if error_count > 0:
        logging.info("Some downloads failed — re-run with --retry-downloads")

    return exit_code


def main() -> int:
    setup_logging()
    return process_attachments(parse_args())


if __name__ == "__main__":
    sys.exit(main())
