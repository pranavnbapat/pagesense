from __future__ import annotations

import time

import requests
from flask import Blueprint, render_template, request

from pagesense.services.extractor import extract_text_from_url
from pagesense.services.request_logs import log_request_event


web_bp = Blueprint("web", __name__)


@web_bp.route("/", methods=["GET", "POST"])
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
        except ValueError as exc:
            error = str(exc)
            response_status = 422
        except requests.exceptions.RequestException as exc:
            error = f"Network/HTTP error: {exc}"
            response_status = 502
        except Exception as exc:
            error = f"Failed to extract text: {exc}"
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
