"""Flatten merger case JSON, scan decision PDFs, and write attachments.csv."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import requests

from pdf_processing import (
    PdfJobResult,
    apply_pdf_result,
    init_pdf_worker,
    pdf_job_from_row,
    process_pdf_job,
    process_pdf_job_packed,
)
from download_json import download_json as fetch_case_json
from pipeline_utils import (
    ATT_DYNAMIC_EXCLUDE,
    ATTACHMENTS_CSV_PATH,
    ATTACHMENTS_EXCLUDED_COLUMNS,
    ATTACHMENTS_SUMMARY_COLUMNS,
    ATTACHMENTS_SUMMARY_CSV_PATH,
    CASE_METADATA_EXCLUDE,
    CASE_SECTORS_COLUMNS,
    CASE_SECTORS_CSV_PATH,
    FIXED_COLUMNS,
    KEYWORDS_PATH,
    RAW_JSON_PATH,
    build_case_sector_rows,
    empty_pdf_columns,
    first_list_value,
    flatten_metadata,
    is_successfully_processed,
    load_keyword_rules,
    normalize_decision_number,
    parse_code_label_items,
    preserve_pdf_columns,
    sanitize_csv_cell,
    setup_logging,
    should_process_pdf,
    strip_attachment_excluded_columns,
)

CSV_SAVE_RETRIES = 5
CSV_SAVE_RETRY_DELAYS = (0.5, 1.0, 2.0, 3.0, 5.0)
DEFAULT_PDF_WORKERS = 6
DEFAULT_PDF_SAVE_EVERY = 100


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
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Parallel PDF workers (default: 6). "
            "Uses a process pool when > 1 for CPU-bound PDF parsing."
        ),
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=None,
        help="Write CSV exports after every N PDFs (default: 100)",
    )
    args = parser.parse_args()

    if args.test_limit is None:
        env_limit = os.environ.get("TEST_LIMIT")
        if env_limit:
            args.test_limit = int(env_limit)

    if not args.retry_downloads and os.environ.get("RETRY_DOWNLOAD_ERRORS") == "1":
        args.retry_downloads = True

    if args.workers is None:
        env_workers = os.environ.get("PDF_WORKERS")
        args.workers = int(env_workers) if env_workers else DEFAULT_PDF_WORKERS
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    if args.save_every is None:
        env_save_every = os.environ.get("PDF_SAVE_EVERY")
        args.save_every = int(env_save_every) if env_save_every else DEFAULT_PDF_SAVE_EVERY
    if args.save_every < 1:
        parser.error("--save-every must be at least 1")

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


def collect_pending_rows(
    rows: list[dict[str, str]],
    retry_downloads: bool,
    test_limit: int | None,
) -> list[dict[str, str]]:
    """Return attachment rows that still need PDF processing this run."""
    pending = [row for row in rows if should_process_pdf(row, retry_downloads)]
    if test_limit is None:
        return pending
    return pending[:test_limit]


def record_pdf_result(result: PdfJobResult, row: dict[str, str]) -> None:
    """Apply worker output to the in-memory attachment row."""
    apply_pdf_result(row, result)


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
    write_columns_csv(rows, ATTACHMENTS_SUMMARY_COLUMNS, ATTACHMENTS_SUMMARY_CSV_PATH)


def write_columns_csv(
    rows: list[dict[str, str]],
    fieldnames: list[str],
    path: Path,
) -> None:
    """Atomically write a fixed-schema CSV subset."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
    write_columns_csv(rows, CASE_SECTORS_COLUMNS, path)


def write_attachment_exports(
    data: dict,
    rows: list[dict[str, str]],
    dynamic_columns: set[str],
) -> int:
    """Write attachments.csv, attachments_summary.csv, and case_sectors.csv."""
    write_csv(rows, dynamic_columns, ATTACHMENTS_CSV_PATH)
    case_sector_rows = build_case_sector_rows(data, rows)
    write_case_sectors_csv(case_sector_rows, CASE_SECTORS_CSV_PATH)
    return len(case_sector_rows)


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


def process_pdfs_parallel(
    data: dict,
    rows: list[dict[str, str]],
    dynamic_columns: set[str],
    pending_rows: list[dict[str, str]],
    rules_by_language: dict,
    request_delay: float,
    workers: int,
    save_every: int,
) -> tuple[int, int, int, float | None]:
    """
    Process pending PDFs sequentially or with a process pool.

    Returns (processed_count, hit_count, error_count, started_at).
    """
    if not pending_rows:
        return 0, 0, 0, None

    processed_count = 0
    hit_count = 0
    error_count = 0
    completed_since_save = 0
    pdf_processing_started_at = time.monotonic()
    save_lock = threading.Lock()
    row_by_ref = {row["att_metadataReference"]: row for row in pending_rows}
    pool_mode = "sequential" if workers == 1 else f"{workers} processes"

    logging.info(
        "Starting PDF processing: %s PDF(s) (%s, save every %s)",
        len(pending_rows),
        pool_mode,
        save_every,
    )

    def save_exports(force: bool = False) -> None:
        nonlocal completed_since_save
        with save_lock:
            if not force and completed_since_save < save_every:
                return
            write_attachment_exports(data, rows, dynamic_columns)
            completed_since_save = 0

    def record_result(result: PdfJobResult) -> None:
        nonlocal processed_count, hit_count, error_count, completed_since_save
        row = row_by_ref[result.att_metadataReference]
        record_pdf_result(result, row)

        processed_count += 1
        if row.get("has_keyword_hit") == "true":
            hit_count += 1
        if row.get("pdf_processing_error"):
            error_count += 1
        completed_since_save += 1

        if workers == 1:
            logging.info(
                "Processing PDF %s/%s: %s",
                processed_count,
                len(pending_rows),
                result.att_metadataReference,
            )
        elif processed_count % 100 == 0:
            elapsed = time.monotonic() - pdf_processing_started_at
            logging.info(
                "Progress: processed=%s/%s hits=%s errors=%s elapsed=%s",
                processed_count,
                len(pending_rows),
                hit_count,
                error_count,
                format_duration(elapsed),
            )

        save_exports()

    if workers == 1:
        with requests.Session() as session:
            for row in pending_rows:
                result = process_pdf_job(
                    pdf_job_from_row(row),
                    rules_by_language,
                    request_delay,
                    session=session,
                )
                record_result(result)
    else:
        job_payloads = [asdict(pdf_job_from_row(row)) for row in pending_rows]
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=init_pdf_worker,
            initargs=(rules_by_language, request_delay),
        ) as executor:
            futures = [
                executor.submit(process_pdf_job_packed, job_dict)
                for job_dict in job_payloads
            ]
            for future in as_completed(futures):
                record_result(future.result())

    if completed_since_save > 0:
        save_exports(force=True)

    return processed_count, hit_count, error_count, pdf_processing_started_at


def ensure_case_json() -> int:
    """Download case JSON when the local raw file is missing."""
    if RAW_JSON_PATH.exists():
        return 0
    logging.info("Raw JSON not found at %s — downloading first", RAW_JSON_PATH)
    return fetch_case_json()


def process_attachments(args: argparse.Namespace) -> int:
    """Run flattening, incremental PDF processing, and CSV export."""
    if ensure_case_json() != 0:
        return 1

    if not KEYWORDS_PATH.exists():
        logging.error("Missing keyword file: %s", KEYWORDS_PATH)
        return 1

    data = json.loads(RAW_JSON_PATH.read_text(encoding="utf-8"))
    new_rows, _ = build_rows_from_json(data)
    existing_by_key = load_existing_csv(ATTACHMENTS_CSV_PATH)

    rows = merge_rows(new_rows, existing_by_key, args.retry_downloads)
    dynamic_columns = collect_dynamic_columns(rows)

    empty_decision_numbers = sum(1 for row in rows if not row.get("dec_decisionNumber"))
    if empty_decision_numbers:
        logging.warning(
            "dec_decisionNumber empty for %s row(s) (missing in source JSON)",
            empty_decision_numbers,
        )

    case_sector_count = write_attachment_exports(data, rows, dynamic_columns)
    logging.info(
        "Wrote %s case-sector row(s) to %s",
        case_sector_count,
        CASE_SECTORS_CSV_PATH,
    )

    rules_by_language = load_keyword_rules(KEYWORDS_PATH)
    request_delay = float(os.environ.get("REQUEST_DELAY_SECONDS", "0"))
    pending_rows = collect_pending_rows(rows, args.retry_downloads, args.test_limit)

    processed_count = 0
    hit_count = 0
    error_count = 0
    pdf_processing_started_at: float | None = None
    exit_code = 0

    try:
        processed_count, hit_count, error_count, pdf_processing_started_at = (
            process_pdfs_parallel(
                data,
                rows,
                dynamic_columns,
                pending_rows,
                rules_by_language,
                request_delay,
                args.workers,
                args.save_every,
            )
        )

    except KeyboardInterrupt:
        exit_code = 130
        try:
            write_attachment_exports(data, rows, dynamic_columns)
        except PermissionError as exc:
            logging.error("%s", exc)
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
