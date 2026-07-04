"""PDF download + text extraction for the analyzer's extracted-text mode.

When the `SEND_EXTRACTED_TEXT` toggle (Settings page) is ON, the analyzer
calls `download_pdf` + `extract_text_for_llm` and feeds the result into
the DeepSeek prompt instead of the metadata-only fallback. When the
toggle is OFF — the default — none of this module runs and the legacy
URL/metadata path is untouched.

Design constraints (speed-news strategy — the spike decays in seconds):
  - The whole download+extract step must fit inside a few seconds, so
    the download has its own short timeout and extraction is a pure
    sync function the service runs in an executor.
  - Filings routinely duplicate every paragraph in English and Hindi;
    the Devanagari filter drops the Hindi half so it never burns prompt
    tokens.
  - 200-page filings are a *selection* problem, not an extraction
    problem: PyMuPDF reads them in well under a second, then we keep the
    covering letter (first pages) plus the highest-scoring content pages
    up to a character budget.
  - Scanned PDFs (no text layer) are reported as `reason="scanned_pdf"`
    — OCR is far too slow for the hot path, so the caller falls back to
    the metadata prompt.

Every failure mode returns an `ExtractionResult` with `ok=False` and a
machine-readable `reason` — this module never raises into the analyzer.
PyMuPDF (`fitz`) is imported lazily so environments without it (or a
broken wheel) degrade to the legacy path instead of killing the app.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.logging_config import get_logger

log = get_logger(__name__)


# Browser-like headers — NSE/BSE attachment CDNs reject bare clients.
# Same UA the NSE monitor uses for its XHR fetches.
PDF_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Hard cap on the download size — an attachment bigger than this is
# almost certainly a scanned annual report we couldn't use in time anyway.
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB

# A page whose extracted text is shorter than this (after whitespace
# normalisation) counts as "empty" for the scanned-PDF heuristic.
_MIN_CHARS_PER_TEXT_PAGE = 40

# Devanagari block (Hindi) — used to drop the duplicated Hindi half.
_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")

# Content keywords used to score pages beyond the covering letter.
# Deliberately broad: these mark "pages where the tradeable facts live"
# (numbers, deal terms, results lines), not any specific event type.
_PAGE_SCORE_KEYWORDS: tuple[str, ...] = (
    # deal / order terms
    "order", "contract", "awarded", "bagged", "loi", "letter of intent",
    "agreement", "acquisition", "acquire", "stake", "merger", "demerger",
    "buyback", "buy-back", "open offer", "preferential",
    # money / scale
    "crore", "lakh", "million", "billion", "consideration", "value of",
    "₹", "rs.", "inr",
    # results lines
    "revenue", "total income", "net profit", "profit after tax", "pat",
    "ebitda", "margin", "eps", "dividend", "quarter ended", "year ended",
    # governance / people
    "resign", "appoint", "cessation", "ceo", "cfo", "managing director",
    # outlook
    "guidance", "outlook", "capacity", "expansion", "commissioning",
)


@dataclass
class ExtractionResult:
    """Outcome of one extraction attempt. `ok=False` → caller falls back
    to the legacy metadata-only prompt; `reason` says why (queryable)."""

    ok: bool
    reason: str = ""                 # "" on success; machine tag on failure
    text: str = ""                   # the LLM-ready extracted text
    pages_total: int = 0
    pages_used: list[int] = field(default_factory=list)  # 1-based page numbers
    chars: int = 0
    truncated: bool = False          # True when the char budget cut pages
    hindi_lines_dropped: int = 0

    def meta(self) -> dict[str, Any]:
        """Compact dict for logs / the analysis raw_response."""
        return {
            "ok": self.ok,
            "reason": self.reason or None,
            "pages_total": self.pages_total,
            "pages_used": self.pages_used,
            "chars": self.chars,
            "truncated": self.truncated,
            "hindi_lines_dropped": self.hindi_lines_dropped,
        }


# -- Download -------------------------------------------------------------


async def download_pdf(
    url: str,
    *,
    timeout_s: float = 6.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Optional[bytes]:
    """Fetch the filing PDF. Returns None on any failure — the caller
    falls back to the metadata prompt, it never blocks the pipeline.

    One attempt, short timeout, no retries: by the time a retry ladder
    finishes, the news spike is gone (same rationale as the LLM cap).
    `transport` lets tests inject an httpx.MockTransport.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s,
            follow_redirects=True,
            headers=PDF_REQUEST_HEADERS,
            transport=transport,
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                log.warning(
                    "pdf_extract.download_bad_status",
                    url=url,
                    status_code=resp.status_code,
                )
                return None
            body = resp.content
            if not body:
                return None
            if len(body) > MAX_PDF_BYTES:
                log.warning(
                    "pdf_extract.download_too_large", url=url, bytes=len(body)
                )
                return None
            return body
    except Exception as e:  # noqa: BLE001 — any transport error → fallback
        log.warning("pdf_extract.download_failed", url=url, error=str(e))
        return None


# -- Hindi (Devanagari) filter ---------------------------------------------


def strip_hindi_lines(text: str) -> tuple[str, int]:
    """Drop lines that are predominantly Devanagari.

    NSE/BSE filings often carry the same content twice — English and
    Hindi. A line is dropped when Devanagari characters outnumber ASCII
    letters, which removes the Hindi half while keeping English lines
    that merely mention a Hindi name. Returns (filtered_text, dropped).
    """
    kept: list[str] = []
    dropped = 0
    for line in text.splitlines():
        deva = len(_DEVANAGARI_RE.findall(line))
        if deva == 0:
            kept.append(line)
            continue
        latin = sum(1 for ch in line if ch.isascii() and ch.isalpha())
        if deva > latin:
            dropped += 1
        else:
            kept.append(line)
    return "\n".join(kept), dropped


# -- Page scoring / selection ----------------------------------------------


def _score_page(text: str) -> int:
    """Cheap relevance score: keyword hits + a bonus for digit density
    (tables of numbers are where the tradeable facts live)."""
    lowered = text.lower()
    score = sum(1 for kw in _PAGE_SCORE_KEYWORDS if kw in lowered)
    digits = sum(1 for ch in text if ch.isdigit())
    if digits > 50:
        score += 2
    elif digits > 15:
        score += 1
    return score


def _normalise(text: str) -> str:
    """Collapse the whitespace noise PDF extraction produces."""
    # Collapse runs of blank lines and trailing spaces; keep line
    # structure (tables read better line-by-line than as one blob).
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


# -- Extraction (sync — run in an executor) ---------------------------------


def extract_text_for_llm(
    pdf_bytes: bytes,
    *,
    max_chars: int = 24_000,
    always_keep_pages: int = 2,
) -> ExtractionResult:
    """Turn a filing PDF into LLM-ready text within a character budget.

    Selection strategy:
      1. Extract every page's text (PyMuPDF — sub-second even at 200
         pages), dropping Hindi-dominant lines per page.
      2. Always keep the first `always_keep_pages` pages — the covering
         letter carries the announcement's substance in most filings.
      3. Rank the remaining pages by keyword/digit score and add the
         best ones, re-sorted into document order, until `max_chars`.

    Pure sync + CPU-bound: callers on the event loop must run this in
    an executor.
    """
    try:
        import fitz  # PyMuPDF — lazy so a missing wheel degrades cleanly
    except Exception:  # noqa: BLE001
        log.warning("pdf_extract.pymupdf_missing")
        return ExtractionResult(ok=False, reason="pymupdf_not_installed")

    if not pdf_bytes:
        return ExtractionResult(ok=False, reason="empty_pdf")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        log.warning("pdf_extract.open_failed", error=str(e))
        return ExtractionResult(ok=False, reason="pdf_open_failed")

    try:
        if doc.needs_pass:
            # Try the empty password (some filings are "encrypted" with
            # no user password); otherwise give up.
            if not doc.authenticate(""):
                return ExtractionResult(
                    ok=False, reason="pdf_encrypted", pages_total=doc.page_count
                )

        pages_total = doc.page_count
        if pages_total == 0:
            return ExtractionResult(ok=False, reason="empty_pdf")

        page_texts: list[str] = []
        hindi_dropped_total = 0
        text_pages = 0
        for page in doc:
            try:
                raw = page.get_text("text") or ""
            except Exception:  # noqa: BLE001 — one broken page is not fatal
                raw = ""
            filtered, dropped = strip_hindi_lines(raw)
            filtered = _normalise(filtered)
            hindi_dropped_total += dropped
            if len(filtered) >= _MIN_CHARS_PER_TEXT_PAGE:
                text_pages += 1
            page_texts.append(filtered)
    finally:
        doc.close()

    # Scanned-PDF heuristic: almost no page has a usable text layer.
    if text_pages == 0 or (pages_total >= 4 and text_pages / pages_total < 0.1):
        return ExtractionResult(
            ok=False,
            reason="scanned_pdf",
            pages_total=pages_total,
            hindi_lines_dropped=hindi_dropped_total,
        )

    # Page selection under the char budget.
    keep_first = min(always_keep_pages, pages_total)
    selected = list(range(keep_first))
    budget_used = sum(len(page_texts[i]) for i in selected)

    rest = [
        (i, _score_page(page_texts[i]))
        for i in range(keep_first, pages_total)
        if len(page_texts[i]) >= _MIN_CHARS_PER_TEXT_PAGE
    ]
    # Highest score first; ties resolve to earlier pages (stable sort).
    rest.sort(key=lambda t: -t[1])

    truncated = False
    for i, score in rest:
        if score <= 0:
            continue
        cost = len(page_texts[i])
        if budget_used + cost > max_chars:
            truncated = True
            continue
        selected.append(i)
        budget_used += cost

    selected.sort()  # back to document order for a coherent read

    parts: list[str] = []
    for i in selected:
        if not page_texts[i]:
            continue
        parts.append(f"--- Page {i + 1} of {pages_total} ---\n{page_texts[i]}")
    text = "\n\n".join(parts)

    # Final hard clamp — first pages alone can exceed the budget.
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    if not text.strip():
        return ExtractionResult(
            ok=False,
            reason="no_text_extracted",
            pages_total=pages_total,
            hindi_lines_dropped=hindi_dropped_total,
        )

    return ExtractionResult(
        ok=True,
        text=text,
        pages_total=pages_total,
        pages_used=[i + 1 for i in selected],
        chars=len(text),
        truncated=truncated,
        hindi_lines_dropped=hindi_dropped_total,
    )
