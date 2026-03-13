# PageSense

PageSense is a small Flask app that fetches a public URL and returns readable text. It supports both regular HTML pages and text-based PDFs, with a browser fallback for pages that render most of their content client-side.

## What it does

- Accepts a single `http` or `https` URL.
- Fetches HTML with streaming reads, timeouts, and size caps.
- Extracts text from text-based PDFs.
- Removes scripts, forms, media, navigation, overlays, paywall markers, cookie banners, and other common non-content elements.
- Falls back to Playwright/Chromium only when the plain HTTP result looks script-driven or the upstream response cannot be decoded reliably.
- Exposes both a browser UI and a JSON API at `/api/extract`.

## Safety and operational limits

- Only `http` and `https` are allowed.
- Literal private IPs and hostnames that resolve to private or loopback ranges are blocked.
- Redirects to private or loopback targets are blocked.
- Common video-platform hosts such as YouTube, Vimeo, Dailymotion, Twitch, and TikTok are blocked.
- Default limits:
  - HTML: 5 MB
  - PDF: 50 MB
  - HTTP timeout: connect 10s, read 30s
  - Browser timeout: 30s
  - Total extraction budget: 30s
- No arbitrary JavaScript is executed unless the browser fallback is required.

## Tech stack

- Python 3.10+
- Flask
- Requests
- BeautifulSoup4 + lxml
- pypdf
- Playwright + Chromium

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

Open `http://127.0.0.1:8006`.

Runtime settings are loaded from [`.env`](/home/pranav/PyCharm/Personal/pagesense/.env). Use [`.env.sample`](/home/pranav/PyCharm/Personal/pagesense/.env.sample) as the template. `UA_POOL` uses `||` as the separator between user-agent strings.

Requests are logged to SQLite when `REQUEST_LOGGING_ENABLED=true`. By default the file is [requests.db](/home/pranav/PyCharm/Personal/pagesense/requests.db) and each row stores timestamp, source (`ui` or `api`), client IP, forwarded IP chain, method, path, target URL, payload details, status, duration, and any error message.

For local inspection, use [view_logs.py](/home/pranav/PyCharm/Personal/pagesense/view_logs.py):

```bash
.venv/bin/python view_logs.py --limit 20
```

You can also enable a token-guarded log API by setting `REQUEST_LOG_API_ENABLED=true` and `REQUEST_LOG_API_TOKEN` in [`.env`](/home/pranav/PyCharm/Personal/pagesense/.env), then calling:

```bash
curl -sS http://127.0.0.1:8006/api/logs \
  -H 'Authorization: Bearer change-me'
```

Interactive API docs are available at `/docs`, with the OpenAPI schema at `/openapi.json`.

For server deployment behind Traefik, use [docker-compose-online.yml](/home/pranav/PyCharm/Personal/pagesense/docker-compose-online.yml) and [`.env.online.sample`](/home/pranav/PyCharm/Personal/pagesense/.env.online.sample) as the starting point. It mounts `./data` into the container so `requests.db` survives container recreation.

## API examples

```bash
curl -sS --get http://127.0.0.1:8006/api/extract \
  --data-urlencode "url=https://example.com/article"
```

```bash
curl -sS -X POST http://127.0.0.1:8006/api/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/article"}'
```

Form-encoded POSTs are also accepted:

```bash
curl -sS -X POST http://127.0.0.1:8006/api/extract \
  -d 'url=https://example.com/article'
```

## Production notes

- Use Gunicorn for production.
- If you keep the Playwright fallback enabled, prefer a process-based worker model over threaded workers unless you have explicitly validated your browser lifecycle design.
