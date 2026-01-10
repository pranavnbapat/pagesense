# app.py

from __future__ import annotations

import ipaddress
import random
import re
import time

import requests

from pathlib import Path

from bs4 import BeautifulSoup, Comment
from flask import Flask, render_template, request, jsonify
from urllib.parse import urlparse

from utils import extract_pdf_text_from_bytes, fetch_with_browser


BASE_DIR = Path(__file__).resolve().parent

LAST_DOMAIN_CALL: dict[str, float] = {}

POLITE_MODE = False  # set True for throttling delays

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

try:
    from flask_cors import CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})  # relax
except Exception:
    pass  # API still works without CORS if same-origin


# --- Basic safety knobs (helpful even for local use) ---
MAX_DOWNLOAD_BYTES = 500_000_000  # ~500 MB cap to avoid huge pages
REQUEST_TIMEOUT = (30, 720)       # (connect, read) seconds
ALLOWED_SCHEMES = {"http", "https"}
DESKTOP_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 12; Pixel 5) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
BROWSERY_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Accept-Encoding": "identity",
}

UA_POOL = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
]

# RFC1918 & friends to reduce SSRF risk (block localhost/intranet)
PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

def is_private_host(hostname: str) -> bool:
    """
    Resolve obvious IP-literals and block private ranges.
    (This is deliberately simple; full DNS resolution is omitted for brevity.)
    """
    try:
        # If hostname is an IPv4/IPv6 literal, check it directly.
        ip = ipaddress.ip_address(hostname)
        return any(ip in net for net in PRIVATE_NETS)
    except ValueError:
        # Not an IP literal; allow (DNS resolution not enforced).
        return False


def extract_text_from_url(raw_url: str) -> tuple[str, str]:
    """
    Fetch URL and return cleaned text. Raises ValueError on validation issues.
    Propagates requests exceptions for network/HTTP problems.
    """
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        raise ValueError("Please enter a valid http(s) URL (e.g., https://example.com/page).")
    if is_private_host(parsed.hostname):
        raise ValueError("Private/loopback addresses are not allowed.")

    session = requests.Session()

    from requests.adapters import HTTPAdapter
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({"Accept-Encoding": "identity"})

    try:
        head_resp = session.head(
            raw_url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        head_resp.raise_for_status()
        ctype_head = (head_resp.headers.get("Content-Type") or "").lower()
    except Exception:
        # Some servers don't like HEAD; fall back to treating as unknown
        ctype_head = ""

    path_lower = parsed.path.lower()
    is_pdf_head = False

    if "application/pdf" in ctype_head:
        is_pdf_head = True
    elif "application/octet-stream" in ctype_head and path_lower.endswith(".pdf"):
        # Many servers send PDFs as generic octet-stream
        is_pdf_head = True

    if is_pdf_head:
        # Download the PDF and extract text directly
        with session.get(
                raw_url,
                timeout=REQUEST_TIMEOUT,
                stream=True,
                headers={
                    "User-Agent": random.choice(UA_POOL),
                    **BROWSERY_HEADERS,
                },
                allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            content_chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("File too large (over 500 MB).")
                content_chunks.append(chunk)
        pdf_bytes = b"".join(content_chunks)
        pdf_text = extract_pdf_text_from_bytes(pdf_bytes)
        if not pdf_text.strip():
            raise ValueError("Could not extract text from PDF (possibly scanned/image-only).")
        return resp.url, pdf_text

    def attempt_fetch(user_agent: str):
        domain = parsed.netloc.lower()

        # --- polite per-domain throttling ---
        if POLITE_MODE:
            now = time.monotonic()
            last = LAST_DOMAIN_CALL.get(domain)
            if last is not None:
                required = random.uniform(1.2, 3.5)
                elapsed = now - last
                if elapsed < required:
                    time.sleep(required - elapsed)

        headers = {
            "User-Agent": random.choice(UA_POOL),
            **BROWSERY_HEADERS,
            # Some sites expect a same-origin Referer; others accept a generic one.
            "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        }

        # 1) Optional warm-up hit to set cookies (ignore errors)
        if POLITE_MODE:
            try:
                session.get(f"{parsed.scheme}://{parsed.netloc}/", headers=headers, timeout=REQUEST_TIMEOUT,
                            allow_redirects=True)
            except Exception:
                pass

        # 2) Real fetch, streamed + size cap
        # human-like browsing delay (only in polite mode)
        if POLITE_MODE:
            time.sleep(random.uniform(0.6, 1.8))

        with session.get(raw_url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True,) as resp:
            resp.raw.decode_content = False

            # record this request time only AFTER real network hit
            LAST_DOMAIN_CALL[domain] = time.monotonic()

            final_url = resp.url
            final_host = urlparse(final_url).netloc.lower()

            if final_host != parsed.netloc.lower():
                headers["Referer"] = f"https://{final_host}/"

            # If origin blocks us, this will raise on 403/401/451 etc.
            resp.raise_for_status()
            # Sanity: only proceed for HTML-ish content
            ctype = (resp.headers.get("Content-Type") or "").lower()
            final_path = urlparse(final_url).path.lower()
            is_pdf = False
            if "application/pdf" in ctype:
                is_pdf = True
            elif "application/octet-stream" in ctype and final_path.endswith(".pdf"):
                # Classic: servers send PDFs as generic octet-stream
                is_pdf = True

            if is_pdf:
                content_chunks, total = [], 0
                for chunk in resp.raw.stream(16384, decode_content=False):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("File too large (over 500 MB).")
                    content_chunks.append(chunk)
                pdf_bytes = b"".join(content_chunks)
                pdf_text = extract_pdf_text_from_bytes(pdf_bytes)
                if not pdf_text.strip():
                    raise ValueError("Could not extract text from PDF (possibly scanned/image-only).")
                # Return as if it were "HTML" so the outer code can reuse its path
                return pdf_text.encode("utf-8"), "utf-8", final_url

            # ---- existing HTML-only check ----
            is_htmlish = (
                    "text/html" in ctype
                    or "application/xhtml+xml" in ctype
                    or "application/octet-stream" in ctype  # many sites mislabel HTML
            )

            if not is_htmlish:
                # Truly unsupported binary: zip, images, etc.
                raise ValueError(f"Unsupported Content-Type: {ctype or 'unknown'}")

            resp.encoding = resp.apparent_encoding or resp.encoding
            content_chunks, total = [], 0
            resp.raw.decode_content = False
            for chunk in resp.raw.stream(16384, decode_content=False):
                if chunk:
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Page too large (over 500 MB).")
                    content_chunks.append(chunk)
            return b"".join(content_chunks), (resp.encoding or "utf-8"), final_url

    # Try desktop fetch first; fall back to real browser if streaming/decompression explodes
    try:
        result = attempt_fetch(DESKTOP_UA)
        html_bytes, enc, resolved_url = result
    except requests.exceptions.HTTPError as ex:
        sc = getattr(ex.response, "status_code", None)
        if sc in (401, 403, 451, 429):
            # clear "access refused" message for the API / UI
            raise ValueError(
                f"Site refused access (HTTP {sc}). "
                "They may require a browser, login, or disallow automated fetches."
            )
        # other HTTP errors: try a headless browser before giving up
        resolved_url, html = fetch_with_browser(raw_url)
        enc = "utf-8"
        html_bytes = html.encode(enc, errors="replace")
    except Exception as ex:
        # Includes "Limit reached while decompressing..." and other low-level issues
        resolved_url, html = fetch_with_browser(raw_url)
        enc = "utf-8"
        html_bytes = html.encode(enc, errors="replace")

    html = html_bytes.decode(enc, errors="replace")
    soup = BeautifulSoup(html, "lxml")
    body = soup.body if soup.body else soup

    # strip comments
    for node in body.find_all(string=lambda t: isinstance(t, Comment)):
        node.extract()
    # drop junk tags
    for tag in body.find_all([
        "script","style","noscript","template","iframe","frame","frameset","object",
        "embed","canvas","svg","video","audio","picture","source","figure","figcaption",
        "form","button","input","select","textarea","label","nav","header","footer",
        "aside","menu","dialog"
    ]):
        tag.decompose()
    # ARIA landmarks
    for el in list(body.select("[role=banner],[role=navigation],[role=complementary],[role=contentinfo],[role=search],[role=dialog],[role=alert],[role=alertdialog]")):
        el.decompose()
    # hidden elements
    for el in list(body.select("[hidden], [style*='display:none'], [style*='visibility:hidden']")):
        el.decompose()
    # id/class substrings
    substrings = ["cookie","consent","gdpr","subscribe","signup","newsletter","modal","overlay",
                  "paywall","meter","gate","promo","breadcrumb","share","social","toolbar",
                  "footer","header","nav","sidebar"]
    selector = ",".join([f'[id*="{s}" i],[class*="{s}" i]' for s in substrings])
    for el in body.select(selector):
        el.decompose()

    # Extract visible text from the cleaned DOM
    text = body.get_text("\n", True)  # type: ignore[arg-type]
    clean_text = re.sub(r"\n{3,}", "\n\n", text)

    # If we got almost nothing, try a real browser render via Playwright
    if len(clean_text) < 500:
        try:
            # Use the final resolved URL if we have it, otherwise fall back to the original
            browser_url = resolved_url or parsed.geturl()
            resolved_url, html = fetch_with_browser(browser_url)

            # Re-parse the fully rendered HTML
            soup = BeautifulSoup(html, "lxml")
            body = soup.body if soup.body else soup

            text = body.get_text("\n", True)    # type: ignore[arg-type]
            clean_text = re.sub(r"\n{3,}", "\n\n", text)

        except Exception as browser_ex:
            # Optional: log it, but keep whatever text we already had
            print(f"[browser fallback failed] {browser_ex}", flush=True)

    return resolved_url, clean_text


@app.route("/", methods=["GET", "POST"])
def index():
    text_result = None
    error = None

    if request.method == "POST":
        raw_url = (request.form.get("url") or "").strip()

        # 1) Basic URL validation
        parsed = urlparse(raw_url)
        if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
            error = "Please enter a valid http(s) URL (e.g., https://example.com/page)."
        elif is_private_host(parsed.hostname):
            error = "Private/loopback addresses are not allowed."
        else:
            try:
                _, text_result = extract_text_from_url(raw_url)
            except requests.exceptions.RequestException as ex:
                error = f"Network/HTTP error: {ex}"
            except Exception as ex:
                error = f"Failed to extract text: {ex}"

    return render_template("index.html", text_result=text_result, error=error)


@app.route("/api/extract", methods=["GET", "POST"])
def api_extract():
    """
    Accepts:
      - GET  /api/extract?url=...
      - POST /api/extract  {"url": "..."}
    Returns JSON: { ok, url, text?, error? }
    """
    url = request.args.get("url") if request.method == "GET" else (request.json or {}).get("url")
    if not url:
        return jsonify({"ok": False, "error": "Missing 'url' parameter"}), 400

    try:
        resolved_url, text = extract_text_from_url(url)
        return jsonify({"ok": True, "url": url, "resolved_url": resolved_url, "text": text}), 200
    except ValueError as ve:
        # input/validation issues (bad scheme, private ip, too large, etc.)
        return jsonify({"ok": False, "url": url, "error": str(ve)}), 422
    except requests.exceptions.Timeout:
        return jsonify({"ok": False, "url": url, "error": "Upstream timeout"}), 504
    except requests.exceptions.RequestException as rexc:
        # network/HTTP errors from the source site
        return jsonify({"ok": False, "url": url, "error": f"Network/HTTP error: {rexc}"}), 502
    except Exception as ex:
        return jsonify({"ok": False, "url": url, "error": f"Failed to extract text: {ex}"}), 500


if __name__ == "__main__":
    # For local dev; in PyCharm, run this configuration.
    app.run(host="127.0.0.1", port=11000, debug=True)
