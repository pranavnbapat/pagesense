# app.py

from __future__ import annotations

import ipaddress
import json
import logging
import os
import random
import re
import socket
import sqlite3
import time
from textwrap import dedent

import requests

from pathlib import Path

from bs4 import BeautifulSoup, Comment
from flask import Flask, render_template, request, jsonify, Response
from urllib.parse import urlparse

from utils import extract_pdf_text_from_bytes, fetch_with_browser


BASE_DIR = Path(__file__).resolve().parent

LAST_DOMAIN_CALL: dict[str, float] = {}

LOGGER = logging.getLogger(__name__)

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

try:
    from flask_cors import CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})  # relax
except Exception:
    pass  # API still works without CORS if same-origin


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_csv(
    name: str,
    default: tuple[str, ...],
    *,
    lowercase: bool = False,
    separator: str = ",",
) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    values = []
    for item in raw.split(separator):
        value = item.strip()
        if not value:
            continue
        values.append(value.lower() if lowercase else value)
    return tuple(values)


load_env_file(BASE_DIR / ".env")


# --- Basic safety knobs (helpful even for local use) ---
POLITE_MODE = env_bool("POLITE_MODE", False)
MAX_HTML_BYTES = env_int("MAX_HTML_BYTES", 5_000_000)
MAX_PDF_BYTES = env_int("MAX_PDF_BYTES", 50_000_000)
REQUEST_TIMEOUT = (
    env_int("HTTP_CONNECT_TIMEOUT_SECONDS", 10),
    env_int("HTTP_READ_TIMEOUT_SECONDS", 30),
)
PLAYWRIGHT_TIMEOUT_MS = env_int("PLAYWRIGHT_TIMEOUT_MS", 30_000)
MIN_BROWSER_FALLBACK_TEXT = env_int("MIN_BROWSER_FALLBACK_TEXT", 120)
EXTRACTION_DEADLINE_SECONDS = env_int("EXTRACTION_DEADLINE_SECONDS", 30)
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = env_int("PORT", 8006)
REQUEST_LOGGING_ENABLED = env_bool("REQUEST_LOGGING_ENABLED", True)
REQUEST_LOG_DB_PATH = os.environ.get("REQUEST_LOG_DB_PATH", str(BASE_DIR / "requests.db"))
REQUEST_LOG_API_ENABLED = env_bool("REQUEST_LOG_API_ENABLED", False)
REQUEST_LOG_API_TOKEN = os.environ.get("REQUEST_LOG_API_TOKEN", "").strip()
ALLOWED_SCHEMES = set(env_csv("ALLOWED_SCHEMES", ("http", "https"), lowercase=True))
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

UA_POOL = list(env_csv(
    "UA_POOL",
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    ),
    separator="||",
))

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

BLOCKED_HOST_PATTERNS = env_csv(
    "BLOCKED_HOST_PATTERNS",
    (
        "youtube.com",
        "youtu.be",
        "m.youtube.com",
        "youtube-nocookie.com",
        "vimeo.com",
        "player.vimeo.com",
        "dailymotion.com",
        "www.dailymotion.com",
        "twitch.tv",
        "www.twitch.tv",
        "tiktok.com",
        "www.tiktok.com",
    ),
    lowercase=True,
)


def init_request_log_db() -> None:
    if not REQUEST_LOGGING_ENABLED:
        return

    db_path = Path(REQUEST_LOG_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                client_ip TEXT,
                forwarded_for TEXT,
                user_agent TEXT,
                referer TEXT,
                target_url TEXT,
                query_string TEXT,
                request_content_type TEXT,
                request_payload TEXT,
                response_status INTEGER,
                ok INTEGER NOT NULL,
                resolved_url TEXT,
                error_message TEXT,
                duration_ms INTEGER,
                headers_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_request_logs_source ON request_logs(source)"
        )


def get_client_ip() -> tuple[str | None, str | None]:
    forwarded_for = request.headers.get("X-Forwarded-For", "").strip() or None
    if forwarded_for:
        client_ip = forwarded_for.split(",", 1)[0].strip() or request.remote_addr
        return client_ip, forwarded_for
    return request.remote_addr, None


def serialize_request_payload() -> str | None:
    if request.method == "GET":
        return None

    payload: dict[str, object] = {}
    json_payload = request.get_json(silent=True)
    if json_payload is not None:
        payload["json"] = json_payload
    if request.form:
        payload["form"] = request.form.to_dict(flat=True)

    if not payload:
        raw_body = request.get_data(cache=True, as_text=True)
        if raw_body:
            payload["raw"] = raw_body[:4000]

    return json.dumps(payload, ensure_ascii=True, sort_keys=True) if payload else None


def log_request_event(
    *,
    source: str,
    started_at: float,
    target_url: str | None,
    response_status: int,
    ok: bool,
    resolved_url: str | None = None,
    error_message: str | None = None,
) -> None:
    if not REQUEST_LOGGING_ENABLED:
        return

    client_ip, forwarded_for = get_client_ip()
    headers_snapshot = {
        "host": request.headers.get("Host"),
        "x_forwarded_proto": request.headers.get("X-Forwarded-Proto"),
        "x_forwarded_host": request.headers.get("X-Forwarded-Host"),
        "accept": request.headers.get("Accept"),
    }
    query_string = request.query_string.decode("utf-8", errors="replace") or None
    duration_ms = int((time.monotonic() - started_at) * 1000)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    with sqlite3.connect(REQUEST_LOG_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO request_logs (
                created_at,
                source,
                method,
                path,
                client_ip,
                forwarded_for,
                user_agent,
                referer,
                target_url,
                query_string,
                request_content_type,
                request_payload,
                response_status,
                ok,
                resolved_url,
                error_message,
                duration_ms,
                headers_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                source,
                request.method,
                request.path,
                client_ip,
                forwarded_for,
                request.headers.get("User-Agent"),
                request.headers.get("Referer"),
                target_url,
                query_string,
                request.content_type,
                serialize_request_payload(),
                response_status,
                1 if ok else 0,
                resolved_url,
                error_message,
                duration_ms,
                json.dumps(headers_snapshot, ensure_ascii=True, sort_keys=True),
            ),
        )


init_request_log_db()


def get_logs_from_db(*, limit: int, offset: int, source: str | None = None, ok: int | None = None) -> list[dict[str, object]]:
    query = """
        SELECT
            id,
            created_at,
            source,
            method,
            path,
            client_ip,
            forwarded_for,
            user_agent,
            referer,
            target_url,
            query_string,
            request_content_type,
            request_payload,
            response_status,
            ok,
            resolved_url,
            error_message,
            duration_ms,
            headers_json
        FROM request_logs
    """
    clauses: list[str] = []
    params: list[object] = []
    if source:
        clauses.append("source = ?")
        params.append(source)
    if ok is not None:
        clauses.append("ok = ?")
        params.append(ok)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with sqlite3.connect(REQUEST_LOG_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    results: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        headers_json = item.get("headers_json")
        request_payload = item.get("request_payload")
        item["ok"] = bool(item["ok"])
        item["headers"] = json.loads(headers_json) if headers_json else None
        item["request_payload"] = json.loads(request_payload) if request_payload else None
        item.pop("headers_json", None)
        results.append(item)
    return results


def is_log_api_authorized() -> bool:
    if not REQUEST_LOG_API_ENABLED:
        return False
    if not REQUEST_LOG_API_TOKEN:
        return False

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip() == REQUEST_LOG_API_TOKEN

    token = request.args.get("token") or request.headers.get("X-Logs-Token", "")
    return token.strip() == REQUEST_LOG_API_TOKEN


def build_openapi_spec() -> dict[str, object]:
    server_url = os.environ.get("PUBLIC_BASE_URL", "").strip()
    servers = [{"url": server_url}] if server_url else []

    extract_success = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "example": True},
            "url": {"type": "string", "format": "uri"},
            "resolved_url": {"type": "string", "format": "uri"},
            "text": {"type": "string"},
        },
        "required": ["ok", "url", "resolved_url", "text"],
    }
    error_response = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "example": False},
            "url": {"type": "string", "format": "uri", "nullable": True},
            "error": {"type": "string"},
        },
        "required": ["ok", "error"],
    }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "PageSense API",
            "version": "1.0.0",
            "description": "Extract readable text from public web pages and PDFs.",
        },
        "servers": servers,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "Token",
                }
            },
            "schemas": {
                "ExtractRequest": {
                    "type": "object",
                    "properties": {"url": {"type": "string", "format": "uri"}},
                    "required": ["url"],
                },
                "ExtractSuccess": extract_success,
                "ErrorResponse": error_response,
                "LogEntry": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "created_at": {"type": "string", "format": "date-time"},
                        "source": {"type": "string", "enum": ["api", "ui"]},
                        "method": {"type": "string"},
                        "path": {"type": "string"},
                        "client_ip": {"type": "string", "nullable": True},
                        "forwarded_for": {"type": "string", "nullable": True},
                        "user_agent": {"type": "string", "nullable": True},
                        "referer": {"type": "string", "nullable": True},
                        "target_url": {"type": "string", "nullable": True},
                        "query_string": {"type": "string", "nullable": True},
                        "request_content_type": {"type": "string", "nullable": True},
                        "request_payload": {"type": "object", "nullable": True},
                        "response_status": {"type": "integer", "nullable": True},
                        "ok": {"type": "boolean"},
                        "resolved_url": {"type": "string", "nullable": True},
                        "error_message": {"type": "string", "nullable": True},
                        "duration_ms": {"type": "integer", "nullable": True},
                        "headers": {"type": "object", "nullable": True},
                    },
                },
                "LogsResponse": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean", "example": True},
                        "logs": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/LogEntry"},
                        },
                        "count": {"type": "integer"},
                        "limit": {"type": "integer"},
                        "offset": {"type": "integer"},
                    },
                    "required": ["ok", "logs", "count", "limit", "offset"],
                },
            },
        },
        "paths": {
            "/api/extract": {
                "get": {
                    "summary": "Extract readable text from a URL",
                    "parameters": [
                        {
                            "name": "url",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string", "format": "uri"},
                            "description": "Public HTTP or HTTPS URL to extract.",
                        }
                    ],
                    "responses": {
                        "200": {"description": "Successful extraction", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ExtractSuccess"}}}},
                        "400": {"description": "Missing URL", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                        "422": {"description": "Blocked, invalid, or unsupported URL", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                        "502": {"description": "Upstream fetch failure", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                        "504": {"description": "Upstream timeout", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                    },
                },
                "post": {
                    "summary": "Extract readable text from a URL",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/ExtractRequest"}},
                            "application/x-www-form-urlencoded": {"schema": {"$ref": "#/components/schemas/ExtractRequest"}},
                        },
                    },
                    "responses": {
                        "200": {"description": "Successful extraction", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ExtractSuccess"}}}},
                        "400": {"description": "Missing URL", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                        "422": {"description": "Blocked, invalid, or unsupported URL", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                        "502": {"description": "Upstream fetch failure", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                        "504": {"description": "Upstream timeout", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                    },
                },
            },
            "/api/logs": {
                "get": {
                    "summary": "Read recent request logs",
                    "security": [{"bearerAuth": []}],
                    "parameters": [
                        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200}},
                        {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0, "minimum": 0}},
                        {"name": "source", "in": "query", "schema": {"type": "string", "enum": ["api", "ui"]}},
                        {"name": "ok", "in": "query", "schema": {"type": "boolean"}},
                    ],
                    "responses": {
                        "200": {"description": "Recent logs", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/LogsResponse"}}}},
                        "401": {"description": "Unauthorized", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                        "404": {"description": "Log API disabled", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}},
                    },
                }
            },
        },
    }


def is_blocked_media_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.lower().rstrip(".")
    return any(host == pattern or host.endswith(f".{pattern}") for pattern in BLOCKED_HOST_PATTERNS)

def is_private_host(hostname: str) -> bool:
    """
    Block IP literals and hostnames that resolve to private/loopback ranges.
    """
    if not hostname:
        return True

    try:
        ip = ipaddress.ip_address(hostname)
        return any(ip in net for net in PRIVATE_NETS)
    except ValueError:
        pass

    try:
        addrinfo = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False

    resolved_ips = {item[4][0] for item in addrinfo if item[4]}
    for ip_text in resolved_ips:
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if any(ip in net for net in PRIVATE_NETS):
            return True
    return False


def is_allowed_url(raw_url: str) -> bool:
    parsed = urlparse((raw_url or "").strip())
    return bool(
        parsed.scheme in ALLOWED_SCHEMES
        and parsed.hostname
        and not is_private_host(parsed.hostname)
        and not is_blocked_media_host(parsed.hostname)
    )


def validate_url(raw_url: str) -> tuple[str, str]:
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.netloc:
        raise ValueError("Please enter a valid http(s) URL (e.g., https://example.com/page).")
    if is_blocked_media_host(parsed.hostname):
        raise ValueError("Video platform URLs are not supported.")
    if is_private_host(parsed.hostname):
        raise ValueError("Private/loopback addresses are not allowed.")
    return parsed.geturl(), parsed.netloc.lower()


def ensure_within_deadline(started_at: float) -> None:
    if time.monotonic() - started_at > EXTRACTION_DEADLINE_SECONDS:
        raise ValueError("Extraction exceeded the 30-second limit.")


def read_response_bytes(resp: requests.Response, *, byte_limit: int, label: str) -> bytes:
    content_chunks: list[bytes] = []
    total = 0
    resp.raw.decode_content = False
    for chunk in resp.raw.stream(16384, decode_content=False):
        if not chunk:
            continue
        total += len(chunk)
        if total > byte_limit:
            size_mb = byte_limit // 1_000_000
            raise ValueError(f"{label} too large (over {size_mb} MB).")
        content_chunks.append(chunk)
    return b"".join(content_chunks)


def extract_clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    body = soup.body if soup.body else soup

    for node in body.find_all(string=lambda t: isinstance(t, Comment)):
        node.extract()

    for tag in body.find_all([
        "script", "style", "noscript", "template", "iframe", "frame", "frameset", "object",
        "embed", "canvas", "svg", "video", "audio", "picture", "source", "figure", "figcaption",
        "form", "button", "input", "select", "textarea", "label", "nav", "header", "footer",
        "aside", "menu", "dialog"
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
        "footer", "header", "nav", "sidebar"
    ]
    selector = ",".join([f'[id*="{s}" i],[class*="{s}" i]' for s in substrings])
    for el in body.select(selector):
        el.decompose()

    text = body.get_text("\n", True)  # type: ignore[arg-type]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def should_use_browser_fallback(html: str, clean_text: str) -> bool:
    if len(clean_text) >= MIN_BROWSER_FALLBACK_TEXT:
        return False
    lowered = html.lower()
    return "<script" in lowered or "id=\"app\"" in lowered or "id=\"root\"" in lowered


def extract_text_from_url(raw_url: str) -> tuple[str, str]:
    """
    Fetch URL and return cleaned text. Raises ValueError on validation issues.
    Propagates requests exceptions for network/HTTP problems.
    """
    normalized_url, domain = validate_url(raw_url)
    parsed = urlparse(normalized_url)
    started_at = time.monotonic()

    session = requests.Session()

    from requests.adapters import HTTPAdapter
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({"Accept-Encoding": "identity"})

    def attempt_fetch(user_agent: str) -> tuple[bytes, str, str, str]:
        ensure_within_deadline(started_at)

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
            "User-Agent": user_agent,
            **BROWSERY_HEADERS,
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

        with session.get(normalized_url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=True) as resp:
            ensure_within_deadline(started_at)

            # record this request time only AFTER real network hit
            LAST_DOMAIN_CALL[domain] = time.monotonic()

            final_url = resp.url
            final_parsed = urlparse(final_url)
            final_host = final_parsed.netloc.lower()

            if final_host != parsed.netloc.lower():
                headers["Referer"] = f"https://{final_host}/"

            if is_blocked_media_host(final_parsed.hostname):
                raise ValueError("Video platform URLs are not supported.")
            if is_private_host(final_parsed.hostname):
                raise ValueError("Redirected to a private/loopback address, which is not allowed.")

            resp.raise_for_status()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            final_path = final_parsed.path.lower()
            is_pdf = False
            if "application/pdf" in ctype:
                is_pdf = True
            elif "application/octet-stream" in ctype and final_path.endswith(".pdf"):
                is_pdf = True

            if is_pdf:
                pdf_bytes = read_response_bytes(resp, byte_limit=MAX_PDF_BYTES, label="File")
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
            html_bytes = read_response_bytes(resp, byte_limit=MAX_HTML_BYTES, label="Page")
            return html_bytes, (resp.encoding or "utf-8"), final_url, "html"

    needs_browser_fetch = False
    browser_fallback_reason: Exception | None = None
    try:
        result = attempt_fetch(random.choice(UA_POOL))
        html_bytes, enc, resolved_url, content_kind = result
    except requests.exceptions.HTTPError as ex:
        sc = getattr(ex.response, "status_code", None)
        if sc in (401, 403, 451, 429):
            raise ValueError(
                f"Site refused access (HTTP {sc}). "
                "They may require a browser, login, or disallow automated fetches."
            )
        raise
    except (
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
        requests.exceptions.ConnectionError,
    ) as ex:
        needs_browser_fetch = True
        browser_fallback_reason = ex
        resolved_url = normalized_url
        content_kind = "html"
        html_bytes = b""
        enc = "utf-8"

    if content_kind == "pdf":
        ensure_within_deadline(started_at)
        return resolved_url, html_bytes.decode(enc, errors="replace")

    html = html_bytes.decode(enc, errors="replace")
    clean_text = extract_clean_text(html) if html else ""

    if needs_browser_fetch or should_use_browser_fallback(html, clean_text):
        try:
            ensure_within_deadline(started_at)
            browser_url = resolved_url or normalized_url
            resolved_url, html = fetch_with_browser(
                browser_url,
                allow_url=is_allowed_url,
                timeout_ms=min(
                    PLAYWRIGHT_TIMEOUT_MS,
                    max(1_000, int((EXTRACTION_DEADLINE_SECONDS - (time.monotonic() - started_at)) * 1000)),
                ),
            )
            ensure_within_deadline(started_at)
            clean_text = extract_clean_text(html)
        except Exception as browser_ex:
            LOGGER.warning("browser fallback failed: %s", browser_ex)
            if not clean_text:
                if browser_fallback_reason is not None:
                    raise ValueError(
                        f"Failed to fetch page with browser fallback: {browser_ex}"
                    ) from browser_fallback_reason
                raise

    ensure_within_deadline(started_at)
    return resolved_url, clean_text


@app.route("/", methods=["GET", "POST"])
def index():
    started_at = time.monotonic()
    text_result = None
    error = None
    raw_url = None
    response_status = 200
    resolved_url = None

    if request.method == "POST":
        raw_url = (request.form.get("url") or "").strip()

        try:
            resolved_url, text_result = extract_text_from_url(raw_url)
        except ValueError as ex:
            error = str(ex)
            response_status = 422
        except requests.exceptions.RequestException as ex:
            error = f"Network/HTTP error: {ex}"
            response_status = 502
        except Exception as ex:
            error = f"Failed to extract text: {ex}"
            response_status = 500

    log_request_event(
        source="ui",
        started_at=started_at,
        target_url=raw_url,
        response_status=response_status,
        ok=error is None,
        resolved_url=resolved_url,
        error_message=error,
    )

    return render_template("index.html", text_result=text_result, error=error)


@app.route("/api/extract", methods=["GET", "POST"])
def api_extract():
    """
    Accepts:
      - GET  /api/extract?url=...
      - POST /api/extract  {"url": "..."}
    Returns JSON: { ok, url, text?, error? }
    """
    started_at = time.monotonic()
    if request.method == "GET":
        url = request.args.get("url")
    else:
        payload = request.get_json(silent=True) or {}
        url = payload.get("url") or request.form.get("url")
    if not url:
        response = jsonify({"ok": False, "error": "Missing 'url' parameter"})
        status = 400
        log_request_event(
            source="api",
            started_at=started_at,
            target_url=None,
            response_status=status,
            ok=False,
            error_message="Missing 'url' parameter",
        )
        return response, status

    try:
        resolved_url, text = extract_text_from_url(url)
        response = jsonify({"ok": True, "url": url, "resolved_url": resolved_url, "text": text})
        status = 200
        log_request_event(
            source="api",
            started_at=started_at,
            target_url=url,
            response_status=status,
            ok=True,
            resolved_url=resolved_url,
        )
        return response, status
    except ValueError as ve:
        response = jsonify({"ok": False, "url": url, "error": str(ve)})
        status = 422
        log_request_event(
            source="api",
            started_at=started_at,
            target_url=url,
            response_status=status,
            ok=False,
            error_message=str(ve),
        )
        return response, status
    except requests.exceptions.Timeout:
        response = jsonify({"ok": False, "url": url, "error": "Upstream timeout"})
        status = 504
        log_request_event(
            source="api",
            started_at=started_at,
            target_url=url,
            response_status=status,
            ok=False,
            error_message="Upstream timeout",
        )
        return response, status
    except requests.exceptions.RequestException as rexc:
        message = f"Network/HTTP error: {rexc}"
        response = jsonify({"ok": False, "url": url, "error": message})
        status = 502
        log_request_event(
            source="api",
            started_at=started_at,
            target_url=url,
            response_status=status,
            ok=False,
            error_message=message,
        )
        return response, status
    except Exception as ex:
        message = f"Failed to extract text: {ex}"
        response = jsonify({"ok": False, "url": url, "error": message})
        status = 500
        log_request_event(
            source="api",
            started_at=started_at,
            target_url=url,
            response_status=status,
            ok=False,
            error_message=message,
        )
        return response, status


@app.route("/api/logs", methods=["GET"])
def api_logs():
    started_at = time.monotonic()

    if not REQUEST_LOG_API_ENABLED:
        response = jsonify({"ok": False, "error": "Log API is disabled."})
        status = 404
        return response, status

    if not is_log_api_authorized():
        response = jsonify({"ok": False, "error": "Unauthorized"})
        status = 401
        return response, status

    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
    except ValueError:
        limit = 50

    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except ValueError:
        offset = 0

    source = request.args.get("source")
    if source not in {None, "", "api", "ui"}:
        source = None

    ok_param = request.args.get("ok")
    if ok_param is None or ok_param == "":
        ok_filter = None
    else:
        ok_filter = 1 if ok_param.strip().lower() in {"1", "true", "yes"} else 0

    logs = get_logs_from_db(limit=limit, offset=offset, source=source, ok=ok_filter)
    response = jsonify({
        "ok": True,
        "logs": logs,
        "count": len(logs),
        "limit": limit,
        "offset": offset,
    })
    status = 200
    log_request_event(
        source="api",
        started_at=started_at,
        target_url=None,
        response_status=status,
        ok=True,
        error_message=None,
    )
    return response, status


@app.route("/openapi.json", methods=["GET"])
def openapi_spec():
    return jsonify(build_openapi_spec())


@app.route("/docs", methods=["GET"])
def swagger_ui():
    html = dedent(
        """
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <title>PageSense API Docs</title>
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
          <style>
            body { margin: 0; background: #fafafa; }
            .topbar { display: none; }
          </style>
        </head>
        <body>
          <div id="swagger-ui"></div>
          <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
          <script>
            window.ui = SwaggerUIBundle({
              url: "/openapi.json",
              dom_id: "#swagger-ui",
              deepLinking: true,
              presets: [SwaggerUIBundle.presets.apis],
            });
          </script>
        </body>
        </html>
        """
    ).strip()
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    init_request_log_db()
    app.run(host=HOST, port=PORT, debug=False)
