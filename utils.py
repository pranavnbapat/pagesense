# utils.py


import re

from io import BytesIO

from pypdf import PdfReader, filters
from playwright.sync_api import sync_playwright

filters.ZLIB_MAX_OUTPUT_LENGTH = 0


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
            raise ValueError("Encrypted PDF — cannot extract text without password.")

    pages_text: list[str] = []
    for page in reader.pages:
        # extract_text() can be None on image-only pages
        txt = page.extract_text() or ""
        txt = txt.strip()
        if txt:
            pages_text.append(txt)

    if not pages_text:
        raise ValueError("No extractable text — likely scanned (image-only) PDF.")

    text = "\n\n".join(pages_text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.replace("\x00", "").strip()

def fetch_with_browser(url: str) -> tuple[str, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=120_000, wait_until="networkidle")
        resolved = page.url
        html = page.content()
        browser.close()
        return resolved, html
