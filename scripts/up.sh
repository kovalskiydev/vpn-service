#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[info] Created .env from .env.example"
  echo "[info] Edit .env for production before exposing to Internet"
fi

docker compose up -d --build

echo "[ok] VPN Panel started"
echo "[ok] URL: http://127.0.0.1:${PANEL_PORT:-18081}"
echo "[ok] To start bot too: docker compose --profile bot up -d --build"
