# utils.py


import atexit
import re

from io import BytesIO
from typing import Optional, Tuple

from pypdf import PdfReader, filters
from playwright.sync_api import sync_playwright

filters.ZLIB_MAX_OUTPUT_LENGTH = 0


_pw = None
_browser = None

def _get_browser():
    """
    Create one Playwright browser per Gunicorn worker process and reuse it.
    Launching Chromium per request is extremely slow.
    """
    global _pw, _browser
    if _browser is None:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)

        # Clean shutdown when the worker exits
        def _close():
            global _pw, _browser
            try:
                if _browser:
                    _browser.close()
            finally:
                _browser = None
                if _pw:
                    _pw.stop()
                _pw = None

        atexit.register(_close)

    return _browser

def extract_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    """
    Extracts text from a PDF given as raw bytes.
    Returns a single string with pages separated by blank lines.
    """
    reader = PdfReader(BytesIO(pdf_bytes))
    if reader.is_encrypted:
        try:
            reader.decrypt("")  # try empty password
        except Exception:
            raise ValueError("Encrypted PDF - cannot extract text without password.")

    pages_text: list[str] = []
    for page in reader.pages:
        # extract_text() can be None on image-only pages
        txt = page.extract_text() or ""
        txt = txt.strip()
        if txt:
            pages_text.append(txt)

    if not pages_text:
        raise ValueError("No extractable text - likely scanned (image-only) PDF.")

    text = "\n\n".join(pages_text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.replace("\x00", "").strip()

def fetch_with_browser(url: str) -> tuple[str, str]:
    browser = _get_browser()
    page = browser.new_page()
    try:
        page.goto(url, timeout=120_000, wait_until="networkidle")
        resolved = page.url
        html = page.content()
        return resolved, html
    finally:
        page.close()

