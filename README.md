# PageSense - Extract body text

PageSense is a tiny Flask app that fetches any public web page and returns just the readable text from the <body>-with scripts, styles, headers/footers, navs, forms, and common overlays removed. It’s handy for “read it later”, downstream AI summarisation, or quick copy/paste without the chrome.

## What it does

- Accepts a single HTTP/HTTPS URL. 
- Downloads the page with sane timeouts and a size cap (2 MB). 
- Parses the HTML and removes noise.
- ARIA/role landmarks not part of content (banner, navigation, complementary, contentinfo, search, dialog, alert), explicitly hidden elements, and common “cookie/consent/subscribe/modal/overlay/paywall” blocks by id/class substring match. 
- Extracts plain text with sensible newlines and normalises blank lines. 
- Renders the result in a dark, minimal UI with word and character counts and a one-click Copy button. 
- Under the hood, we use Flask for routing/templates, Requests for HTTP, and BeautifulSoup (lxml parser) for HTML cleanup. See: Flask docs, Requests docs, and BeautifulSoup docs if you want to extend the extractor.

## Why it’s safe enough for local use
- SSRF guard: blocks obvious private/loopback IPs (127.0.0.0/8, RFC1918 ranges, link-local, IPv6 loopback/ULA/LL). 
- Protocol allow-list: only http and https. 
- Download cap: stops at ~2 MB to avoid huge pages. 
- Timeouts: (connect=5s, read=15s). 
- No JavaScript execution: fast, deterministic, and keeps the attack surface small. 
- Note: pages that require client-side JS to render content won’t be fully captured. That’s by design.

## Tech stack
- Python 3.10+ (tested on Linux)
- Flask (routing + Jinja templates)
- Requests (HTTP)
- BeautifulSoup4 + lxml (HTML parsing)

## Quick start (Linux)
- Create and activate a venv
```
python3 -m venv .venv
source .venv/bin/activate
```
- Install deps
```
pip install --upgrade pip
pip install -r requirements.txt
```
- Run
```
python app.py
```
#### Open http://127.0.0.1:10000 in your browser

### **You can also access it via an API:**

```
curl -sS --get http://127.0.0.1:10000/api/extract \
     --data-urlencode "url=https://example.com/article"
```

```
curl -sS -X POST http://127.0.0.1:10000/api/extract \
     -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com/article"}'
```