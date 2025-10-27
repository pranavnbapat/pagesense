# app.py

from __future__ import annotations

import ipaddress
import re

import requests

from pathlib import Path

from bs4 import BeautifulSoup, Comment
from flask import Flask, render_template, request, jsonify
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

try:
    from flask_cors import CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})  # relax
except Exception:
    pass  # API still works without CORS if same-origin


# --- Basic safety knobs (helpful even for local use) ---
MAX_DOWNLOAD_BYTES = 10_000_000  # ~10 MB cap to avoid huge pages
REQUEST_TIMEOUT = (10, 60)       # (connect, read) seconds
ALLOWED_SCHEMES = {"http", "https"}
DESKTOP_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 12; Pixel 5) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
BROWSERY_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    # 'Accept-Encoding' is added by requests automatically; no need to set.
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

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


def extract_text_from_url(raw_url: str) -> str:
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

    def attempt_fetch(user_agent: str):
        headers = {
            "User-Agent": user_agent,
            **BROWSERY_HEADERS,
            # Some sites expect a same-origin Referer; others accept a generic one.
            "Referer": f"{parsed.scheme}://{parsed.netloc}/",
        }
        # 1) Optional warm-up hit to set cookies (ignore errors)
        try:
            session.get(f"{parsed.scheme}://{parsed.netloc}/",
                        headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except Exception:
            pass

        # 2) Real fetch, streamed + size cap
        with session.get(
                raw_url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                stream=True,
                allow_redirects=True,
        ) as resp:
            # If origin blocks us, this will raise on 403/401/451 etc.
            resp.raise_for_status()
            # Sanity: only proceed for HTML-ish content
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" not in ctype and "application/xhtml+xml" not in ctype:
                raise ValueError(f"Unsupported Content-Type: {ctype or 'unknown'}")

            resp.encoding = resp.apparent_encoding or resp.encoding
            content_chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=16384):
                if chunk:
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Page too large (over 2 MB).")
                    content_chunks.append(chunk)
            return b"".join(content_chunks), (resp.encoding or "utf-8")

    # Try desktop first, then mobile if 403/401
    try:
        html_bytes, enc = attempt_fetch(DESKTOP_UA)
    except requests.exceptions.HTTPError as ex:
        sc = getattr(ex.response, "status_code", None)
        if sc in (401, 403, 451, 429):
            raise ValueError(
                f"Site refused access (HTTP {sc}). "
                "They may require a browser, login, or disallow automated fetches."
            )
        else:
            raise

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

    text = body.get_text(separator="\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


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
                text_result = extract_text_from_url(raw_url)
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
        text = extract_text_from_url(url)
        return jsonify({"ok": True, "url": url, "text": text}), 200
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
