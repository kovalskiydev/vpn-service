# BURMALDAA VPN Project

Русский | English below

## RU: О проекте
Монорепозиторий с двумя частями:
- `web-admin/` - админ-панель VPN (FastAPI + UI + Telegram bot).
- `Sources/`, `Config/`, `VPNClient.xcodeproj` - macOS VPN клиент (SwiftUI/Network Extension).

## RU: Быстрый запуск (одна команда)
Требования: Docker + Docker Compose.

```bash
./scripts/up.sh
```

Что делает команда:
- создает `.env` из `.env.example` (если файла нет),
- собирает и запускает `vpn-panel`.

Панель будет доступна на:
- `http://127.0.0.1:18081`

Запуск панели + Telegram-бота:
```bash
docker compose --profile bot up -d --build
```

## RU: Настройка перед продакшеном
1. Откройте `.env` и поменяйте минимум:
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `BOT_API_TOKEN`
- `PUBLIC_BASE_URL` (если используете бота)
2. Настройте reverse proxy (Nginx/Caddy) и HTTPS.
3. Для HTTPS поставьте `ADMIN_COOKIE_SECURE=1`.

## RU: Структура
- `docker-compose.yml` - основной стек.
- `.env.example` - пример переменных окружения.
- `scripts/up.sh` - one-command запуск.
- `web-admin/server.py` - backend API.
- `web-admin/static/index.html` - frontend.
- `web-admin/telegram_key_bot.py` - Telegram bot.
- `archive/` - старые макеты и бэкапы.

## RU: Публикация на GitHub
```bash
git init
git add .
git commit -m "Initial public release"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo>.git
git push -u origin main
```

---

## EN: About
Monorepo containing:
- `web-admin/` - VPN admin panel (FastAPI + UI + Telegram bot).
- `Sources/`, `Config/`, `VPNClient.xcodeproj` - macOS VPN client (SwiftUI/Network Extension).

## EN: Quick Start (one command)
Requirements: Docker + Docker Compose.

```bash
./scripts/up.sh
```

What this command does:
- creates `.env` from `.env.example` (if missing),
- builds and starts `vpn-panel`.

Panel URL:
- `http://127.0.0.1:18081`

Run panel + Telegram bot:
```bash
docker compose --profile bot up -d --build
```

## EN: Production checklist
1. Edit `.env` and change at least:
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `BOT_API_TOKEN`
- `PUBLIC_BASE_URL` (if bot is enabled)
2. Configure reverse proxy (Nginx/Caddy) and HTTPS.
3. Set `ADMIN_COOKIE_SECURE=1` when using HTTPS.

## EN: Structure
- `docker-compose.yml` - main stack.
- `.env.example` - environment template.
- `scripts/up.sh` - one-command launcher.
- `web-admin/server.py` - backend API.
- `web-admin/static/index.html` - frontend.
- `web-admin/telegram_key_bot.py` - Telegram bot.
- `archive/` - old mockups and backups.

## EN: Publish to GitHub
```bash
git init
git add .
git commit -m "Initial public release"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo>.git
git push -u origin main
```
