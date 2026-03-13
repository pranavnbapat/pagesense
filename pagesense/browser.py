from __future__ import annotations

import atexit
import re
import threading
from io import BytesIO
from typing import Callable

from pypdf import PdfReader, filters
from playwright.sync_api import sync_playwright

filters.ZLIB_MAX_OUTPUT_LENGTH = 0


_thread_state = threading.local()
_cleanup_registered = False
_cleanup_lock = threading.Lock()
_playwright_instances: list[tuple[object, object]] = []


def _get_browser():
    global _cleanup_registered

    browser = getattr(_thread_state, "browser", None)
    if browser is None:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        _thread_state.playwright = playwright
        _thread_state.browser = browser
        with _cleanup_lock:
            _playwright_instances.append((playwright, browser))

        with _cleanup_lock:
            if not _cleanup_registered:
                def _close_all():
                    with _cleanup_lock:
                        instances = list(_playwright_instances)
                        _playwright_instances.clear()
                    for playwright_obj, browser_obj in instances:
                        try:
                            browser_obj.close()
                        finally:
                            playwright_obj.stop()

                atexit.register(_close_all)
                _cleanup_registered = True

    return browser


def extract_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    reader = PdfReader(BytesIO(pdf_bytes))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("Encrypted PDF - cannot extract text without password.") from exc

    pages_text: list[str] = []
    for page in reader.pages:
        txt = (page.extract_text() or "").strip()
        if txt:
            pages_text.append(txt)

    if not pages_text:
        raise ValueError("No extractable text - likely scanned (image-only) PDF.")

    text = "\n\n".join(pages_text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.replace("\x00", "").strip()


def fetch_with_browser(
    url: str,
    allow_url: Callable[[str], bool] | None = None,
    timeout_ms: int = 45_000,
) -> tuple[str, str]:
    browser = _get_browser()
    context = browser.new_context()
    page = context.new_page()
    try:
        if allow_url is not None:
            def _route_handler(route):
                if allow_url(route.request.url):
                    route.continue_()
                else:
                    route.abort("blockedbyclient")

            page.route("**/*", _route_handler)

        page.goto(url, timeout=timeout_ms, wait_until="networkidle")
        resolved = page.url
        if allow_url is not None and not allow_url(resolved):
            raise ValueError("Browser navigation resolved to a blocked address.")
        return resolved, page.content()
    finally:
        page.close()
        context.close()
