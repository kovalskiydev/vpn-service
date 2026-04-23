#!/usr/bin/env bash
set -eu

# Beautiful one-shot setup: Xray VLESS + REALITY (TCP)
# OS: Debian/Ubuntu with systemd

PORT="${PORT:-443}"
SNI="${SNI:-www.cloudflare.com}"
CLIENT_NAME="${CLIENT_NAME:-happ-vless}"
FLOW="${FLOW:-}"
PUBLIC_IP="${PUBLIC_IP:-}"
XRAY_CONFIG="/usr/local/etc/xray/config.json"

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
step() { printf '%s\n' "${C_BOLD}${C_BLUE}[$1/8]${C_RST} $2"; }
fail() { err "$1"; exit 1; }

print_header() {
  line
  printf '%s\n' "${C_BOLD}${C_CYAN}VLESS + REALITY Auto Setup${C_RST}"
  printf '%s\n' "Target: Debian/Ubuntu | Service: Xray"
  line
  printf '%s\n' "PORT=${PORT} | SNI=${SNI} | CLIENT_NAME=${CLIENT_NAME}"
  if [ -n "$FLOW" ]; then
    printf '%s\n' "FLOW=${FLOW}"
  else
    printf '%s\n' "FLOW=(disabled, better compatibility)"
  fi
  line
}

require_root() {
  [ "$(id -u)" -eq 0 ] || fail "Run as root: sudo bash $0"
}

require_os() {
  command -v apt-get >/dev/null 2>&1 || fail "This script supports Debian/Ubuntu (apt-get)."
  command -v systemctl >/dev/null 2>&1 || fail "systemd is required."
}

validate_vars() {
  case "$PORT" in
    ''|*[!0-9]*) fail "PORT must be numeric." ;;
  esac
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || fail "PORT must be in range 1..65535"

  printf '%s' "$SNI" | grep -Eq '^[A-Za-z0-9.-]+$' || fail "SNI must look like a domain (letters, digits, dot, dash)."
}

check_port_free() {
  if ss -lntp 2>/dev/null | grep -q ":${PORT} "; then
    warn "Port ${PORT} is already in use:"
    ss -lntp | grep ":${PORT} " || true
    fail "Choose another port, e.g.: PORT=2053 sudo bash $0"
  fi
}

install_base() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y bash curl unzip openssl uuid-runtime ca-certificates
}

install_xray_if_needed() {
  if ! command -v xray >/dev/null 2>&1; then
    INSTALLER="/tmp/install-release.sh"
    curl -fsSL https://raw.githubusercontent.com/XTLS/Xray-install/main/install-release.sh -o "$INSTALLER"
    bash "$INSTALLER" install
  fi
  command -v xray >/dev/null 2>&1 || fail "Xray install failed."
}

generate_keys() {
  UUID="$(cat /proc/sys/kernel/random/uuid)"
  KEYS="$(xray x25519 2>&1 || true)"
  PRIVATE_KEY="$(printf '%s\n' "$KEYS" | grep -iE 'private[[:space:]_]?key[[:space:]]*:' | head -n1 | sed -E 's/^[^:]*:[[:space:]]*//' | tr -d '\r')"
  PUBLIC_KEY="$(printf '%s\n' "$KEYS" | grep -iE 'public[[:space:]_]?key[[:space:]]*[:)]|password[[:space:]]*\(publickey\)[[:space:]]*:' | head -n1 | sed -E 's/^[^:]*:[[:space:]]*//' | tr -d '\r')"
  SHORT_ID="$(openssl rand -hex 8)"

  [ -n "$PRIVATE_KEY" ] && [ -n "$PUBLIC_KEY" ] || {
    err "Failed to generate Reality keypair. Raw output:"
    printf '%s\n' "$KEYS"
    exit 1
  }
}

detect_public_ip() {
  if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP="$(curl -4 -s --max-time 10 https://api.ipify.org || true)"
  fi
  if [ -z "$PUBLIC_IP" ]; then
    PUBLIC_IP="$(hostname -I | awk '{print $1}')"
  fi
  [ -n "$PUBLIC_IP" ] || fail "Could not detect public IP. Re-run with: PUBLIC_IP=x.x.x.x sudo bash $0"
}

write_config() {
  mkdir -p /usr/local/etc/xray

  if [ -n "$FLOW" ]; then
    CLIENT_JSON="\"id\": \"${UUID}\",\n            \"flow\": \"${FLOW}\""
    FLOW_QUERY="&flow=${FLOW}"
  else
    CLIENT_JSON="\"id\": \"${UUID}\""
    FLOW_QUERY=""
  fi

  cat > "$XRAY_CONFIG" <<CFG
{
  "log": {
    "loglevel": "warning"
  },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": ${PORT},
      "protocol": "vless",
      "settings": {
        "clients": [
          {
            ${CLIENT_JSON}
          }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${SNI}:443",
          "xver": 0,
          "serverNames": [
            "${SNI}"
          ],
          "privateKey": "${PRIVATE_KEY}",
          "shortIds": [
            "${SHORT_ID}"
          ]
        }
      },
      "sniffing": {
        "enabled": true,
        "destOverride": [
          "http",
          "tls",
          "quic"
        ]
      }
    }
  ],
  "outbounds": [
    {
      "protocol": "freedom",
      "tag": "direct"
    },
    {
      "protocol": "blackhole",
      "tag": "block"
    }
  ]
}
CFG
}

restart_xray() {
  systemctl enable xray >/dev/null 2>&1 || true
  systemctl restart xray
  if ! systemctl is-active --quiet xray; then
    err "Xray service is not active. Check logs:"
    printf '%s\n' "journalctl -u xray -n 100 --no-pager"
    exit 1
  fi
}

open_firewall() {
  if command -v ufw >/dev/null 2>&1; then
    UFW_STATUS="$(ufw status 2>/dev/null | head -n1 || true)"
    case "$UFW_STATUS" in
      *active*) ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true ;;
    esac
  fi
}

print_result() {
  LINK_NAME="$(printf '%s' "$CLIENT_NAME" | tr ' ' '_')"
  VLESS_LINK="vless://${UUID}@${PUBLIC_IP}:${PORT}?encryption=none${FLOW_QUERY}&security=reality&sni=${SNI}&fp=chrome&pbk=${PUBLIC_KEY}&sid=${SHORT_ID}&type=tcp&headerType=none#${LINK_NAME}"

  line
  ok "Setup complete."
  printf '%s\n' "${C_BOLD}Client import link (VLESS Reality):${C_RST}"
  printf '%s\n' "$VLESS_LINK"
  line
  printf '%s\n' "${C_BOLD}Connection params:${C_RST}"
  printf '%s\n' "IP=${PUBLIC_IP}"
  printf '%s\n' "PORT=${PORT}"
  printf '%s\n' "UUID=${UUID}"
  printf '%s\n' "PublicKey=${PUBLIC_KEY}"
  printf '%s\n' "ShortID=${SHORT_ID}"
  printf '%s\n' "SNI=${SNI}"
  if [ -n "$FLOW" ]; then
    printf '%s\n' "FLOW=${FLOW}"
  fi
  line
  info "Tip: if client cannot connect, try another port: 2053, 2083, 8443"
}

main() {
  print_header

  step 1 "Checking permissions and OS"
  require_root
  require_os
  validate_vars

  step 2 "Checking port availability"
  check_port_free

  step 3 "Installing base packages"
  install_base

  step 4 "Installing Xray (if missing)"
  install_xray_if_needed

  step 5 "Generating Reality credentials"
  generate_keys
  detect_public_ip

  step 6 "Writing Xray config"
  write_config

  step 7 "Restarting Xray service"
  restart_xray

  step 8 "Configuring firewall and printing link"
  open_firewall
  print_result
}

main "$@"
