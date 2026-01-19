#!/usr/bin/env bash
set -euo pipefail

# bootstrap.sh
# - Clones/updates pagesense
# - Creates/updates venv
# - Installs Python deps + Playwright Chromium (and required system libs)
# - Runs the service on the correct RunPod proxy port via gunicorn under nohup
#
# Usage:
#   bash bootstrap.sh
#
# Env overrides (optional):
#   APP_DIR=/workspace/pagesense
#   GIT_URL=https://github.com/pranavnbapat/pagesense.git
#   PORT=8006
#   WORKERS=1
#   TIMEOUT=180

APP_DIR="${APP_DIR:-/workspace/pagesense}"
GIT_URL="${GIT_URL:-https://github.com/pranavnbapat/pagesense.git}"
PORT="${PORT:-8006}"
WORKERS="${WORKERS:-1}"
TIMEOUT="${TIMEOUT:-180}"

PY_BIN="${PY_BIN:-python3}"

echo "[bootstrap] APP_DIR=$APP_DIR"
echo "[bootstrap] GIT_URL=$GIT_URL"
echo "[bootstrap] PORT=$PORT"

# --- Clone or update repo ---
if [[ -d "$APP_DIR/.git" ]]; then
  echo "[bootstrap] Repo exists; pulling latest..."
  git -C "$APP_DIR" pull --ff-only
else
  echo "[bootstrap] Cloning repo..."
  mkdir -p "$APP_DIR"
  git clone "$GIT_URL" "$APP_DIR"
fi

cd "$APP_DIR"

# --- Ensure system deps for Playwright Chromium ---
echo "[bootstrap] Installing system libraries for Playwright (Chromium)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  curl ca-certificates \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 libcairo2 \
  libx11-6 libx11-xcb1 libxcb1 libxext6 libxrender1 \
  libglib2.0-0 libdrm2 libdbus-1-3 \
  && rm -rf /var/lib/apt/lists/*

# --- Create venv if missing ---
if [[ ! -d ".venv" ]]; then
  echo "[bootstrap] Creating venv..."
  "$PY_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[bootstrap] Upgrading pip tooling..."
python -m pip install -U pip wheel setuptools

echo "[bootstrap] Installing Python requirements..."
pip install -r requirements.txt

echo "[bootstrap] Ensuring gunicorn is installed..."
pip install -U gunicorn

# Playwright browsers are NOT installed by pip; must run this explicitly
echo "[bootstrap] Installing Playwright Chromium..."
python -m playwright install chromium

# --- Patch app.py to bind to 0.0.0.0 and use PORT env (idempotent-ish) ---
echo "[bootstrap] Patching app.py __main__ to bind 0.0.0.0:${PORT} (if needed)..."
if grep -q 'app.run(host="127.0.0.1"' app.py; then
  # Replace everything from if __name__ == "__main__": to end of file with a robust block.
  python - <<'PY'
from pathlib import Path
import re

p = Path("app.py")
s = p.read_text(encoding="utf-8")

pattern = r'if __name__ == "__main__":\s*(?:.|\n)*\Z'
replacement = (
    'if __name__ == "__main__":\n'
    '    import os\n'
    '    host = "0.0.0.0"\n'
    '    port = int(os.environ.get("PORT", "8006"))\n'
    '    # Flask dev server is fine for quick tests, but we run gunicorn in production.\n'
    '    app.run(host=host, port=port, debug=False)\n'
)

if re.search(pattern, s):
    s2 = re.sub(pattern, replacement, s, flags=re.MULTILINE)
else:
    # If the pattern isn't found, just append (won't break gunicorn usage).
    s2 = s.rstrip() + "\n\n" + replacement

p.write_text(s2, encoding="utf-8")
print("Patched app.py")
PY
else
  echo "[bootstrap] app.py already looks patched (or different); skipping patch."
fi

# --- Stop existing server if running ---
if [[ -f server.pid ]]; then
  OLD_PID="$(cat server.pid || true)"
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[bootstrap] Stopping existing server PID=$OLD_PID"
    kill "$OLD_PID" || true
    sleep 1
  fi
  rm -f server.pid
fi

# --- Start gunicorn under nohup ---
echo "[bootstrap] Starting gunicorn..."
export PORT="$PORT"

nohup .venv/bin/gunicorn \
  -w "$WORKERS" \
  -b "0.0.0.0:${PORT}" \
  --timeout "$TIMEOUT" \
  app:app \
  > server.log 2>&1 &

echo $! > server.pid
echo "[bootstrap] Started. PID=$(cat server.pid)"
echo "[bootstrap] Logs: tail -f $APP_DIR/server.log"
echo "[bootstrap] Local test: curl -sS http://127.0.0.1:${PORT}/ | head"
echo "[bootstrap] RunPod URL should be: https://<your-id>-${PORT}.proxy.runpod.net/"
