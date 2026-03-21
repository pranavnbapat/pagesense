from __future__ import annotations

import ipaddress
import logging
import random
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Comment
from flask import current_app

from pagesense.browser import extract_pdf_text_from_bytes, fetch_with_browser
from pagesense.config import AppConfig


LOGGER = logging.getLogger(__name__)
LAST_DOMAIN_CALL: dict[str, float] = {}


@dataclass(frozen=True)
class ExtractionResult:
    resolved_url: str
    text: str
    downloaded_bytes: int
    extracted_text_bytes: int


def get_config() -> AppConfig:
    return current_app.extensions["pagesense_config"]


def get_private_networks() -> list[ipaddress._BaseNetwork]:
    return [ipaddress.ip_network(net) for net in get_config().private_nets]


def _get_private_networks_for_config(config: AppConfig) -> list[ipaddress._BaseNetwork]:
    return [ipaddress.ip_network(net) for net in config.private_nets]


def _is_allowed_url_for_config(raw_url: str, config: AppConfig) -> bool:
    parsed = urlparse((raw_url or "").strip())
    return bool(
        parsed.scheme in config.allowed_schemes
        and parsed.hostname
        and not _is_private_host_for_config(parsed.hostname, config)
        and not _is_blocked_media_host_for_config(parsed.hostname, config)
    )


def _is_blocked_media_host_for_config(hostname: str | None, config: AppConfig) -> bool:
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    return any(host == pattern or host.endswith(f".{pattern}") for pattern in config.blocked_host_patterns)


def is_blocked_media_host(hostname: str | None) -> bool:
    return _is_blocked_media_host_for_config(hostname, get_config())


def _is_private_host_for_config(hostname: str | None, config: AppConfig) -> bool:
    if not hostname:
        return True

    private_nets = _get_private_networks_for_config(config)
    try:
        ip = ipaddress.ip_address(hostname)
        return any(ip in net for net in private_nets)
    except ValueError:
        pass

    try:
        addrinfo = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False

    for ip_text in {item[4][0] for item in addrinfo if item[4]}:
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if any(ip in net for net in private_nets):
            return True
    return False


def is_private_host(hostname: str | None) -> bool:
    return _is_private_host_for_config(hostname, get_config())


def is_allowed_url(raw_url: str) -> bool:
    return _is_allowed_url_for_config(raw_url, get_config())


def validate_url(raw_url: str) -> tuple[str, str]:
    config = get_config()
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme not in config.allowed_schemes or not parsed.netloc:
        raise ValueError("Please enter a valid http(s) URL (e.g., https://example.com/page).")
    if _is_blocked_media_host_for_config(parsed.hostname, config):
        raise ValueError("Video platform URLs are not supported.")
    if _is_private_host_for_config(parsed.hostname, config):
        raise ValueError("Private/loopback addresses are not allowed.")
    return parsed.geturl(), parsed.netloc.lower()


def ensure_within_deadline(started_at: float) -> None:
    if time.monotonic() - started_at > get_config().extraction_deadline_seconds:
        raise ValueError("Extraction exceeded the 30-second limit.")


def read_response_bytes(resp: requests.Response, *, byte_limit: int, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    resp.raw.decode_content = False
    for chunk in resp.raw.stream(16384, decode_content=False):
        if not chunk:
            continue
        total += len(chunk)
        if total > byte_limit:
            raise ValueError(f"{label} too large (over {byte_limit // 1_000_000} MB).")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_simple_html(url: str, config: AppConfig) -> tuple[str, str]:
    response = requests.get(
        url,
        headers={"User-Agent": random.choice(config.ua_pool), "Accept": "text/html,application/xhtml+xml"},
        timeout=config.request_timeout,
        allow_redirects=True,
    )
    response.raise_for_status()
    final_url = response.url
    final_parsed = urlparse(final_url)
    if _is_blocked_media_host_for_config(final_parsed.hostname, config):
        raise ValueError("Video platform URLs are not supported.")
    if _is_private_host_for_config(final_parsed.hostname, config):
        raise ValueError("Redirected to a private/loopback address, which is not allowed.")
    return final_url, response.text


def extract_clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    body = soup.body if soup.body else soup

    for node in body.find_all(string=lambda t: isinstance(t, Comment)):
        node.extract()

    for tag in body.find_all([
        "script", "style", "noscript", "template", "iframe", "frame", "frameset", "object",
        "embed", "canvas", "svg", "video", "audio", "picture", "source", "figure", "figcaption",
        "form", "button", "input", "select", "textarea", "label", "nav", "header", "footer",
        "aside", "menu", "dialog",
    ]):
        tag.decompose()

    for el in list(body.select(
        "[role=banner],[role=navigation],[role=complementary],[role=contentinfo],"
        "[role=search],[role=dialog],[role=alert],[role=alertdialog]"
    )):
        el.decompose()

    for el in list(body.select("[hidden], [style*='display:none'], [style*='visibility:hidden']")):
        el.decompose()

    substrings = [
        "cookie", "consent", "gdpr", "subscribe", "signup", "newsletter", "modal", "overlay",
        "paywall", "meter", "gate", "promo", "breadcrumb", "share", "social", "toolbar",
        "footer", "header", "nav", "sidebar",
    ]

    def has_noise_marker(tag) -> bool:
        tag_id = (tag.get("id") or "").lower()
        if any(term in tag_id for term in substrings):
            return True

        classes = [cls.lower() for cls in (tag.get("class") or [])]
        for cls in classes:
            if any(
                cls == term or cls.startswith(f"{term}-") or cls.startswith(f"{term}_")
                for term in substrings
            ):
                return True
        return False

    for el in [tag for tag in body.find_all(True) if has_noise_marker(tag)]:
        el.decompose()

    text = re.sub(r"\n{3,}", "\n\n", body.get_text("\n", True)).strip()
    return post_process_text(text)


def post_process_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    cleaned_lines: list[str] = []
    for line in lines:
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue

        lowered = line.lower()
        if lowered == "back":
            continue
        if re.fullmatch(r"pdf\s*\[[^\]]+\]", line, flags=re.IGNORECASE):
            continue
        if lowered in {"leaflets & guidelines", "projects", "social media", "discuss the tool", "disqus"}:
            continue
        if lowered in {
            "applicability",
            "theme",
            "languages",
            "keywords",
            "year of release",
            "country of origin",
            "issuing organisation",
            "contact",
            "number of pages",
            "average rating to the tool:",
            "number of ratings to the tool:",
            "give your rating to the tool:",
        }:
            continue
        if lowered.startswith("more about the tool on organic eprints"):
            continue

        cleaned_lines.append(line)

    result = "\n".join(cleaned_lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def should_use_browser_fallback(html: str, clean_text: str) -> bool:
    config = get_config()
    if len(clean_text) >= config.min_browser_fallback_text:
        return False
    lowered = html.lower()
    return "<script" in lowered or 'id="app"' in lowered or 'id="root"' in lowered


def extract_text_from_url(raw_url: str) -> ExtractionResult:
    config = get_config()
    normalized_url, domain = validate_url(raw_url)
    parsed = urlparse(normalized_url)
    started_at = time.monotonic()

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"Accept-Encoding": "identity"})

    def attempt_fetch(user_agent: str) -> tuple[bytes, str, str, str]:
        ensure_within_deadline(started_at)

        if config.polite_mode:
            now = time.monotonic()
            last = LAST_DOMAIN_CALL.get(domain)
            if last is not None:
                required = random.uniform(1.2, 3.5)
                elapsed = now - last
                if elapsed < required:
                    time.sleep(required - elapsed)

        headers = {
            "User-Agent": user_agent,
            **config.browsery_headers,
            "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        }

        if config.polite_mode:
            try:
                session.get(f"{parsed.scheme}://{parsed.netloc}/", headers=headers, timeout=config.request_timeout, allow_redirects=True)
            except Exception:
                pass
            time.sleep(random.uniform(0.6, 1.8))

        with session.get(normalized_url, headers=headers, timeout=config.request_timeout, stream=True, allow_redirects=True) as resp:
            ensure_within_deadline(started_at)
            LAST_DOMAIN_CALL[domain] = time.monotonic()

            final_url = resp.url
            final_parsed = urlparse(final_url)
            if _is_blocked_media_host_for_config(final_parsed.hostname, config):
                raise ValueError("Video platform URLs are not supported.")
            if _is_private_host_for_config(final_parsed.hostname, config):
                raise ValueError("Redirected to a private/loopback address, which is not allowed.")

            resp.raise_for_status()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            final_path = final_parsed.path.lower()
            is_pdf = "application/pdf" in ctype or ("application/octet-stream" in ctype and final_path.endswith(".pdf"))

            if is_pdf:
                pdf_bytes = read_response_bytes(resp, byte_limit=config.max_pdf_bytes, label="File")
                pdf_text = extract_pdf_text_from_bytes(pdf_bytes)
                if not pdf_text.strip():
                    raise ValueError("Could not extract text from PDF (possibly scanned/image-only).")
                return pdf_text.encode("utf-8"), "utf-8", final_url, "pdf"

            is_htmlish = (
                "text/html" in ctype
                or "application/xhtml+xml" in ctype
                or "application/octet-stream" in ctype
            )
            if not is_htmlish:
                raise ValueError(f"Unsupported Content-Type: {ctype or 'unknown'}")

            resp.encoding = resp.apparent_encoding or resp.encoding
            html_bytes = read_response_bytes(resp, byte_limit=config.max_html_bytes, label="Page")
            return html_bytes, (resp.encoding or "utf-8"), final_url, "html"

    needs_browser_fetch = False
    browser_fallback_reason: Exception | None = None
    try:
        html_bytes, enc, resolved_url, content_kind = attempt_fetch(random.choice(config.ua_pool))
    except requests.exceptions.HTTPError as exc:
        sc = getattr(exc.response, "status_code", None)
        if sc in (401, 403, 451, 429):
            raise ValueError(
                f"Site refused access (HTTP {sc}). They may require a browser, login, or disallow automated fetches."
            ) from exc
        raise
    except (
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
        requests.exceptions.ConnectionError,
    ) as exc:
        needs_browser_fetch = True
        browser_fallback_reason = exc
        resolved_url = normalized_url
        content_kind = "html"
        html_bytes = b""
        enc = "utf-8"

    if content_kind == "pdf":
        ensure_within_deadline(started_at)
        text = html_bytes.decode(enc, errors="replace")
        return ExtractionResult(
            resolved_url=resolved_url,
            text=text,
            downloaded_bytes=len(html_bytes),
            extracted_text_bytes=len(text.encode("utf-8")),
        )

    html = html_bytes.decode(enc, errors="replace")
    clean_text = extract_clean_text(html) if html else ""

    if not clean_text:
        try:
            ensure_within_deadline(started_at)
            simple_resolved_url, simple_html = fetch_simple_html(normalized_url, config)
            simple_text = extract_clean_text(simple_html)
            if simple_text:
                resolved_url = simple_resolved_url
                clean_text = simple_text
                html = simple_html
        except Exception as exc:
            LOGGER.warning("simple html fallback failed: %s", exc)

    if needs_browser_fetch or should_use_browser_fallback(html, clean_text):
        try:
            ensure_within_deadline(started_at)
            remaining_ms = max(1_000, int((config.extraction_deadline_seconds - (time.monotonic() - started_at)) * 1000))
            resolved_url, html = fetch_with_browser(
                resolved_url or normalized_url,
                allow_url=lambda url: _is_allowed_url_for_config(url, config),
                timeout_ms=min(config.playwright_timeout_ms, remaining_ms),
            )
            ensure_within_deadline(started_at)
            clean_text = extract_clean_text(html)
        except Exception as exc:
            LOGGER.warning("browser fallback failed: %s", exc)
            if not clean_text:
                if browser_fallback_reason is not None:
                    raise ValueError(f"Failed to fetch page with browser fallback: {exc}") from browser_fallback_reason
                raise

    ensure_within_deadline(started_at)
    return ExtractionResult(
        resolved_url=resolved_url,
        text=clean_text,
        downloaded_bytes=len(html.encode("utf-8")),
        extracted_text_bytes=len(clean_text.encode("utf-8")),
    )
