"""Tests for the extracted-text pipeline (app/analyzer/pdf_extract.py).

Synthetic PDFs are built with PyMuPDF itself, so the tests skip cleanly
in an environment without the wheel (the runtime does the same — the
analyzer falls back to the legacy URL/metadata path).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.analyzer.pdf_extract import (
    ExtractionResult,
    download_pdf,
    extract_text_for_llm,
    strip_hindi_lines,
)

fitz = pytest.importorskip("fitz", reason="PyMuPDF not installed")


HINDI_LINE = "कंपनी ने तिमाही परिणाम घोषित किए और लाभांश की घोषणा की"
ENGLISH_LINE = "The company declared quarterly results and announced a dividend."


def _make_pdf(pages: list[str]) -> bytes:
    """Build an in-memory PDF, one text block per page ('' = blank page).

    Text is inserted line-by-line: PyMuPDF's insert_text does not wrap,
    so a single long line would be clipped at the page edge and never
    reach the extractor. Lines past the page bottom are dropped.
    """
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            y = 60.0
            for line in text.splitlines():
                if y > page.rect.height - 60:
                    break
                page.insert_text((50, y), line, fontsize=10)
                y += 13.0
    data = doc.tobytes()
    doc.close()
    return data


def _lines(sentence: str, n: int) -> str:
    return "\n".join(sentence for _ in range(n))


# -- strip_hindi_lines -----------------------------------------------------


def test_strip_hindi_lines_drops_devanagari_dominant_lines() -> None:
    text = f"{ENGLISH_LINE}\n{HINDI_LINE}\n{ENGLISH_LINE}"
    filtered, dropped = strip_hindi_lines(text)
    assert dropped == 1
    assert HINDI_LINE not in filtered
    assert filtered.count(ENGLISH_LINE) == 2


def test_strip_hindi_lines_keeps_english_line_with_hindi_name() -> None:
    # An English sentence that merely contains a Devanagari word stays.
    line = "The subsidiary भारत Industries Limited won a new export order today."
    filtered, dropped = strip_hindi_lines(line)
    assert dropped == 0
    assert "Industries Limited" in filtered


# -- extract_text_for_llm --------------------------------------------------


def test_extract_basic_english_pdf() -> None:
    pdf = _make_pdf([f"Covering letter. {ENGLISH_LINE}", "Annexure with details."])
    res = extract_text_for_llm(pdf)
    assert res.ok, res.reason
    assert res.pages_total == 2
    assert 1 in res.pages_used  # covering letter always kept
    assert "quarterly results" in res.text
    assert not res.truncated


def test_extract_scanned_pdf_falls_back() -> None:
    # Pages with no text layer at all → scanned_pdf, ok=False.
    pdf = _make_pdf(["", "", "", ""])
    res = extract_text_for_llm(pdf)
    assert not res.ok
    assert res.reason == "scanned_pdf"


def test_extract_prefers_keyword_pages_under_budget() -> None:
    filler = _lines("General corporate boilerplate without relevant content.", 30)
    money_page = _lines(
        "The Board approved an order worth Rs. 450 crore from a new "
        "customer. Revenue impact and net profit guidance discussed.",
        20,
    )
    # Pages: 2 covering pages, then filler pages, then the money page.
    pdf = _make_pdf(["Covering letter page one.", "Covering page two."]
                    + [filler] * 5 + [money_page])
    res = extract_text_for_llm(pdf, max_chars=6_000)
    assert res.ok
    # The keyword-heavy page must be selected; page numbers are 1-based.
    assert res.pages_total == 8
    assert 8 in res.pages_used
    assert "450 crore" in res.text


def test_extract_respects_char_budget() -> None:
    big_page = _lines(
        "Order worth Rs 100 crore awarded; revenue and profit up.", 50
    )
    pdf = _make_pdf([big_page, big_page, big_page])
    res = extract_text_for_llm(pdf, max_chars=3_000)
    assert res.ok
    assert res.chars <= 3_000
    assert res.truncated


def test_extract_garbage_bytes_fail_clean() -> None:
    res = extract_text_for_llm(b"this is not a pdf at all")
    assert not res.ok
    assert res.reason in {"pdf_open_failed", "scanned_pdf", "no_text_extracted"}


def test_extract_empty_bytes() -> None:
    res = extract_text_for_llm(b"")
    assert not res.ok
    assert res.reason == "empty_pdf"


# -- download_pdf ----------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_download_pdf_success() -> None:
    payload = b"%PDF-1.4 fake body"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    got = _run(
        download_pdf(
            "https://example.com/filing.pdf",
            transport=httpx.MockTransport(handler),
        )
    )
    assert got == payload


def test_download_pdf_non_200_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    got = _run(
        download_pdf(
            "https://example.com/filing.pdf",
            transport=httpx.MockTransport(handler),
        )
    )
    assert got is None


def test_download_pdf_bad_url_returns_none() -> None:
    assert _run(download_pdf("")) is None
    assert _run(download_pdf("ftp://example.com/x.pdf")) is None
