"""PDF download, fast text extraction, and page-by-page keyword scanning."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import fitz
import pdfplumber
import requests

from pipeline_utils import (
    empty_pdf_columns,
    match_keywords,
    request_with_retries,
    utc_now_iso,
)

# Picklable rules type: language -> list of (original_pattern, [sub_patterns])
KeywordRules = dict[str, list[tuple[str, list[str]]]]


@dataclass(frozen=True)
class PdfJob:
    """Inputs needed to process one attachment PDF."""

    att_attachmentLink: str
    att_metadataReference: str
    att_attachmentLanguage: str
    att_language: str


@dataclass(frozen=True)
class PdfJobResult:
    """PDF processing output for one attachment row."""

    att_metadataReference: str
    columns: dict[str, str]


def pdf_job_from_row(row: dict[str, str]) -> PdfJob:
    """Build a picklable job payload from a CSV row."""
    return PdfJob(
        att_attachmentLink=row["att_attachmentLink"],
        att_metadataReference=row["att_metadataReference"],
        att_attachmentLanguage=row.get("att_attachmentLanguage", ""),
        att_language=row.get("att_language", ""),
    )


def resolve_job_language(job: PdfJob) -> str:
    """Read attachment language from job fields."""
    language = job.att_attachmentLanguage or job.att_language or ""
    return language.strip().upper()


def iter_page_texts_pymupdf(pdf_bytes: bytes):
    """Yield page text from a PDF using PyMuPDF."""
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in document:
            yield page.get_text() or ""
    finally:
        document.close()


def extract_page_texts_pdfplumber(pdf_bytes: bytes) -> list[str]:
    """Extract all page texts with pdfplumber (fallback extractor)."""
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def scan_page_texts_for_keywords(
    page_texts,
    language: str,
    rules_by_language: KeywordRules,
) -> tuple[bool, str, str, str]:
    """
    Scan pages in order, matching against accumulated text.

    Stops early when a keyword hit is found (same semantics as scanning the
    full document, including AND rules spanning earlier pages).
    """
    accumulated: list[str] = []
    for page_text in page_texts:
        accumulated.append(page_text)
        combined = "\n".join(accumulated)
        hit, matched_keywords, matched_language, match_context = match_keywords(
            combined,
            language,
            rules_by_language,
        )
        if hit:
            return hit, matched_keywords, matched_language, match_context

    combined = "\n".join(accumulated)
    return match_keywords(combined, language, rules_by_language)


def scan_pdf_bytes_for_keywords(
    pdf_bytes: bytes,
    language: str,
    rules_by_language: KeywordRules,
) -> tuple[tuple[bool, str, str, str], str]:
    """
    Extract and scan a PDF for keywords.

    Returns ((hit, matched_keywords, matched_language, match_context), extractor).
    Primary extractor is PyMuPDF; pdfplumber is used on failure.
    """
    try:
        page_iter = iter_page_texts_pymupdf(pdf_bytes)
        result = scan_page_texts_for_keywords(page_iter, language, rules_by_language)
        return result, "pymupdf"
    except Exception as exc:
        logging.warning(
            "PyMuPDF failed for language %s, falling back to pdfplumber: %s",
            language,
            exc,
        )
        pages = extract_page_texts_pdfplumber(pdf_bytes)
        result = scan_page_texts_for_keywords(pages, language, rules_by_language)
        return result, "pdfplumber"


def build_success_columns(
    *,
    hit: bool,
    matched_keywords: str,
    matched_language: str,
    match_context: str,
) -> dict[str, str]:
    """Build PDF result columns for a successful scan."""
    return {
        "has_keyword_hit": "true" if hit else "false",
        "matchedKeywords": matched_keywords,
        "matchedLanguage": matched_language,
        "matchContext": match_context,
        "pdf_processed_at": utc_now_iso(),
        "pdf_processing_error": "",
    }


def build_missing_language_columns() -> dict[str, str]:
    """Build PDF result columns when attachment language is unknown."""
    columns = empty_pdf_columns()
    columns.update(
        {
            "has_keyword_hit": "false",
            "pdf_processed_at": utc_now_iso(),
        }
    )
    return columns


def build_no_rules_columns(language: str) -> dict[str, str]:
    """Build PDF result columns when no keyword rules exist for the language."""
    return build_success_columns(
        hit=False,
        matched_keywords="",
        matched_language=language,
        match_context="",
    )


def build_error_columns(error_prefix: str, message: str) -> dict[str, str]:
    """Build PDF result columns for download/processing failures."""
    columns = empty_pdf_columns()
    columns.update(
        {
            "has_keyword_hit": "false",
            "pdf_processed_at": utc_now_iso(),
            "pdf_processing_error": f"{error_prefix}: {message}",
        }
    )
    return columns


def process_pdf_job(
    job: PdfJob,
    rules_by_language: KeywordRules,
    request_delay: float,
    *,
    session: requests.Session | None = None,
) -> PdfJobResult:
    """Download, extract, and keyword-scan one attachment."""
    link = job.att_attachmentLink
    ref = job.att_metadataReference
    language = resolve_job_language(job)

    if not language:
        logging.warning("Missing attachment language for %s (%s)", ref, link)
        return PdfJobResult(ref, build_missing_language_columns())

    try:
        response = request_with_retries("GET", link, session=session)
        pdf_bytes = response.content

        if not rules_by_language.get(language):
            logging.warning("No keyword rules for language %s (%s)", language, ref)
            return PdfJobResult(ref, build_no_rules_columns(language))

        (hit, matched_keywords, matched_language, match_context), _extractor = (
            scan_pdf_bytes_for_keywords(pdf_bytes, language, rules_by_language)
        )
        return PdfJobResult(
            ref,
            build_success_columns(
                hit=hit,
                matched_keywords=matched_keywords,
                matched_language=matched_language,
                match_context=match_context,
            ),
        )

    except requests.RequestException as exc:
        logging.error("Download failed for %s (%s): %s", ref, link, exc)
        return PdfJobResult(ref, build_error_columns("download", str(exc)))
    except Exception as exc:
        logging.error("Processing failed for %s (%s): %s", ref, link, exc)
        return PdfJobResult(ref, build_error_columns("processing", str(exc)))
    finally:
        if request_delay > 0:
            time.sleep(request_delay)


_worker_rules: KeywordRules | None = None
_worker_request_delay: float = 0.0


def init_pdf_worker(rules_by_language: KeywordRules, request_delay: float) -> None:
    """Initializer for process-pool workers."""
    global _worker_rules, _worker_request_delay
    _worker_rules = rules_by_language
    _worker_request_delay = request_delay


def process_pdf_job_packed(job_dict: dict[str, Any]) -> PdfJobResult:
    """Process one picklable job dict in a worker process."""
    if _worker_rules is None:
        raise RuntimeError("PDF worker not initialized")
    job = PdfJob(**job_dict)
    return process_pdf_job(job, _worker_rules, _worker_request_delay)


def apply_pdf_result(row: dict[str, str], result: PdfJobResult) -> None:
    """Merge PDF result columns into an attachment row."""
    row.update(result.columns)
