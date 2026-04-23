#!/usr/bin/env sh
set -eu

# One-shot deploy for telegram_key_bot.py as systemd service.
# Usage:
#   sudo sh deploy-telegram-bot.sh
# Optional env overrides:
#   BOT_SCRIPT=/root/web-admin/telegram_key_bot.py
#   PYTHON_BIN=/usr/bin/python3
#   SERVICE_NAME=vpn-telegram-bot
#   ENV_FILE=/etc/vpn-telegram-bot.env

SERVICE_NAME="${SERVICE_NAME:-vpn-telegram-bot}"
BOT_SCRIPT="${BOT_SCRIPT:-/root/web-admin/telegram_key_bot.py}"
WORKDIR="${WORKDIR:-/root/web-admin}"
ENV_FILE="${ENV_FILE:-/etc/vpn-telegram-bot.env}"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"

PANEL_BASE_URL="${PANEL_BASE_URL:-http://127.0.0.1:18081}"
BOT_SUB_DAYS="${BOT_SUB_DAYS:-7}"
BOT_SUB_COOLDOWN_DAYS="${BOT_SUB_COOLDOWN_DAYS:-7}"
BOT_SUB_TRAFFIC_GB="${BOT_SUB_TRAFFIC_GB:-50}"
BOT_SUB_DEVICE_LIMIT="${BOT_SUB_DEVICE_LIMIT:-1}"
TG_POLL_TIMEOUT="${TG_POLL_TIMEOUT:-30}"
TG_POLL_SLEEP_ON_ERROR="${TG_POLL_SLEEP_ON_ERROR:-3}"

if [ -t 1 ]; then
  C_RST="$(printf '\033[0m')"
  C_BOLD="$(printf '\033[1m')"
  C_BLUE="$(printf '\033[34m')"
  C_CYAN="$(printf '\033[36m')"
  C_GREEN="$(printf '\033[32m')"
  C_YELLOW="$(printf '\033[33m')"
  C_RED="$(printf '\033[31m')"
else
  C_RST=""; C_BOLD=""; C_BLUE=""; C_CYAN=""; C_GREEN=""; C_YELLOW=""; C_RED=""
fi

line() { printf '%s\n' "${C_BLUE}------------------------------------------------------------${C_RST}"; }
info() { printf '%s\n' "${C_CYAN}$*${C_RST}"; }
ok() { printf '%s\n' "${C_GREEN}$*${C_RST}"; }
warn() { printf '%s\n' "${C_YELLOW}$*${C_RST}"; }
err() { printf '%s\n' "${C_RED}$*${C_RST}"; }
step() { printf '%s\n' "${C_BOLD}${C_BLUE}[$1/7]${C_RST} $2"; }
fail() { err "$1"; exit 1; }

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "Run as root: sudo sh $0"
}

require_tools() {
  command -v systemctl >/dev/null 2>&1 || fail "systemd is required."
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python not found: $PYTHON_BIN"
}

require_files() {
  [ -f "$BOT_SCRIPT" ] || fail "Bot script not found: $BOT_SCRIPT"
}

ask_if_empty() {
  var_name="$1"
  prompt="$2"
  current="$(eval "printf '%s' \"\${$var_name:-}\"")"
  if [ -n "$current" ]; then
    return 0
  fi
  printf '%s' "$prompt"
  IFS= read -r value
  if [ -z "$value" ]; then
    fail "Required value is empty: $var_name"
  fi
  eval "$var_name=\$value"
}

escape_for_env() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

collect_inputs() {
  ask_if_empty TG_BOT_TOKEN "Telegram bot token (TG_BOT_TOKEN): "
  ask_if_empty TG_CHANNEL "Channel username/id (TG_CHANNEL, e.g. @mychannel): "
  ask_if_empty BOT_API_TOKEN "Backend bot secret (BOT_API_TOKEN): "
  ask_if_empty PUBLIC_BASE_URL "Public panel URL (PUBLIC_BASE_URL, e.g. https://example.com/vpn/panel): "
}

write_env_file() {
  mkdir -p "$(dirname "$ENV_FILE")"
  cat > "$ENV_FILE" <<EOF
TG_BOT_TOKEN="$(escape_for_env "$TG_BOT_TOKEN")"
TG_CHANNEL="$(escape_for_env "$TG_CHANNEL")"
PANEL_BASE_URL="$(escape_for_env "$PANEL_BASE_URL")"
BOT_API_TOKEN="$(escape_for_env "$BOT_API_TOKEN")"
PUBLIC_BASE_URL="$(escape_for_env "$PUBLIC_BASE_URL")"
TG_POLL_TIMEOUT="$(escape_for_env "$TG_POLL_TIMEOUT")"
TG_POLL_SLEEP_ON_ERROR="$(escape_for_env "$TG_POLL_SLEEP_ON_ERROR")"

# These values are consumed by backend /api/bot/issue-key
BOT_SUB_DAYS="$(escape_for_env "$BOT_SUB_DAYS")"
BOT_SUB_COOLDOWN_DAYS="$(escape_for_env "$BOT_SUB_COOLDOWN_DAYS")"
BOT_SUB_TRAFFIC_GB="$(escape_for_env "$BOT_SUB_TRAFFIC_GB")"
BOT_SUB_DEVICE_LIMIT="$(escape_for_env "$BOT_SUB_DEVICE_LIMIT")"
EOF
  chmod 600 "$ENV_FILE"
}

write_service() {
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Telegram VPN Key Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$WORKDIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON_BIN $BOT_SCRIPT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

enable_start_service() {
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null
  systemctl restart "$SERVICE_NAME"
}

print_result() {
  line
  ok "Done. Service deployed: $SERVICE_NAME"
  printf '%s\n' "Check status:  systemctl status $SERVICE_NAME --no-pager -l"
  printf '%s\n' "Tail logs:     journalctl -u $SERVICE_NAME -f --no-pager"
  printf '%s\n' "Env file:      $ENV_FILE"
  printf '%s\n' "Service file:  $SERVICE_FILE"
  line
  warn "Important: set same BOT_API_TOKEN in vpn-panel service Environment."
}

main() {
  line
  printf '%s\n' "${C_BOLD}${C_CYAN}Telegram Bot Deployment${C_RST}"
  line
  step 1 "Checking permissions"
  require_root
  step 2 "Checking dependencies"
  require_tools
  step 3 "Checking bot script"
  require_files
  step 4 "Collecting configuration"
  collect_inputs
  step 5 "Writing environment file"
  write_env_file
  step 6 "Writing systemd service"
  write_service
  step 7 "Enabling and starting service"
  enable_start_service
  print_result
}

main "$@"
