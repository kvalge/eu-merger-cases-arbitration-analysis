"""Shared helpers for the merger-case arbitration analysis pipeline."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import pdfplumber
import requests

USER_AGENT = "eu-merger-cases-arbitration-analysis/1.0"
HTTP_TIMEOUT = 120
RETRY_DELAYS = (1, 2, 4)
MATCH_CONTEXT_LENGTH = 150

PROJECT_ROOT = Path(__file__).resolve().parents[2]

JSON_URL = (
    "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-M.json"
)
RAW_JSON_PATH = PROJECT_ROOT / "data/raw/case-data-M.json"
RAW_JSON_TEMP_PATH = PROJECT_ROOT / "data/raw/case-data-M.json.tmp"
ATTACHMENTS_CSV_PATH = PROJECT_ROOT / "data/processed/attachments.csv"
CASE_SECTORS_CSV_PATH = PROJECT_ROOT / "data/processed/case_sectors.csv"
SUMMARY_JSON_PATH = PROJECT_ROOT / "data/processed/summary.json"
KEYWORDS_PATH = PROJECT_ROOT / "config/keywords.txt"
MIN_CASE_COUNT = 1000

FIXED_COLUMNS = [
    "att_attachmentLink",
    "att_metadataReference",
    "has_keyword_hit",
    "matchedKeywords",
    "matchedLanguage",
    "matchContext",
    "pdf_processed_at",
    "pdf_processing_error",
    "decision_type_code",
    "decision_type_label",
    "is_active",
]

CASE_SECTORS_COLUMNS = [
    "case_caseNumber",
    "case_caseSectors_code",
    "case_caseSectors_label",
]

ATTACHMENTS_EXCLUDED_COLUMNS = frozenset(
    {
        "sector_code",
        "sector_label",
        "case_caseSectors_code",
        "case_caseSectors_label",
    }
)

CASE_METADATA_EXCLUDE = {"caseSectors"}

PDF_RESULT_COLUMNS = [
    "has_keyword_hit",
    "matchedKeywords",
    "matchedLanguage",
    "matchContext",
    "pdf_processed_at",
    "pdf_processing_error",
]


def sanitize_csv_cell(value: str) -> str:
    """Replace embedded line breaks so Excel can open the CSV as one row per record."""
    if not value:
        return value
    return value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")

ATT_DYNAMIC_EXCLUDE = {"attachmentLink", "metadataReference"}

DECISION_NUMBER_GUID_PATTERN = re.compile(
    r"^\{([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\}$"
)


def setup_logging() -> None:
    """Configure root logger for CLI scripts."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def request_with_retries(
    method: str,
    url: str,
    *,
    session: requests.Session | None = None,
    **kwargs: Any,
) -> requests.Response:
    """HTTP request with retries on transient failures."""
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    kwargs.setdefault("headers", {})
    kwargs["headers"].setdefault("User-Agent", USER_AGENT)

    client = session or requests
    last_error: Exception | None = None

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code >= 500:
                raise requests.HTTPError(f"HTTP {response.status_code} for {url}")
            response.raise_for_status()
            return response
        except (requests.RequestException, requests.HTTPError) as exc:
            last_error = exc
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])

    assert last_error is not None
    raise last_error


def first_list_value(metadata: dict[str, Any], key: str) -> str:
    """Return the first value from a metadata list field."""
    values = metadata.get(key) or []
    if not values:
        return ""
    return str(values[0])


def normalize_decision_number(value: str) -> str:
    """
    Normalize decision numbers to plain text strings.

    - Numeric values stay as digit strings (e.g. ``40398``).
    - GUID values lose braces and are uppercased (e.g. ``01760B06-...``).
    """
    text = value.strip()
    if not text:
        return ""
    # Metadata lists are joined with " | " when flattened.
    first_value = text.split(" | ")[0].strip()
    if first_value.isdigit():
        return first_value

    guid_match = DECISION_NUMBER_GUID_PATTERN.match(first_value)
    if guid_match:
        return guid_match.group(1).upper()

    return first_value


def _try_parse_json(value: Any) -> Any:
    """Parse JSON object/array strings; return other values unchanged."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _scalar_to_text(value: Any) -> str:
    """Convert a scalar metadata value to text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _join_text_parts(parts: list[str]) -> str:
    """Join text parts for a CSV cell."""
    return " | ".join(part for part in parts if part)


def _merge_column_parts(target: dict[str, list[str]], columns: dict[str, str]) -> None:
    """Append column values into grouped lists before final join."""
    for column, text in columns.items():
        if text:
            target.setdefault(column, []).append(text)


def _is_code_label_dict(obj: dict[str, Any]) -> bool:
    """True when an object uses the common code/label metadata shape."""
    return "code" in obj and "label" in obj


def _flatten_parsed_object(obj: dict[str, Any], field_key: str) -> dict[str, str]:
    """Expand one parsed metadata object into flat text columns."""
    if not obj:
        return {}

    if _is_code_label_dict(obj):
        return {
            f"{field_key}_code": _scalar_to_text(obj.get("code")),
            f"{field_key}_label": _scalar_to_text(obj.get("label")),
        }

    if "items" in obj and isinstance(obj["items"], list):
        item_parts: dict[str, list[str]] = {}
        for item in obj["items"]:
            if isinstance(item, dict):
                for sub_key, sub_value in item.items():
                    column = f"{field_key}_{sub_key}"
                    if isinstance(sub_value, dict):
                        nested = _flatten_parsed_object(sub_value, column)
                        _merge_column_parts(item_parts, nested)
                    elif isinstance(sub_value, list):
                        for list_item in sub_value:
                            parsed_item = _try_parse_json(list_item)
                            if isinstance(parsed_item, dict):
                                nested = _flatten_parsed_object(parsed_item, column)
                                _merge_column_parts(item_parts, nested)
                            else:
                                text = _scalar_to_text(
                                    parsed_item if parsed_item is not None else list_item
                                )
                                if text:
                                    item_parts.setdefault(column, []).append(text)
                    else:
                        text = _scalar_to_text(sub_value)
                        if text:
                            item_parts.setdefault(column, []).append(text)
            elif item is not None:
                text = _scalar_to_text(item)
                if text:
                    item_parts.setdefault(field_key, []).append(text)
        return {column: _join_text_parts(parts) for column, parts in item_parts.items()}

    generic_parts: dict[str, list[str]] = {}
    for sub_key, sub_value in obj.items():
        column = f"{field_key}_{sub_key}"
        if isinstance(sub_value, dict):
            nested = _flatten_parsed_object(sub_value, column)
            _merge_column_parts(generic_parts, nested)
        elif isinstance(sub_value, list):
            for list_item in sub_value:
                parsed_item = _try_parse_json(list_item)
                if isinstance(parsed_item, dict):
                    nested = _flatten_parsed_object(parsed_item, column)
                    _merge_column_parts(generic_parts, nested)
                else:
                    text = _scalar_to_text(parsed_item if parsed_item is not None else list_item)
                    if text:
                        generic_parts.setdefault(column, []).append(text)
        else:
            text = _scalar_to_text(sub_value)
            if text:
                generic_parts.setdefault(column, []).append(text)

    return {column: _join_text_parts(parts) for column, parts in generic_parts.items()}


def flatten_field_columns(value: Any, field_key: str) -> dict[str, str]:
    """Expand one metadata field into flat text columns without nested JSON."""
    items = value if isinstance(value, list) else [value]
    merged_parts: dict[str, list[str]] = {}

    for item in items:
        if item is None:
            continue
        parsed = _try_parse_json(item)
        if isinstance(parsed, dict):
            columns = _flatten_parsed_object(parsed, field_key)
            _merge_column_parts(merged_parts, columns)
        else:
            text = _scalar_to_text(parsed if parsed is not None else item)
            if text:
                merged_parts.setdefault(field_key, []).append(text)

    return {column: _join_text_parts(parts) for column, parts in merged_parts.items()}


def flatten_metadata(
    metadata: dict[str, Any],
    prefix: str,
    *,
    exclude: set[str] | None = None,
) -> dict[str, str]:
    """Flatten metadata dict into prefixed text columns without nested JSON values."""
    excluded = exclude or set()
    flattened: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if key in excluded:
            continue
        for sub_key, text in flatten_field_columns(value, key).items():
            flattened[f"{prefix}{sub_key}"] = text
    return flattened


def parse_code_label_item_list(items: list[Any] | None) -> list[tuple[str, str]]:
    """Parse JSON code/label objects into one (code, label) pair per list item."""
    pairs: list[tuple[str, str]] = []

    for item in items or []:
        try:
            obj = json.loads(item) if isinstance(item, str) else item
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        code = obj.get("code")
        label = obj.get("label")
        if not code and not label:
            continue
        pairs.append(
            (
                str(code) if code else "",
                str(label) if label else "",
            )
        )

    return pairs


def parse_code_label_items(items: list[Any] | None) -> tuple[str, str]:
    """Parse JSON code/label objects from metadata list fields."""
    pairs = parse_code_label_item_list(items)
    codes = [code for code, _ in pairs if code]
    labels = [label for _, label in pairs if label]
    return " | ".join(codes), " | ".join(labels)


def build_case_sector_rows(data: dict[str, Any]) -> list[dict[str, str]]:
    """Build one row per case sector (case number repeated when needed)."""
    rows: list[dict[str, str]] = []

    for case in data.values():
        case_meta = case.get("metadata") or {}
        case_number = first_list_value(case_meta, "caseNumber")
        if not case_number:
            continue

        for code, label in parse_code_label_item_list(case_meta.get("caseSectors")):
            rows.append(
                {
                    "case_caseNumber": case_number,
                    "case_caseSectors_code": code,
                    "case_caseSectors_label": label,
                }
            )

    rows.sort(
        key=lambda row: (
            row["case_caseNumber"],
            row["case_caseSectors_code"],
            row["case_caseSectors_label"],
        )
    )
    return rows


def strip_attachment_excluded_columns(row: dict[str, str]) -> None:
    """Remove sector columns that belong in case_sectors.csv only."""
    for column in ATTACHMENTS_EXCLUDED_COLUMNS:
        row.pop(column, None)


def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a keyword wildcard pattern to a case-insensitive regex."""
    parts = [".*" if char == "*" else re.escape(char) for char in pattern]
    return re.compile("".join(parts), re.IGNORECASE)


def load_keyword_rules(path: Path = KEYWORDS_PATH) -> dict[str, list[tuple[str, list[str]]]]:
    """
    Load keyword rules grouped by language.

    Each rule is (original_pattern, [sub_patterns]).
    """
    rules: dict[str, list[tuple[str, list[str]]]] = {}

    if not path.exists():
        logging.warning("Keyword file not found: %s", path)
        return rules

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            logging.warning("Invalid keyword line (missing ':'): %s", raw_line)
            continue

        lang, pattern_rest = line.split(":", 1)
        language = lang.strip().upper()
        sub_patterns = [part.strip() for part in pattern_rest.split(":") if part.strip()]
        if not language or not sub_patterns:
            logging.warning("Invalid keyword line: %s", raw_line)
            continue

        rules.setdefault(language, []).append((pattern_rest.strip(), sub_patterns))

    return rules


def normalize_whitespace(text: str) -> str:
    """Collapse whitespace to single spaces."""
    return re.sub(r"\s+", " ", text).strip()


def extract_match_context(text: str, position: int, length: int = MATCH_CONTEXT_LENGTH) -> str:
    """Return normalized text around a match position."""
    half = length // 2
    start = max(0, position - half)
    end = min(len(text), start + length)
    if end - start < length and start > 0:
        start = max(0, end - length)
    return normalize_whitespace(text[start:end])[:length]


def match_keywords(
    text: str,
    language: str,
    rules_by_language: dict[str, list[tuple[str, list[str]]]],
) -> tuple[bool, str, str, str]:
    """
    Match keyword rules against PDF text.

    Returns (hit, matched_keywords, matched_language, match_context).
    """
    rules = rules_by_language.get(language.upper(), [])
    if not rules:
        return False, "", language.upper(), ""

    earliest_pos: int | None = None
    matched_patterns: list[str] = []

    for original_pattern, sub_patterns in rules:
        regexes = [pattern_to_regex(sub_pattern) for sub_pattern in sub_patterns]
        if not all(regex.search(text) for regex in regexes):
            continue

        matched_patterns.append(original_pattern)
        for regex in regexes:
            match = regex.search(text)
            if match and (earliest_pos is None or match.start() < earliest_pos):
                earliest_pos = match.start()

    if not matched_patterns:
        return False, "", language.upper(), ""

    context = extract_match_context(text, earliest_pos or 0)
    return True, " | ".join(matched_patterns), language.upper(), context


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        pages = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(pages)


def is_successfully_processed(row: dict[str, str]) -> bool:
    """True when PDF processing completed without error."""
    return bool(row.get("pdf_processed_at")) and not row.get("pdf_processing_error")


def should_process_pdf(row: dict[str, str], retry_downloads: bool) -> bool:
    """Decide whether the PDF step should run for a row."""
    if not row.get("pdf_processed_at"):
        return True
    if retry_downloads and row.get("pdf_processing_error", "").startswith("download:"):
        return True
    return False


def preserve_pdf_columns(target: dict[str, str], source: dict[str, str]) -> None:
    """Copy PDF result columns from an existing CSV row."""
    for column in PDF_RESULT_COLUMNS:
        if column in source:
            target[column] = source[column]


def empty_pdf_columns() -> dict[str, str]:
    """Default PDF result values for a new row."""
    return {
        "has_keyword_hit": "false",
        "matchedKeywords": "",
        "matchedLanguage": "",
        "matchContext": "",
        "pdf_processed_at": "",
        "pdf_processing_error": "",
    }
