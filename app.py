# app.py

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request
from bs4 import BeautifulSoup, Comment

from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

# --- Basic safety knobs (helpful even for local use) ---
MAX_DOWNLOAD_BYTES = 2_000_000  # ~2 MB cap to avoid huge pages
REQUEST_TIMEOUT = (5, 15)       # (connect, read) seconds
ALLOWED_SCHEMES = {"http", "https"}

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
                # 2) Fetch with a sane UA, timeouts, streaming, and size cap
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    )
                }
                with requests.get(
                    raw_url,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                    stream=True,
                    allow_redirects=True,
                ) as resp:
                    resp.raise_for_status()
                    # Respect encoding if server specifies one
                    resp.encoding = resp.apparent_encoding or resp.encoding

                    # Read up to MAX_DOWNLOAD_BYTES
                    content_chunks = []
                    total = 0
                    for chunk in resp.iter_content(chunk_size=16384):
                        if chunk:
                            total += len(chunk)
                            if total > MAX_DOWNLOAD_BYTES:
                                raise ValueError("Page too large (over 2 MB).")
                            content_chunks.append(chunk)
                    html_bytes = b"".join(content_chunks)
                    html = html_bytes.decode(resp.encoding or "utf-8", errors="replace")

                # 3) Parse and strip <head>, <script>, <style>, and comments
                soup = BeautifulSoup(html, "lxml")

                # We only care about <body>; if missing, fallback to whole doc
                body = soup.body if soup.body else soup

                for node in body.find_all(string=lambda t: isinstance(t, Comment)):
                    node.extract()

                # 1) Drop by tag name (junk containers)
                for tag in body.find_all([
                    "script", "style", "noscript", "template",
                    "iframe", "frame", "frameset", "object", "embed", "canvas", "svg", "video", "audio", "picture",
                    "source", "figure", "figcaption",
                    "form", "button", "input", "select", "textarea", "label",
                    "nav", "header", "footer", "aside", "menu", "dialog"
                ]):
                    tag.decompose()

                # 2) Drop ARIA/role landmarks not part of main content
                for el in list(body.select(
                        "[role=banner],[role=navigation],[role=complementary],[role=contentinfo],[role=search],[role=dialog],[role=alert],[role=alertdialog]")):
                    el.decompose()

                # Also remove anything explicitly hidden
                for el in list(body.select("[hidden], [style*='display:none'], [style*='visibility:hidden']")):
                    el.decompose()

                # 3) Drop common chrome by id/class substrings (case-insensitive)
                substrings = [
                    "cookie", "consent", "gdpr", "subscribe", "signup", "newsletter", "modal", "overlay",
                    "paywall", "meter", "gate", "promo", "breadcrumb", "share", "social", "toolbar",
                    "footer", "header", "nav", "sidebar"
                ]

                # Build a single CSS selector that finds any element whose id/class contains those substrings (case-insensitive)
                selector = ",".join([f'[id*="{s}" i],[class*="{s}" i]' for s in substrings])

                for el in body.select(selector):
                    el.decompose()

                # 4) Extract text with line breaks
                # Using a newline separator preserves block structure fairly well
                text = body.get_text(separator="\n", strip=True)

                # 5) Normalise excessive blank lines (keep it readable)
                text = re.sub(r"\n{3,}", "\n\n", text)

                text_result = text

            except requests.exceptions.RequestException as ex:
                error = f"Network/HTTP error: {ex}"
            except Exception as ex:
                error = f"Failed to extract text: {ex}"

    return render_template("index.html", text_result=text_result, error=error)

if __name__ == "__main__":
    # For local dev; in PyCharm, run this configuration.
    app.run(host="127.0.0.1", port=8000, debug=True)
