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

PROJECT_ROOT = Path(__file__).resolve().parent.parent

JSON_URL = (
    "https://compcases-open-data-portal-files-prod.s3.eu-west-1.amazonaws.com/case-data-M.json"
)
RAW_JSON_PATH = PROJECT_ROOT / "data/raw/case-data-M.json"
RAW_JSON_TEMP_PATH = PROJECT_ROOT / "data/raw/case-data-M.json.tmp"
ATTACHMENTS_CSV_PATH = PROJECT_ROOT / "data/processed/attachments.csv"
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
    "sector_code",
    "sector_label",
    "is_active",
]

PDF_RESULT_COLUMNS = [
    "has_keyword_hit",
    "matchedKeywords",
    "matchedLanguage",
    "matchContext",
    "pdf_processed_at",
    "pdf_processing_error",
]

ATT_DYNAMIC_EXCLUDE = {"attachmentLink", "metadataReference"}


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


def metadata_value_to_text(value: Any) -> str:
    """Convert a metadata value to a CSV-safe string."""
    if isinstance(value, list):
        return " | ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def flatten_metadata(
    metadata: dict[str, Any],
    prefix: str,
    *,
    exclude: set[str] | None = None,
) -> dict[str, str]:
    """Flatten metadata dict into prefixed text columns."""
    excluded = exclude or set()
    flattened: dict[str, str] = {}
    for key, value in (metadata or {}).items():
        if key in excluded:
            continue
        flattened[f"{prefix}{key}"] = metadata_value_to_text(value)
    return flattened


def parse_code_label_items(items: list[Any] | None) -> tuple[str, str]:
    """Parse JSON code/label objects from metadata list fields."""
    codes: list[str] = []
    labels: list[str] = []

    for item in items or []:
        try:
            obj = json.loads(item) if isinstance(item, str) else item
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        code = obj.get("code")
        label = obj.get("label")
        if code:
            codes.append(str(code))
        if label:
            labels.append(str(label))

    return " | ".join(codes), " | ".join(labels)


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
