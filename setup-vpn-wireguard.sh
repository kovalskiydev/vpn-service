#!/usr/bin/env sh
set -eu

# WireGuard one-shot setup for Ubuntu/Debian.
# Creates server + one client and prints import link for apps that support wireguard:// URI.

WG_IFACE="wg0"
WG_PORT="${WG_PORT:-51820}"
WG_NET="${WG_NET:-10.66.66.0/24}"
WG_SERVER_IP="${WG_SERVER_IP:-10.66.66.1/24}"
WG_CLIENT_IP="${WG_CLIENT_IP:-10.66.66.2/32}"
CLIENT_NAME="${CLIENT_NAME:-happ-client}"
DNS_1="${DNS_1:-1.1.1.1}"
DNS_2="${DNS_2:-8.8.8.8}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo sh $0"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This script currently supports Debian/Ubuntu (apt-get)."
  exit 1
fi

echo "[1/8] Installing packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y wireguard iptables curl qrencode

echo "[2/8] Detecting network interface and server IP..."
PUB_IFACE="$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}')"
if [ -z "${PUB_IFACE:-}" ]; then
  PUB_IFACE="eth0"
fi

PUBLIC_IP="${PUBLIC_IP:-}"
if [ -z "$PUBLIC_IP" ]; then
  PUBLIC_IP="$(curl -4 -s --max-time 10 https://api.ipify.org || true)"
fi
if [ -z "$PUBLIC_IP" ]; then
  PUBLIC_IP="$(hostname -I | awk '{print $1}')"
fi
if [ -z "$PUBLIC_IP" ]; then
  echo "Could not detect public IP. Set it manually: PUBLIC_IP=x.x.x.x sudo sh $0"
  exit 1
fi

echo "[3/8] Enabling IP forwarding..."
SYSCTL_FILE="/etc/sysctl.d/99-wireguard-forward.conf"
printf '%s\n' 'net.ipv4.ip_forward=1' > "$SYSCTL_FILE"
sysctl -p "$SYSCTL_FILE" >/dev/null

echo "[4/8] Generating keys..."
mkdir -p /etc/wireguard
chmod 700 /etc/wireguard

SERVER_PRIV="/etc/wireguard/server_private.key"
SERVER_PUB="/etc/wireguard/server_public.key"
CLIENT_PRIV="/etc/wireguard/${CLIENT_NAME}_private.key"
CLIENT_PUB="/etc/wireguard/${CLIENT_NAME}_public.key"
PSK_FILE="/etc/wireguard/${CLIENT_NAME}_psk.key"

if [ ! -f "$SERVER_PRIV" ]; then
  wg genkey > "$SERVER_PRIV"
fi
chmod 600 "$SERVER_PRIV"
cat "$SERVER_PRIV" | wg pubkey > "$SERVER_PUB"

if [ ! -f "$CLIENT_PRIV" ]; then
  wg genkey > "$CLIENT_PRIV"
fi
chmod 600 "$CLIENT_PRIV"
cat "$CLIENT_PRIV" | wg pubkey > "$CLIENT_PUB"

if [ ! -f "$PSK_FILE" ]; then
  wg genpsk > "$PSK_FILE"
fi
chmod 600 "$PSK_FILE"

echo "[5/8] Writing server config..."
WG_CONF="/etc/wireguard/${WG_IFACE}.conf"
cat > "$WG_CONF" <<CFG
[Interface]
Address = ${WG_SERVER_IP}
ListenPort = ${WG_PORT}
PrivateKey = $(cat "$SERVER_PRIV")
SaveConfig = true
PostUp = iptables -A FORWARD -i ${WG_IFACE} -j ACCEPT; iptables -A FORWARD -o ${WG_IFACE} -j ACCEPT; iptables -t nat -A POSTROUTING -o ${PUB_IFACE} -j MASQUERADE
PostDown = iptables -D FORWARD -i ${WG_IFACE} -j ACCEPT; iptables -D FORWARD -o ${WG_IFACE} -j ACCEPT; iptables -t nat -D POSTROUTING -o ${PUB_IFACE} -j MASQUERADE

[Peer]
PublicKey = $(cat "$CLIENT_PUB")
PresharedKey = $(cat "$PSK_FILE")
AllowedIPs = ${WG_CLIENT_IP}
CFG
chmod 600 "$WG_CONF"

echo "[6/8] Starting WireGuard..."
systemctl enable "wg-quick@${WG_IFACE}" >/dev/null 2>&1 || true
systemctl restart "wg-quick@${WG_IFACE}"

echo "[7/8] Creating client config..."
CLIENT_CONF="/root/${CLIENT_NAME}.conf"
cat > "$CLIENT_CONF" <<CFG
[Interface]
PrivateKey = $(cat "$CLIENT_PRIV")
Address = ${WG_CLIENT_IP}
DNS = ${DNS_1}, ${DNS_2}

[Peer]
PublicKey = $(cat "$SERVER_PUB")
PresharedKey = $(cat "$PSK_FILE")
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = ${PUBLIC_IP}:${WG_PORT}
PersistentKeepalive = 25
CFG
chmod 600 "$CLIENT_CONF"

echo "[8/8] Opening firewall port if UFW is active..."
if command -v ufw >/dev/null 2>&1; then
  UFW_STATUS="$(ufw status 2>/dev/null | head -n1 || true)"
  case "$UFW_STATUS" in
    *active*) ufw allow "${WG_PORT}/udp" >/dev/null 2>&1 || true ;;
  esac
fi

# Build wireguard:// link (base64 of full config)
if base64 --help >/dev/null 2>&1; then
  WG_B64="$(base64 -w 0 "$CLIENT_CONF" 2>/dev/null || base64 "$CLIENT_CONF" | tr -d '\n')"
else
  WG_B64="$(base64 "$CLIENT_CONF" | tr -d '\n')"
fi
WG_LINK="wireguard://${WG_B64}"

echo ""
echo "Done."
echo "Client config: $CLIENT_CONF"
echo ""
echo "Import link for Happ (if it supports wireguard://):"
echo "$WG_LINK"
echo ""
echo "QR (scan in app):"
qrencode -t ANSIUTF8 < "$CLIENT_CONF"
