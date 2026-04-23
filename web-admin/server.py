#!/usr/bin/env python3
import base64
import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
try:
    import paramiko
except ImportError:  # pragma: no cover
    paramiko = None

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8080"))
BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "panel.db"))).resolve()
STATIC_DIR = BASE_DIR / "static"
SETUP_SCRIPT_PATH = REPO_DIR / "setup-vless-reality.sh"
DEFAULT_REALITY_SNI = os.getenv("DEFAULT_REALITY_SNI", "www.cloudflare.com")
REAL_DEPLOY_ENABLED = os.getenv("REAL_DEPLOY_ENABLED", "1") == "1"
METRICS_POLL_INTERVAL = int(os.getenv("METRICS_POLL_INTERVAL", "15"))
SUBS_SYNC_INTERVAL = int(os.getenv("SUBS_SYNC_INTERVAL", "30"))
HAPP_SUPPORT_URL = os.getenv("HAPP_SUPPORT_URL", "").strip()
HAPP_RENEW_URL = os.getenv("HAPP_RENEW_URL", "").strip()
HAPP_SUB_INFO_TEXT = os.getenv("HAPP_SUB_INFO_TEXT", "").strip()
HAPP_PROFILE_UPDATE_INTERVAL = int(os.getenv("HAPP_PROFILE_UPDATE_INTERVAL", "1"))
DEFAULT_DEVICE_LIMIT = int(os.getenv("DEFAULT_DEVICE_LIMIT", "2"))
PROMO_TELEGRAM_NAME = os.getenv("PROMO_TELEGRAM_NAME", "Наш Telegram").strip()
PROMO_TELEGRAM_URL = os.getenv("PROMO_TELEGRAM_URL", "").strip()
PROMO_SITE_NAME = os.getenv("PROMO_SITE_NAME", "Наш сайт").strip()
PROMO_SITE_URL = os.getenv("PROMO_SITE_URL", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123").strip()
ADMIN_SESSION_COOKIE = os.getenv("ADMIN_SESSION_COOKIE", "vpn_admin_session").strip()
ADMIN_SESSION_TTL = int(os.getenv("ADMIN_SESSION_TTL", "86400"))
ADMIN_COOKIE_SECURE = os.getenv("ADMIN_COOKIE_SECURE", "1") == "1"
BOT_API_TOKEN = os.getenv("BOT_API_TOKEN", "").strip()
BOT_SUB_DAYS = int(os.getenv("BOT_SUB_DAYS", "7"))
BOT_SUB_TRAFFIC_GB = int(os.getenv("BOT_SUB_TRAFFIC_GB", "50"))
BOT_SUB_DEVICE_LIMIT = int(os.getenv("BOT_SUB_DEVICE_LIMIT", "1"))
BOT_SUB_COOLDOWN_DAYS = int(os.getenv("BOT_SUB_COOLDOWN_DAYS", "7"))
VPN_BRAND_NAME = os.getenv("VPN_BRAND_NAME", "BURMALDAA VPN").strip()

DB_LOCK = threading.Lock()
NET_STATE_LOCK = threading.Lock()
NET_STATE: dict[str, tuple[float, float]] = {}
AUTH_SESSIONS_LOCK = threading.Lock()
AUTH_SESSIONS: dict[str, tuple[str, float]] = {}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows}


def _db_needs_schema_reset() -> bool:
    if not DB_PATH.exists():
        return False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            locations_cols = _table_columns(conn, "locations")
            if locations_cols and "code" not in locations_cols:
                return True

            nodes_cols = _table_columns(conn, "nodes")
            if nodes_cols and "location_code" not in nodes_cols:
                return True

            subs_cols = _table_columns(conn, "subscriptions")
            if subs_cols and ("days" not in subs_cols or "traffic_gb" not in subs_cols or "base_url" not in subs_cols):
                return True
    except sqlite3.Error:
        return True
    return False


def _prepare_db_file() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _db_needs_schema_reset():
        return

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = DB_PATH.with_name(f"{DB_PATH.stem}.legacy-{ts}{DB_PATH.suffix}")
    DB_PATH.rename(backup)

    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    shm = DB_PATH.with_name(DB_PATH.name + "-shm")
    if wal.exists():
        wal.rename(backup.with_name(backup.name + "-wal"))
    if shm.exists():
        shm.rename(backup.with_name(backup.name + "-shm"))

    print(f"[db] Old incompatible schema detected. Backup created: {backup}")


class CreateDeploymentRequest(BaseModel):
    location_code: str
    host: str
    port: int = 443
    sni: Optional[str] = None
    ssh_port: int = 22
    ssh_user: str
    ssh_password: str
    node_name: str
    provider: str
    protocol: str = "VLESS"


class CreateLocationRequest(BaseModel):
    code: str
    flag: str
    country: str
    city: str
    region: str


class UpdateLocationRequest(BaseModel):
    flag: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None


class ToggleNodeRequest(BaseModel):
    enabled: bool


class UpdateNodeRequest(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    ip: Optional[str] = None
    protocol: Optional[str] = None
    location_code: Optional[str] = None
    enabled: Optional[bool] = None


class CreateSubscriptionRequest(BaseModel):
    name: str
    protocol: str = "VLESS"
    days: int = Field(ge=1)
    traffic_gb: int = Field(ge=1)
    device_limit: int = Field(default=DEFAULT_DEVICE_LIMIT, ge=1, le=20)
    location_codes: list[str] = Field(min_length=1)
    base_url: Optional[str] = None


class UpdateSubscriptionRequest(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    days: Optional[int] = Field(default=None, ge=1)
    traffic_gb: Optional[int] = Field(default=None, ge=1)
    device_limit: Optional[int] = Field(default=None, ge=1, le=20)
    location_codes: Optional[list[str]] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class BotIssueKeyRequest(BaseModel):
    telegram_user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    base_url: Optional[str] = None


app = FastAPI(title="VPN Panel API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/bot/"):
        return await call_next(request)
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if _get_auth_user(request) is None:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def range_start(range_key: str) -> datetime:
    now = now_dt()
    if range_key == "7d":
        return now - timedelta(days=7)
    if range_key == "30d":
        return now - timedelta(days=30)
    return now - timedelta(hours=24)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    if len(vals) == 1:
        return float(vals[0])
    idx = (len(vals) - 1) * max(0.0, min(1.0, p))
    lo = int(idx)
    hi = min(len(vals) - 1, lo + 1)
    frac = idx - lo
    return float(vals[lo] * (1 - frac) + vals[hi] * frac)


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_conn() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS locations (
              code TEXT PRIMARY KEY,
              flag TEXT NOT NULL,
              country TEXT NOT NULL,
              city TEXT NOT NULL,
              region TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS nodes (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              provider TEXT NOT NULL,
              ip TEXT NOT NULL,
              ssh_port INTEGER,
              ssh_user TEXT,
              ssh_password TEXT,
              protocol TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              location_code TEXT NOT NULL,
              vless_uuid TEXT,
              reality_public_key TEXT,
              reality_short_id TEXT,
              reality_sni TEXT,
              reality_port INTEGER,
              last_cpu REAL NOT NULL DEFAULT 20,
              last_ram REAL NOT NULL DEFAULT 35,
              last_mbps REAL NOT NULL DEFAULT 80,
              last_load REAL NOT NULL DEFAULT 30,
              last_active_conns INTEGER NOT NULL DEFAULT 0,
              last_latency_ms REAL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(location_code) REFERENCES locations(code) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS deployments (
              id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              progress INTEGER NOT NULL DEFAULT 0,
              message TEXT,
              error TEXT,
              payload_json TEXT NOT NULL,
              node_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(node_id) REFERENCES nodes(id)
            );

            CREATE TABLE IF NOT EXISTS deployment_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              deployment_id TEXT NOT NULL,
              seq INTEGER NOT NULL,
              event_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(deployment_id) REFERENCES deployments(id) ON DELETE CASCADE,
              UNIQUE(deployment_id, seq)
            );

            CREATE TABLE IF NOT EXISTS node_metrics_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              node_id TEXT NOT NULL,
              cpu REAL NOT NULL,
              ram REAL NOT NULL,
              mbps REAL NOT NULL,
              load REAL NOT NULL,
              latency_ms REAL,
              xray_active INTEGER NOT NULL,
              collected_at TEXT NOT NULL,
              FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS node_command_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              node_id TEXT NOT NULL,
              phase TEXT NOT NULL,
              command TEXT NOT NULL,
              exit_code INTEGER,
              stdout TEXT,
              stderr TEXT,
              created_at TEXT NOT NULL,
              FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              protocol TEXT NOT NULL,
              days INTEGER NOT NULL,
              traffic_gb INTEGER NOT NULL,
              device_limit INTEGER NOT NULL DEFAULT 2,
              used_gb REAL NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'active',
              token TEXT NOT NULL UNIQUE,
              client_uuid TEXT,
              base_url TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscription_locations (
              subscription_id TEXT NOT NULL,
              location_code TEXT NOT NULL,
              PRIMARY KEY (subscription_id, location_code),
              FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
              FOREIGN KEY(location_code) REFERENCES locations(code) ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS subscription_pulls (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subscription_id TEXT NOT NULL,
              remote_ip TEXT,
              user_agent TEXT,
              pulled_at TEXT NOT NULL,
              FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS subscription_devices (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subscription_id TEXT NOT NULL,
              device_key TEXT NOT NULL,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              last_ip TEXT,
              last_user_agent TEXT,
              blocked INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
              UNIQUE(subscription_id, device_key)
            );

            CREATE TABLE IF NOT EXISTS bot_key_issues (
              telegram_user_id TEXT PRIMARY KEY,
              subscription_id TEXT,
              issued_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
            );
            """
        )


def migrate_db() -> None:
    with db_conn() as conn:
        node_cols = _table_columns(conn, "nodes")
        for col, ddl in [
            ("ssh_port", "INTEGER"),
            ("ssh_user", "TEXT"),
            ("ssh_password", "TEXT"),
            ("vless_uuid", "TEXT"),
            ("reality_public_key", "TEXT"),
            ("reality_short_id", "TEXT"),
            ("reality_sni", "TEXT"),
            ("reality_port", "INTEGER"),
            ("last_active_conns", "INTEGER NOT NULL DEFAULT 0"),
            ("last_latency_ms", "REAL"),
        ]:
            if col not in node_cols:
                conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {ddl}")

        sub_cols = _table_columns(conn, "subscriptions")
        if "client_uuid" not in sub_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN client_uuid TEXT")
        if "device_limit" not in sub_cols:
            conn.execute(f"ALTER TABLE subscriptions ADD COLUMN device_limit INTEGER NOT NULL DEFAULT {DEFAULT_DEVICE_LIMIT}")
        rows = conn.execute("SELECT id, client_uuid FROM subscriptions").fetchall()
        for r in rows:
            cur = str(r["client_uuid"] or "").strip()
            if not re.match(r"^[0-9a-fA-F-]{32,36}$", cur):
                conn.execute(
                    "UPDATE subscriptions SET client_uuid = ? WHERE id = ?",
                    (str(uuid.uuid4()), r["id"]),
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_devices (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subscription_id TEXT NOT NULL,
              device_key TEXT NOT NULL,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              last_ip TEXT,
              last_user_agent TEXT,
              blocked INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE,
              UNIQUE(subscription_id, device_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_key_issues (
              telegram_user_id TEXT PRIMARY KEY,
              subscription_id TEXT,
              issued_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
            )
            """
        )


def seed_data() -> None:
    now = utc_now()
    defaults_locations = [
        ("DE", "🇩🇪", "Германия", "Франкфурт", "eu-central-1"),
        ("NL", "🇳🇱", "Нидерланды", "Амстердам", "eu-west-1"),
        ("US", "🇺🇸", "США", "Нью-Йорк", "us-east-1"),
    ]
    defaults_nodes = [
        ("node-de-fra-01", "DE-FRA-01", "Hetzner", "198.51.100.12", "VLESS", 1, "DE", 42, 51, 210, 48),
        ("node-nl-ams-01", "NL-AMS-01", "DigitalOcean", "203.0.113.45", "VLESS", 1, "NL", 38, 46, 185, 43),
        ("node-us-ny-01", "US-NY-01", "AWS", "192.0.2.110", "VLESS", 0, "US", 9, 14, 5, 8),
    ]

    with db_conn() as conn:
        if conn.execute("SELECT COUNT(*) c FROM locations").fetchone()["c"] == 0:
            for code, flag, country, city, region in defaults_locations:
                conn.execute(
                    "INSERT INTO locations(code, flag, country, city, region, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (code, flag, country, city, region, now, now),
                )

        if conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"] == 0:
            for item in defaults_nodes:
                conn.execute(
                    """
                    INSERT INTO nodes(id, name, provider, ip, protocol, enabled, location_code,
                                      last_cpu, last_ram, last_mbps, last_load, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*item, now, now),
                )


def ensure_location_exists(code: str) -> None:
    code = code.upper().strip()
    if not code:
        return
    with db_conn() as conn:
        row = conn.execute("SELECT code FROM locations WHERE code = ?", (code,)).fetchone()
        if row:
            return
        now = utc_now()
        conn.execute(
            "INSERT INTO locations(code, flag, country, city, region, created_at, updated_at) VALUES(?, '🌐', ?, 'Auto', 'edge', ?, ?)",
            (code, code, now, now),
        )


def node_health(row: dict[str, Any]) -> str:
    if not row.get("enabled"):
        return "offline"
    if float(row.get("last_load", 0) or 0) >= 88:
        return "problem"
    return "online"


def location_list() -> list[dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT l.*,
                   COUNT(n.id) AS node_count,
                   SUM(CASE WHEN n.enabled = 1 THEN 1 ELSE 0 END) AS active_node_count
            FROM locations l
            LEFT JOIN nodes n ON n.location_code = l.code
            GROUP BY l.code
            ORDER BY l.country, l.city, l.code
            """
        ).fetchall()
    return [dict(r) for r in rows]


def node_list(location_code: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    q = "SELECT * FROM nodes"
    args: list[Any] = []
    if location_code:
        q += " WHERE location_code = ?"
        args.append(location_code.upper())
    q += " ORDER BY name"

    with db_conn() as conn:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]

    for r in rows:
        r.pop("ssh_password", None)
        r.pop("ssh_user", None)
        r.pop("ssh_port", None)
        r["enabled"] = bool(r["enabled"])
        r["health"] = node_health(r)

    if status:
        rows = [r for r in rows if r["health"] == status]
    return rows


def deployment_get(dep_id: str) -> dict[str, Any] | None:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM deployments WHERE id = ?", (dep_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    return item


def deployment_push_event(dep_id: str, payload: dict[str, Any]) -> None:
    with DB_LOCK:
        with db_conn() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM deployment_events WHERE deployment_id = ?",
                (dep_id,),
            ).fetchone()["next_seq"]
            conn.execute(
                "INSERT INTO deployment_events(deployment_id, seq, event_json, created_at) VALUES(?, ?, ?, ?)",
                (dep_id, seq, json.dumps(payload, ensure_ascii=False), utc_now()),
            )


def deployment_set_state(dep_id: str, status: str, progress: int, message: str, error: str | None = None, node_id: str | None = None) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE deployments
            SET status = ?, progress = ?, message = ?, error = ?, node_id = COALESCE(?, node_id), updated_at = ?
            WHERE id = ?
            """,
            (status, int(progress), message, error, node_id, utc_now(), dep_id),
        )


def _remote_exec_stream(client, command: str, on_line) -> int:
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("SSH transport is not available")
    chan = transport.open_session()
    chan.get_pty()
    chan.exec_command(command)

    buf = ""
    while True:
        if chan.recv_ready():
            chunk = chan.recv(4096).decode("utf-8", errors="replace")
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                on_line(line.rstrip("\r"))

        if chan.recv_stderr_ready():
            chunk = chan.recv_stderr(4096).decode("utf-8", errors="replace")
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                on_line(line.rstrip("\r"))

        if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
            break
        time.sleep(0.05)

    if buf.strip():
        on_line(buf.strip())
    return int(chan.recv_exit_status())


def _extract_reality_values(output_text: str) -> dict[str, Any]:
    link_match = re.search(r"(vless://[^\s]+)", output_text)
    if not link_match:
        raise RuntimeError("Не найдена VLESS ссылка в выводе установочного скрипта")

    link = link_match.group(1).strip()
    if "?" not in link or "@" not in link:
        raise RuntimeError("Некорректный формат VLESS ссылки")

    uuid_part = link.split("://", 1)[1].split("@", 1)[0]
    host_port = link.split("@", 1)[1].split("?", 1)[0]
    query = link.split("?", 1)[1].split("#", 1)[0]

    host = host_port.rsplit(":", 1)[0]
    port = int(host_port.rsplit(":", 1)[1])
    params = {}
    for kv in query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = v

    pbk = params.get("pbk", "")
    sid = params.get("sid", "")
    sni = params.get("sni", DEFAULT_REALITY_SNI)
    if not pbk or not sid:
        raise RuntimeError("Не удалось извлечь pbk/sid из ссылки")

    return {
        "link": link,
        "vless_uuid": uuid_part,
        "host": host,
        "port": port,
        "reality_public_key": pbk,
        "reality_short_id": sid,
        "reality_sni": sni,
    }


def _run_real_deploy(payload: dict[str, Any], on_log) -> dict[str, Any]:
    if paramiko is None:
        raise RuntimeError("paramiko не установлен. Установите: pip install paramiko")
    if not SETUP_SCRIPT_PATH.exists():
        raise RuntimeError(f"Не найден setup-скрипт: {SETUP_SCRIPT_PATH}")

    host = str(payload.get("host", "")).strip()
    ssh_port = int(payload.get("ssh_port", 22))
    ssh_user = str(payload.get("ssh_user", "")).strip()
    ssh_password = str(payload.get("ssh_password", ""))
    node_name = str(payload.get("node_name") or "NODE").strip()
    sni = str(payload.get("sni") or DEFAULT_REALITY_SNI).strip()
    port = int(payload.get("port") or 443)

    if not host or not ssh_user or not ssh_password:
        raise RuntimeError("Для деплоя требуются host/ssh_user/ssh_password")
    if ssh_user != "root":
        raise RuntimeError("Текущий setup-скрипт требует ssh_user=root")

    def sq(value: Any) -> str:
        return "'" + str(value).replace("'", "'\"'\"'") + "'"

    on_log("SSH: подключение к серверу...", "info")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=ssh_port,
        username=ssh_user,
        password=ssh_password,
        timeout=15,
        banner_timeout=20,
        auth_timeout=20,
    )
    try:
        sftp = client.open_sftp()
        remote_script = "/tmp/setup-vless-reality.sh"
        sftp.put(str(SETUP_SCRIPT_PATH), remote_script)
        sftp.close()
        on_log("SSH: setup-скрипт загружен", "info")

        _remote_exec_stream(client, f"chmod +x {remote_script}", lambda line: None)
        cmd = (
            f"PORT={sq(port)} SNI={sq(sni)} CLIENT_NAME={sq(node_name)} PUBLIC_IP={sq(host)} "
            f"bash {sq(remote_script)}"
        )
        output_lines: list[str] = []

        def push_line(line: str) -> None:
            if not line:
                return
            output_lines.append(line)
            lvl = "info"
            low = line.lower()
            if "error" in low or "failed" in low or "fail" in low:
                lvl = "error"
            elif "warning" in low:
                lvl = "warn"
            elif "complete" in low or "ok" in low:
                lvl = "success"
            on_log(line, lvl)

        code = _remote_exec_stream(client, cmd, push_line)
        if code != 0:
            raise RuntimeError(f"Удаленный setup-скрипт завершился с кодом {code}")

        raw = "\n".join(output_lines)
        return _extract_reality_values(raw)
    finally:
        client.close()


def _open_ssh_client(host: str, port: int, user: str, password: str, timeout: int = 12):
    if paramiko is None:
        raise RuntimeError("paramiko не установлен. Установите: pip install paramiko")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=int(port),
        username=user,
        password=password,
        timeout=timeout,
        banner_timeout=max(15, timeout),
        auth_timeout=max(15, timeout),
    )
    return client


def _exec_capture(client, command: str, timeout: int = 20) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    code = int(stdout.channel.recv_exit_status())
    return code, out, err


def _sh_quote(value: Any) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _header_safe(value: Any) -> str:
    # HTTP headers in Starlette must be latin-1 encodable.
    return str(value).encode("latin-1", "ignore").decode("latin-1")


def _create_auth_session(username: str) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    expires_ts = int(time.time() + max(300, ADMIN_SESSION_TTL))
    with AUTH_SESSIONS_LOCK:
        AUTH_SESSIONS[token] = (username, float(expires_ts))
    return token, expires_ts


def _get_auth_user(request: Request) -> Optional[str]:
    token = str(request.cookies.get(ADMIN_SESSION_COOKIE, "")).strip()
    if not token:
        return None
    with AUTH_SESSIONS_LOCK:
        info = AUTH_SESSIONS.get(token)
        if not info:
            return None
        username, expires_ts = info
        if float(expires_ts) <= time.time():
            AUTH_SESSIONS.pop(token, None)
            return None
        return username


def _drop_auth_session(request: Request) -> None:
    token = str(request.cookies.get(ADMIN_SESSION_COOKIE, "")).strip()
    if not token:
        return
    with AUTH_SESSIONS_LOCK:
        AUTH_SESSIONS.pop(token, None)


def _bot_token_valid(token: str | None) -> bool:
    candidate = str(token or "").strip()
    if not BOT_API_TOKEN or not candidate:
        return False
    return secrets.compare_digest(candidate, BOT_API_TOKEN)


def _active_vless_location_codes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT n.location_code
        FROM nodes n
        WHERE n.enabled = 1
          AND TRIM(COALESCE(n.reality_public_key, '')) <> ''
          AND TRIM(COALESCE(n.reality_short_id, '')) <> ''
        ORDER BY n.location_code
        """
    ).fetchall()
    return [str(r["location_code"]).upper().strip() for r in rows if str(r["location_code"]).strip()]


def _device_fingerprint(request: Optional[Request], fallback_token: str) -> str:
    if request is None:
        return f"fallback:{fallback_token}"
    headers = request.headers
    for key in ("x-hwid", "x-device-id", "x-client-id", "x-udid", "happ-hwid", "device-id"):
        value = str(headers.get(key, "")).strip()
        if value:
            return f"{key}:{value[:256]}"
    qval = str(request.query_params.get("device_id", "")).strip()
    if qval:
        return f"query:{qval[:256]}"
    ua = str(headers.get("user-agent", "")).strip()
    return f"ua:{ua[:256] if ua else fallback_token}"


def _promo_links(client_uuid: str) -> list[str]:
    if not client_uuid:
        return []
    links: list[str] = []
    if PROMO_TELEGRAM_NAME and PROMO_TELEGRAM_URL:
        params = (
            "encryption=none&security=tls&sni=invalid.local"
            "&type=tcp&headerType=none"
        )
        remark = quote(f"{PROMO_TELEGRAM_NAME} | {PROMO_TELEGRAM_URL}", safe="")
        links.append(f"vless://{client_uuid}@0.0.0.0:1?{params}#{remark}")
    if PROMO_SITE_NAME and PROMO_SITE_URL:
        params = (
            "encryption=none&security=tls&sni=invalid.local"
            "&type=tcp&headerType=none"
        )
        remark = quote(f"{PROMO_SITE_NAME} | {PROMO_SITE_URL}", safe="")
        links.append(f"vless://{client_uuid}@0.0.0.0:1?{params}#{remark}")
    return links


def _node_from_db(node_id: str) -> Optional[dict[str, Any]]:
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def _log_node_command(node_id: str, phase: str, command: str, exit_code: Optional[int], stdout: str, stderr: str) -> None:
    out = (stdout or "")[:16000]
    err = (stderr or "")[:16000]
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO node_command_logs(node_id, phase, command, exit_code, stdout, stderr, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (node_id, phase, command, exit_code, out, err, utc_now()),
        )


def _has_ssh(node: dict[str, Any]) -> bool:
    return bool(str(node.get("ssh_user") or "").strip() and str(node.get("ssh_password") or "").strip() and str(node.get("ip") or "").strip())


def _collect_remote_metrics(node: dict[str, Any]) -> dict[str, Any]:
    host = str(node.get("ip") or "").strip()
    ssh_port = int(node.get("ssh_port") or 22)
    ssh_user = str(node.get("ssh_user") or "").strip()
    ssh_password = str(node.get("ssh_password") or "")
    if not host or not ssh_user or not ssh_password:
        raise RuntimeError("SSH credentials are not set for node")

    reality_port = int(node.get("reality_port") or 443)
    cmd = (
        "sh -lc '"
        f"P={reality_port}; "
        "N=$(nproc 2>/dev/null || echo 1); "
        "L=$(awk \"{print \\$1}\" /proc/loadavg 2>/dev/null || echo 0); "
        "CPU=$(awk -v l=$L -v n=$N \"BEGIN{v=(l/n)*100; if(v>100)v=100; if(v<0)v=0; printf \\\"%.2f\\\", v}\"); "
        "RAM=$(free 2>/dev/null | awk \"/Mem:/ {if(\\$2>0) printf \\\"%.2f\\\", (\\$3/\\$2)*100; else printf \\\"0\\\"}\"); "
        "S=0; "
        "for P in /sys/class/net/*; do "
        "  IF=$(basename \"$P\"); "
        "  [ \"$IF\" = \"lo\" ] && continue; "
        "  RX=$(cat \"$P/statistics/rx_bytes\" 2>/dev/null || echo 0); "
        "  TX=$(cat \"$P/statistics/tx_bytes\" 2>/dev/null || echo 0); "
        "  S=$((S + RX + TX)); "
        "done; "
        "RXTX=$S; "
        "CONN=$(ss -Hnt state established \"( sport = :$P )\" 2>/dev/null | wc -l | tr -d \"[:space:]\" 2>/dev/null); "
        "[ -n \"$CONN\" ] || CONN=0; "
        "systemctl is-active xray >/dev/null 2>&1 && XR=1 || XR=0; "
        "echo \"$CPU $RAM $RXTX $XR $CONN\"'"
    )

    client = _open_ssh_client(host, ssh_port, ssh_user, ssh_password, timeout=12)
    try:
        code, out, err = _exec_capture(client, cmd, timeout=25)
        _log_node_command(node["id"], "metrics", cmd, code, out, err)
        if code != 0:
            raise RuntimeError(err or f"remote metrics command failed ({code})")
    finally:
        client.close()

    parts = out.split()
    if len(parts) < 5:
        raise RuntimeError(f"unexpected metrics payload: {out!r}")

    cpu = float(parts[0])
    ram = float(parts[1])
    total_bytes = float(parts[2])
    xray_active = parts[3] == "1"
    active_conns = int(float(parts[4] or 0))

    now_ts = time.time()
    mbps = 0.0
    with NET_STATE_LOCK:
        prev = NET_STATE.get(node["id"])
        if prev:
            prev_bytes, prev_ts = prev
            dt = max(0.001, now_ts - prev_ts)
            delta = max(0.0, total_bytes - prev_bytes)
            mbps = (delta * 8.0) / dt / 1_000_000.0
        NET_STATE[node["id"]] = (total_bytes, now_ts)

    load = max(0.0, min(100.0, max(cpu, ram * 0.85)))
    lat_started = time.time()
    latency_ms = None
    try:
        port = int(node.get("reality_port") or 443)
        with socket.create_connection((host, port), timeout=2.0):
            latency_ms = max(0.1, (time.time() - lat_started) * 1000.0)
    except Exception:
        latency_ms = None

    return {
        "cpu": round(cpu, 2),
        "ram": round(ram, 2),
        "mbps": round(mbps, 2),
        "load": round(load, 2),
        "active_conns": max(0, active_conns),
        "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
        "xray_active": xray_active,
    }


def _update_node_metrics(node_id: str, metrics: dict[str, Any]) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE nodes
            SET last_cpu = ?, last_ram = ?, last_mbps = ?, last_load = ?, last_active_conns = ?, last_latency_ms = ?,
                enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                metrics["cpu"],
                metrics["ram"],
                metrics["mbps"],
                metrics["load"],
                int(metrics.get("active_conns") or 0),
                metrics.get("latency_ms"),
                1 if metrics["xray_active"] else 0,
                utc_now(),
                node_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO node_metrics_history(node_id, cpu, ram, mbps, load, latency_ms, xray_active, collected_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                metrics["cpu"],
                metrics["ram"],
                metrics["mbps"],
                metrics["load"],
                metrics.get("latency_ms"),
                1 if metrics["xray_active"] else 0,
                utc_now(),
            ),
        )


def _set_node_service_state(node: dict[str, Any], enabled: bool) -> None:
    host = str(node.get("ip") or "").strip()
    ssh_port = int(node.get("ssh_port") or 22)
    ssh_user = str(node.get("ssh_user") or "").strip()
    ssh_password = str(node.get("ssh_password") or "")
    if not host or not ssh_user or not ssh_password:
        raise RuntimeError("SSH credentials are not set for node")

    cmd = "systemctl start xray" if enabled else "systemctl stop xray"
    check = "systemctl is-active xray"
    client = _open_ssh_client(host, ssh_port, ssh_user, ssh_password, timeout=12)
    try:
        code, _, err = _exec_capture(client, cmd, timeout=20)
        _log_node_command(node["id"], "toggle", cmd, code, "", err)
        if code != 0:
            raise RuntimeError(err or f"failed to execute: {cmd}")
        code2, out2, _ = _exec_capture(client, check, timeout=10)
        _log_node_command(node["id"], "toggle-check", check, code2, out2, "")
        active = (code2 == 0 and out2.strip() == "active")
        if enabled and not active:
            raise RuntimeError("xray service did not start")
        if not enabled and active:
            raise RuntimeError("xray service did not stop")
    finally:
        client.close()


def _restart_node_service(node: dict[str, Any]) -> None:
    host = str(node.get("ip") or "").strip()
    ssh_port = int(node.get("ssh_port") or 22)
    ssh_user = str(node.get("ssh_user") or "").strip()
    ssh_password = str(node.get("ssh_password") or "")
    if not host or not ssh_user or not ssh_password:
        raise RuntimeError("SSH credentials are not set for node")

    client = _open_ssh_client(host, ssh_port, ssh_user, ssh_password, timeout=12)
    try:
        cmd = "systemctl restart xray"
        code, out, err = _exec_capture(client, cmd, timeout=25)
        _log_node_command(node["id"], "restart", cmd, code, out, err)
        if code != 0:
            raise RuntimeError(err or "failed to restart xray")
        code2, out2, _ = _exec_capture(client, "systemctl is-active xray", timeout=10)
        _log_node_command(node["id"], "restart-check", "systemctl is-active xray", code2, out2, "")
        if not (code2 == 0 and out2.strip() == "active"):
            raise RuntimeError("xray service is not active after restart")
    finally:
        client.close()


def _active_client_uuids_for_location(location_code: str) -> list[str]:
    now = utc_now()
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.client_uuid
            FROM subscriptions s
            JOIN subscription_locations sl ON sl.subscription_id = s.id
            WHERE sl.location_code = ?
              AND s.status = 'active'
              AND s.expires_at > ?
              AND s.used_gb < s.traffic_gb
              AND s.client_uuid IS NOT NULL
              AND TRIM(s.client_uuid) <> ''
            ORDER BY s.created_at ASC
            """,
            (location_code, now),
        ).fetchall()
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        value = str(r["client_uuid"]).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _sync_node_clients(node: dict[str, Any], uuids: list[str], reason: str = "sync") -> None:
    host = str(node.get("ip") or "").strip()
    ssh_port = int(node.get("ssh_port") or 22)
    ssh_user = str(node.get("ssh_user") or "").strip()
    ssh_password = str(node.get("ssh_password") or "")
    if not host or not ssh_user or not ssh_password:
        raise RuntimeError("SSH credentials are not set for node")

    clients = [{"id": u, "email": f"sub-{u[:8]}"} for u in uuids]
    payload_b64 = base64.b64encode(json.dumps(clients, ensure_ascii=False).encode("utf-8")).decode("ascii")
    payload_quoted = _sh_quote(payload_b64)
    script = (
        f"python3 - {payload_quoted} <<'PY'\n"
        "import base64, json, os, sys\n"
        "path = \"/usr/local/etc/xray/config.json\"\n"
        "arg = sys.argv[1] if len(sys.argv) > 1 else \"\"\n"
        "clients = json.loads(base64.b64decode(arg.encode(\"ascii\")).decode(\"utf-8\"))\n"
        "with open(path, \"r\", encoding=\"utf-8\") as f:\n"
        "    cfg = json.load(f)\n"
        "changed = False\n"
        "for inbound in cfg.get(\"inbounds\", []):\n"
        "    if str(inbound.get(\"protocol\", \"\")).lower() != \"vless\":\n"
        "        continue\n"
        "    settings = inbound.setdefault(\"settings\", {})\n"
        "    settings[\"decryption\"] = \"none\"\n"
        "    settings[\"clients\"] = clients\n"
        "    changed = True\n"
        "if not changed:\n"
        "    raise SystemExit(\"vless inbound not found\")\n"
        "tmp = path + \".tmp\"\n"
        "with open(tmp, \"w\", encoding=\"utf-8\") as f:\n"
        "    json.dump(cfg, f, ensure_ascii=False, indent=2)\n"
        "os.replace(tmp, path)\n"
        "PY\n"
        "/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json\n"
        "systemctl restart xray\n"
        "systemctl is-active xray\n"
    )
    cmd = f"sh -lc {_sh_quote(script)}"

    client = _open_ssh_client(host, ssh_port, ssh_user, ssh_password, timeout=14)
    try:
        code, out, err = _exec_capture(client, cmd, timeout=50)
        _log_node_command(node["id"], f"subs-sync-{reason}", cmd, code, out, err)
        if code != 0:
            raise RuntimeError(err or out or "failed to sync clients")
    finally:
        client.close()


def _sync_locations_clients(location_codes: list[str], reason: str = "sync") -> None:
    codes = sorted({str(c).upper().strip() for c in location_codes if str(c).strip()})
    if not codes:
        return
    placeholders = ",".join(["?"] * len(codes))
    with db_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE location_code IN ({placeholders})",
            codes,
        ).fetchall()
    errors: list[str] = []
    for r in rows:
        node = dict(r)
        if not _has_ssh(node):
            continue
        try:
            uuids = _active_client_uuids_for_location(str(node.get("location_code") or "").upper())
            _sync_node_clients(node, uuids, reason=reason)
        except Exception as exc:
            _log_node_command(node["id"], f"subs-sync-{reason}-error", "sync-clients", None, "", str(exc))
            errors.append(f"{node.get('name') or node.get('id')}: {exc}")
    if errors:
        raise RuntimeError("; ".join(errors))


def _sync_all_nodes_clients(reason: str = "all") -> None:
    with db_conn() as conn:
        codes = [r["location_code"] for r in conn.execute("SELECT DISTINCT location_code FROM nodes").fetchall()]
    _sync_locations_clients(codes, reason=reason)


def _refresh_bot_subscriptions_locations(reason: str = "bot-auto-locations") -> None:
    with db_conn() as conn:
        codes = _active_vless_location_codes(conn)
        if not codes:
            return
        bot_sub_ids = [
            r["subscription_id"]
            for r in conn.execute(
                """
                SELECT DISTINCT b.subscription_id
                FROM bot_key_issues b
                JOIN subscriptions s ON s.id = b.subscription_id
                WHERE b.subscription_id IS NOT NULL
                """
            ).fetchall()
        ]
        if not bot_sub_ids:
            return

        placeholders = ",".join(["?"] * len(bot_sub_ids))
        old_codes = {
            r["location_code"]
            for r in conn.execute(
                f"SELECT DISTINCT location_code FROM subscription_locations WHERE subscription_id IN ({placeholders})",
                bot_sub_ids,
            ).fetchall()
        }

        for sid in bot_sub_ids:
            conn.execute("DELETE FROM subscription_locations WHERE subscription_id = ?", (sid,))
            for code in codes:
                conn.execute("INSERT INTO subscription_locations(subscription_id, location_code) VALUES(?, ?)", (sid, code))
            conn.execute("UPDATE subscriptions SET updated_at = ? WHERE id = ?", (utc_now(), sid))

    affected_codes = sorted(old_codes.union(codes))
    if affected_codes:
        _sync_locations_clients(affected_codes, reason=reason)


def _enforce_subscription_limits_loop() -> None:
    while True:
        try:
            now = utc_now()
            with db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT s.id, sl.location_code
                    FROM subscriptions s
                    JOIN subscription_locations sl ON sl.subscription_id = s.id
                    WHERE s.status = 'active'
                      AND (s.expires_at <= ? OR s.used_gb >= s.traffic_gb)
                    """,
                    (now,),
                ).fetchall()
                if rows:
                    sub_ids = sorted({r["id"] for r in rows})
                    loc_codes = sorted({r["location_code"] for r in rows})
                    placeholders = ",".join(["?"] * len(sub_ids))
                    conn.execute(
                        f"UPDATE subscriptions SET status = 'revoked', updated_at = ? WHERE id IN ({placeholders})",
                        (now, *sub_ids),
                    )
                else:
                    loc_codes = []
            if loc_codes:
                _sync_locations_clients(loc_codes, reason="limits")
        except Exception:
            pass
        time.sleep(max(10, SUBS_SYNC_INTERVAL))


def _metrics_collector_loop() -> None:
    while True:
        try:
            with db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM nodes
                    WHERE ssh_user IS NOT NULL AND TRIM(ssh_user) <> ''
                      AND ssh_password IS NOT NULL AND TRIM(ssh_password) <> ''
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            for r in rows:
                node = dict(r)
                try:
                    metrics = _collect_remote_metrics(node)
                    _update_node_metrics(node["id"], metrics)
                except Exception as exc:
                    _log_node_command(node["id"], "metrics-error", "collect", None, "", str(exc))
                    with db_conn() as conn:
                        conn.execute(
                            """
                            UPDATE nodes
                            SET enabled = 0, last_mbps = 0, updated_at = ?
                            WHERE id = ?
                            """,
                            (utc_now(), node["id"]),
                        )
        except Exception:
            pass
        time.sleep(max(5, METRICS_POLL_INTERVAL))


def start_metrics_collector() -> None:
    th = threading.Thread(target=_metrics_collector_loop, daemon=True, name="metrics-collector")
    th.start()


def start_subscriptions_enforcer() -> None:
    th = threading.Thread(target=_enforce_subscription_limits_loop, daemon=True, name="subscriptions-enforcer")
    th.start()


def deployment_worker(dep_id: str, payload: dict[str, Any]) -> None:
    try:
        deployment_set_state(dep_id, "running", 0, "Запуск")
        deployment_push_event(dep_id, {"status": "running", "progress": 0, "message": "Задача запущена", "level": "info"})
        progress_state = {"value": 3}

        def log(msg: str, level: str = "info") -> None:
            if progress_state["value"] < 96:
                progress_state["value"] += 1
            deployment_set_state(dep_id, "running", progress_state["value"], msg)
            deployment_push_event(
                dep_id,
                {"status": "running", "progress": progress_state["value"], "message": msg, "level": level},
            )

        if REAL_DEPLOY_ENABLED:
            reality = _run_real_deploy(payload, log)
            host_ip = reality["host"]
            reality_port = int(reality["port"])
            vless_uuid = reality["vless_uuid"]
            reality_public_key = reality["reality_public_key"]
            reality_short_id = reality["reality_short_id"]
            reality_sni = reality["reality_sni"]
        else:
            # fallback simulation for local debug
            steps = [
                (12, "Проверка SSH-доступа"),
                (26, "Обновление пакетов"),
                (41, "Установка Xray"),
                (58, "Генерация ключей VLESS/REALITY"),
                (78, "Запись конфигурации и systemd"),
                (91, "Проверка сервиса"),
            ]
            for progress, message in steps:
                deployment_set_state(dep_id, "running", progress, message)
                deployment_push_event(dep_id, {"status": "running", "progress": progress, "message": message, "level": "info"})
                time.sleep(0.9)
            host_ip = str(payload.get("host") or "0.0.0.0").strip()
            reality_port = int(payload.get("port") or 443)
            vless_uuid = str(uuid.uuid4())
            reality_public_key = "demo_public_key"
            reality_short_id = "abcd1234"
            reality_sni = str(payload.get("sni") or DEFAULT_REALITY_SNI)

        loc_code = str(payload.get("location_code", "OT")).upper()
        ensure_location_exists(loc_code)

        node_id = "node-" + uuid.uuid4().hex[:10]
        node = {
            "id": node_id,
            "name": str(payload.get("node_name") or (loc_code + "-NODE")).strip(),
            "provider": str(payload.get("provider") or "VPS").strip(),
            "ip": host_ip,
            "ssh_port": int(payload.get("ssh_port") or 22),
            "ssh_user": str(payload.get("ssh_user") or "").strip(),
            "ssh_password": str(payload.get("ssh_password") or ""),
            "protocol": str(payload.get("protocol") or "VLESS").upper().strip(),
            "enabled": True,
            "location_code": loc_code,
            "vless_uuid": vless_uuid,
            "reality_public_key": reality_public_key,
            "reality_short_id": reality_short_id,
            "reality_sni": reality_sni,
            "reality_port": reality_port,
            "last_cpu": 22.0,
            "last_ram": 37.0,
            "last_mbps": 65.0,
            "last_load": 31.0,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }

        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO nodes(id, name, provider, ip, ssh_port, ssh_user, ssh_password, protocol, enabled, location_code,
                                  vless_uuid, reality_public_key, reality_short_id, reality_sni, reality_port,
                                  last_cpu, last_ram, last_mbps, last_load, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node["id"],
                    node["name"],
                    node["provider"],
                    node["ip"],
                    node["ssh_port"],
                    node["ssh_user"],
                    node["ssh_password"],
                    node["protocol"],
                    1,
                    node["location_code"],
                    node["vless_uuid"],
                    node["reality_public_key"],
                    node["reality_short_id"],
                    node["reality_sni"],
                    node["reality_port"],
                    node["last_cpu"],
                    node["last_ram"],
                    node["last_mbps"],
                    node["last_load"],
                    node["created_at"],
                    node["updated_at"],
                ),
            )

        try:
            _sync_locations_clients([loc_code], reason="deploy")
        except Exception as exc:
            log(f"Предупреждение: не удалось синхронизировать клиентов на ноде: {exc}", "warn")
        try:
            _refresh_bot_subscriptions_locations(reason="deploy-bot-autolocations")
        except Exception as exc:
            log(f"Предупреждение: не удалось обновить telegram-подписки: {exc}", "warn")

        deployment_set_state(dep_id, "success", 100, "Развертывание завершено", node_id=node_id)
        deployment_push_event(
            dep_id,
            {
                "status": "success",
                "progress": 100,
                "message": "Развертывание завершено",
                "level": "success",
                "node": {
                    "id": node["id"],
                    "name": node["name"],
                    "provider": node["provider"],
                    "ip": node["ip"],
                    "protocol": node["protocol"],
                    "location_code": node["location_code"],
                    "enabled": node["enabled"],
                    "reality_sni": node["reality_sni"],
                    "reality_port": node["reality_port"],
                },
            },
        )
    except Exception as exc:  # pragma: no cover
        err = str(exc)
        deployment_set_state(dep_id, "failed", 100, "Развертывание завершилось ошибкой", error=err)
        deployment_push_event(
            dep_id,
            {"status": "failed", "progress": 100, "message": "Развертывание завершилось ошибкой", "error": err, "level": "error"},
        )


def get_subscription_links_payload(sub: dict[str, Any], location_codes: list[str]) -> tuple[str, str]:
    base_url = str(sub["base_url"]).rstrip("/")
    subscription_url = (
        f"{base_url}/sub/{sub['id']}"
        f"?token={quote(sub['token'])}"
    )
    # Happ compatibility: avoid query-string in the primary share URL.
    # Many clients handle /sub/{token} more consistently than ?token=...
    happ_url = f"{base_url}/sub/{quote(sub['token'])}"
    return subscription_url, happ_url


def get_subscription(sub_id: str) -> Optional[dict[str, Any]]:
    with db_conn() as conn:
        srow = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone()
        if not srow:
            return None
        lrows = conn.execute(
            "SELECT location_code FROM subscription_locations WHERE subscription_id = ? ORDER BY location_code",
            (sub_id,),
        ).fetchall()

    sub = dict(srow)
    sub["location_codes"] = [r["location_code"] for r in lrows]
    sub_url, happ_url = get_subscription_links_payload(sub, sub["location_codes"])
    sub["subscription_url"] = sub_url
    sub["happ_url"] = happ_url
    return sub


def list_subscriptions() -> list[dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute("SELECT id FROM subscriptions ORDER BY created_at DESC").fetchall()
    return [get_subscription(r["id"]) for r in rows]


def build_vless_link_for_location(sub: dict[str, Any], location: dict[str, Any], node: dict[str, Any]) -> str:
    flag = str(location.get("flag") or "").strip()
    place = f"{location['country']} {location['city']}".strip()
    if flag:
        remark = f"{flag} {place}".strip()
    else:
        remark = f"{place}".strip()
    port = int(node.get("reality_port") or 443)
    sni = str(node.get("reality_sni") or DEFAULT_REALITY_SNI)
    pbk = str(node.get("reality_public_key") or "")
    sid = str(node.get("reality_short_id") or "")
    # Per-subscription UUID: access can be revoked by removing this UUID from node config.
    client_uuid = str(sub.get("client_uuid") or "")
    if not pbk or not sid or not client_uuid:
        raise RuntimeError("Нода или подписка не содержит reality/client_uuid параметров")
    params = (
        f"encryption=none&security=reality&sni={quote(sni, safe='')}"
        f"&fp=chrome&pbk={quote(pbk, safe='')}&sid={quote(sid, safe='')}"
        "&type=tcp&headerType=none"
    )
    return f"vless://{client_uuid}@{node['ip']}:{port}?{params}#{quote(remark, safe='')}"


def sse_deployment_stream(dep_id: str, last_seq: int = 0) -> Generator[str, None, None]:
    last_heartbeat = time.time()
    while True:
        with db_conn() as conn:
            events = conn.execute(
                "SELECT seq, event_json FROM deployment_events WHERE deployment_id = ? AND seq > ? ORDER BY seq ASC",
                (dep_id, last_seq),
            ).fetchall()

        for ev in events:
            last_seq = ev["seq"]
            yield f"event: message\ndata: {ev['event_json']}\n\n"

        dep = deployment_get(dep_id)
        if dep and dep["status"] in ("success", "failed") and not events:
            break

        if time.time() - last_heartbeat > 15:
            yield ": keepalive\n\n"
            last_heartbeat = time.time()

        time.sleep(0.7)


def _region_bucket(region: str) -> str:
    r = (region or "").lower()
    if r.startswith("eu"):
        return "Европа"
    if r.startswith("us") or r.startswith("na"):
        return "Сев. Америка"
    if r.startswith("ap") or r.startswith("asia"):
        return "Азия"
    return "Прочее"


def build_stats_payload(range_key: str) -> dict[str, Any]:
    since = range_start(range_key)
    since_iso = iso(since)
    now = now_dt()
    seconds = max(1.0, (now - since).total_seconds())

    with db_conn() as conn:
        nodes = [dict(r) for r in conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()]
        locations = [dict(r) for r in conn.execute("SELECT * FROM locations").fetchall()]
        deploy_errors = conn.execute(
            "SELECT COUNT(*) c FROM deployments WHERE status = 'failed' AND created_at >= ?",
            (since_iso,),
        ).fetchone()["c"]

        pulls_period = conn.execute(
            "SELECT subscription_id, remote_ip, pulled_at FROM subscription_pulls WHERE pulled_at >= ?",
            (since_iso,),
        ).fetchall()
        dau = conn.execute(
            "SELECT COUNT(DISTINCT subscription_id) c FROM subscription_pulls WHERE pulled_at >= ?",
            (iso(now - timedelta(hours=24)),),
        ).fetchone()["c"]
        mau = conn.execute(
            "SELECT COUNT(DISTINCT subscription_id) c FROM subscription_pulls WHERE pulled_at >= ?",
            (iso(now - timedelta(days=30)),),
        ).fetchone()["c"]

        new_users = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE created_at >= ?",
            (since_iso,),
        ).fetchone()["c"]
        churn_revoked = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE status = 'revoked' AND updated_at >= ?",
            (since_iso,),
        ).fetchone()["c"]
        churn_expired = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE status = 'active' AND expires_at >= ? AND expires_at < ?",
            (since_iso, iso(now),),
        ).fetchone()["c"]
        active_subs = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE status = 'active' AND expires_at > ?",
            (iso(now),),
        ).fetchone()["c"]
        total_subs = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions",
        ).fetchone()["c"]

        pulls_agg = conn.execute(
            """
            SELECT collected_at, SUM(mbps) AS total_mbps
            FROM node_metrics_history
            WHERE collected_at >= ?
            GROUP BY collected_at
            ORDER BY collected_at
            """,
            (since_iso,),
        ).fetchall()
        uptime_agg = conn.execute(
            "SELECT AVG(xray_active) AS v FROM node_metrics_history WHERE collected_at >= ?",
            (since_iso,),
        ).fetchone()["v"]

        metrics_by_node = conn.execute(
            """
            SELECT node_id, AVG(mbps) AS avg_mbps
            FROM node_metrics_history
            WHERE collected_at >= ?
            GROUP BY node_id
            """,
            (since_iso,),
        ).fetchall()

    total_nodes = len(nodes)
    online_nodes = sum(1 for n in nodes if int(n.get("enabled") or 0) == 1)
    current_gbps = sum(float(n.get("last_mbps") or 0.0) for n in nodes) / 1000.0
    current_online_connections = sum(int(n.get("last_active_conns") or 0) for n in nodes if int(n.get("enabled") or 0) == 1)
    traffic_tb = 0.0
    if pulls_agg:
        avg_total_mbps = sum(float(r["total_mbps"] or 0.0) for r in pulls_agg) / len(pulls_agg)
        traffic_tb = avg_total_mbps * seconds / 8_000_000.0

    slots = 7
    interval = seconds / slots
    sums = [0.0] * slots
    cnts = [0] * slots
    for r in pulls_agg:
        try:
            ts = parse_utc(str(r["collected_at"]))
        except Exception:
            continue
        idx = int((ts - since).total_seconds() / interval)
        if idx < 0:
            idx = 0
        if idx >= slots:
            idx = slots - 1
        sums[idx] += float(r["total_mbps"] or 0.0)
        cnts[idx] += 1
    traffic_series_values = []
    for i in range(slots):
        avg_mbps_bucket = (sums[i] / cnts[i]) if cnts[i] > 0 else 0.0
        tb = avg_mbps_bucket * interval / 8_000_000.0
        traffic_series_values.append(round(tb, 3))

    traffic_series_labels = []
    for i in range(slots):
        t = since + timedelta(seconds=interval * (i + 1))
        if range_key == "24h":
            traffic_series_labels.append(t.strftime("%H:%M"))
        elif range_key == "7d":
            traffic_series_labels.append(t.strftime("%d.%m"))
        else:
            traffic_series_labels.append(t.strftime("%d.%m"))

    uptime_percent = float(uptime_agg or 0.0) * 100.0 if uptime_agg is not None else (100.0 if total_nodes == 0 else (online_nodes / max(1, total_nodes) * 100.0))
    devices_per_sub = 0.0
    if active_subs > 0:
        uniq_devices = len({(r["subscription_id"], r["remote_ip"]) for r in pulls_period if r["remote_ip"]})
        devices_per_sub = uniq_devices / active_subs

    avg_mbps_by_node = {r["node_id"]: float(r["avg_mbps"] or 0.0) for r in metrics_by_node}
    top_nodes = []
    for n in sorted(nodes, key=lambda x: float(x.get("last_load") or 0.0), reverse=True)[:5]:
        mbps = float(n.get("last_mbps") or 0.0)
        top_nodes.append(
            {
                "id": n["id"],
                "name": n["name"],
                "health": node_health(n),
                "cpu": round(float(n.get("last_cpu") or 0.0), 1),
                "ram": round(float(n.get("last_ram") or 0.0), 1),
                "rx_mbps": round(mbps * 0.55, 1),
                "tx_mbps": round(mbps * 0.45, 1),
                "load": round(float(n.get("last_load") or 0.0), 1),
                "active_connections": int(n.get("last_active_conns") or 0),
            }
        )

    by_loc: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        by_loc.setdefault(str(n.get("location_code") or "OT"), []).append(n)
    heatmap = []
    loc_meta = {l["code"]: l for l in locations}
    for code, arr in sorted(by_loc.items(), key=lambda kv: kv[0]):
        avg_load = sum(float(x.get("last_load") or 0.0) for x in arr) / max(1, len(arr))
        heatmap.append({
            "code": code,
            "country": (loc_meta.get(code) or {}).get("country", code),
            "avg_load": round(avg_load, 1),
            "nodes": len(arr),
        })

    by_region: dict[str, list[float]] = {}
    for n in nodes:
        lat = n.get("last_latency_ms")
        if lat is None:
            continue
        code = str(n.get("location_code") or "")
        rg = _region_bucket((loc_meta.get(code) or {}).get("region", ""))
        by_region.setdefault(rg, []).append(float(lat))
    latency = []
    for name, vals in by_region.items():
        latency.append({
            "region": name,
            "p50": round(percentile(vals, 0.5), 1),
            "p95": round(percentile(vals, 0.95), 1),
        })
    latency.sort(key=lambda x: x["region"])

    anomalies = []
    for n in nodes:
        load = float(n.get("last_load") or 0.0)
        mbps = float(n.get("last_mbps") or 0.0)
        avg_hist = avg_mbps_by_node.get(n["id"], 0.0)
        if int(n.get("enabled") or 0) == 0:
            anomalies.append({"title": n["name"], "message": "Нода недоступна (xray inactive)"})
        elif load >= 90:
            anomalies.append({"title": n["name"], "message": f"Высокая нагрузка: {load:.0f}%"})
        elif avg_hist > 0 and mbps > avg_hist * 2.2:
            anomalies.append({"title": n["name"], "message": f"Всплеск трафика: {mbps:.0f} Mbps (avg {avg_hist:.0f})"})
    anomalies = anomalies[:5]

    return {
        "range": range_key,
        "dau": int(dau),
        "mau": int(mau),
        "online_nodes": online_nodes,
        "total_nodes": total_nodes,
        "current_gbps": round(current_gbps, 3),
        "current_online_connections": int(current_online_connections),
        "traffic_tb": round(traffic_tb, 2),
        "uptime_percent": round(uptime_percent, 2),
        "deploy_errors": int(deploy_errors),
        "new_users": int(new_users),
        "churn": int(churn_revoked + churn_expired),
        "devices_per_sub": round(devices_per_sub, 2),
        "active_subscriptions": int(active_subs),
        "total_subscriptions": int(total_subs),
        "traffic_series": {
            "labels": traffic_series_labels,
            "values": traffic_series_values,
        },
        "top_nodes": top_nodes,
        "heatmap": heatmap,
        "latency": latency,
        "anomalies": anomalies,
    }


@app.on_event("startup")
def on_startup() -> None:
    _prepare_db_file()
    init_db()
    migrate_db()
    seed_data()
    start_metrics_collector()
    start_subscriptions_enforcer()
    threading.Thread(target=_sync_all_nodes_clients, kwargs={"reason": "startup"}, daemon=True, name="subscriptions-sync-startup").start()
    threading.Thread(target=_refresh_bot_subscriptions_locations, kwargs={"reason": "startup-bot-autolocations"}, daemon=True, name="bot-autolocations-startup").start()


@app.get("/")
def root() -> FileResponse:
    page = REPO_DIR / "generated-page-4.html"
    if page.exists():
        return FileResponse(str(page))
    fallback = STATIC_DIR / "index.html"
    if fallback.exists():
        return FileResponse(str(fallback))
    raise HTTPException(status_code=404, detail="No UI file found")


@app.get("/generated-page-{n}.html")
def generated_page(n: int) -> FileResponse:
    path = REPO_DIR / f"generated-page-{n}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path))


@app.get("/styles.css")
def styles() -> FileResponse:
    path = STATIC_DIR / "styles.css"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path))


@app.get("/app.js")
def app_js() -> FileResponse:
    path = STATIC_DIR / "app.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(path))


@app.post("/api/auth/login")
def api_auth_login(body: LoginRequest):
    if body.username != ADMIN_USERNAME or body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token, expires_ts = _create_auth_session(body.username)
    resp = JSONResponse({"ok": True, "username": body.username, "expires_at": expires_ts})
    resp.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=token,
        max_age=max(300, ADMIN_SESSION_TTL),
        httponly=True,
        samesite="lax",
        secure=ADMIN_COOKIE_SECURE,
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    _drop_auth_session(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(ADMIN_SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
def api_auth_me(request: Request):
    username = _get_auth_user(request)
    if username is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"ok": True, "username": username}


@app.post("/api/bot/issue-key")
def api_bot_issue_key(body: BotIssueKeyRequest, x_bot_token: Optional[str] = Header(default=None, alias="X-Bot-Token")) -> dict[str, Any]:
    if not _bot_token_valid(x_bot_token):
        raise HTTPException(status_code=401, detail="Unauthorized bot token")

    tg_user_id = str(body.telegram_user_id).strip()
    if not tg_user_id or tg_user_id == "0":
        raise HTTPException(status_code=400, detail="telegram_user_id is required")

    now = now_dt()
    now_iso = iso(now)
    cooldown_days = max(1, int(BOT_SUB_COOLDOWN_DAYS))
    sub_days = max(1, int(BOT_SUB_DAYS))
    traffic_gb = max(1, int(BOT_SUB_TRAFFIC_GB))
    device_limit = max(1, min(20, int(BOT_SUB_DEVICE_LIMIT)))

    with db_conn() as conn:
        issue = conn.execute(
            "SELECT telegram_user_id, subscription_id, issued_at FROM bot_key_issues WHERE telegram_user_id = ?",
            (tg_user_id,),
        ).fetchone()
        if issue:
            issued_at = parse_utc(str(issue["issued_at"]))
            next_at = issued_at + timedelta(days=cooldown_days)
            if next_at > now:
                wait_sec = int((next_at - now).total_seconds())
                return {
                    "ok": False,
                    "reason": "cooldown",
                    "retry_after_seconds": max(1, wait_sec),
                    "next_issue_at": iso(next_at),
                    "subscription_id": issue["subscription_id"],
                }

        location_codes = _active_vless_location_codes(conn)
        if not location_codes:
            raise HTTPException(status_code=503, detail="No active nodes for issuing key")

        sid = "sub-" + uuid.uuid4().hex[:12]
        token = uuid.uuid4().hex
        client_uuid = str(uuid.uuid4())
        expires = now + timedelta(days=sub_days)
        expires_iso = iso(expires)
        base_url = (body.base_url or f"http://{HOST}:{PORT}").strip().rstrip("/")
        name = str(VPN_BRAND_NAME or "BURMALDAA VPN")

        conn.execute(
            """
            INSERT INTO subscriptions(id, name, protocol, days, traffic_gb, device_limit, used_gb, status, token, client_uuid, base_url, created_at, expires_at, updated_at)
            VALUES(?, ?, 'VLESS', ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (sid, name, sub_days, traffic_gb, device_limit, token, client_uuid, base_url, now_iso, expires_iso, now_iso),
        )
        for code in location_codes:
            conn.execute("INSERT INTO subscription_locations(subscription_id, location_code) VALUES(?, ?)", (sid, code))
        conn.execute(
            """
            INSERT INTO bot_key_issues(telegram_user_id, subscription_id, issued_at, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
              subscription_id = excluded.subscription_id,
              issued_at = excluded.issued_at,
              updated_at = excluded.updated_at
            """,
            (tg_user_id, sid, now_iso, now_iso),
        )

    try:
        _sync_locations_clients(location_codes, reason="bot-issue")
    except Exception as exc:
        with db_conn() as conn:
            conn.execute("UPDATE subscriptions SET status = 'revoked', updated_at = ? WHERE id = ?", (utc_now(), sid))
        raise HTTPException(status_code=502, detail=f"Key issued but node sync failed: {exc}")

    sub = get_subscription(sid)
    if not sub:
        raise HTTPException(status_code=500, detail="Failed to build issued subscription")
    return {
        "ok": True,
        "subscription_id": sid,
        "expires_at": sub.get("expires_at"),
        "subscription_url": sub.get("subscription_url"),
        "happ_url": sub.get("happ_url"),
        "cooldown_days": cooldown_days,
    }


@app.get("/api/locations")
def api_locations() -> dict[str, Any]:
    return {"items": location_list()}


@app.post("/api/locations", status_code=201)
def api_create_location(body: CreateLocationRequest) -> dict[str, Any]:
    code = body.code.upper().strip()
    now = utc_now()
    with db_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO locations(code, flag, country, city, region, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (code, body.flag.strip(), body.country.strip(), body.city.strip(), body.region.strip(), now, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Location code already exists")
    return {"code": code, "flag": body.flag, "country": body.country, "city": body.city, "region": body.region}


@app.patch("/api/locations/{code}")
def api_update_location(code: str, body: UpdateLocationRequest) -> dict[str, Any]:
    updates = {k: v.strip() for k, v in body.model_dump(exclude_none=True).items() if str(v).strip()}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    sets = ", ".join([f"{k} = ?" for k in updates.keys()] + ["updated_at = ?"])
    values = [*updates.values(), utc_now(), code.upper()]
    with db_conn() as conn:
        row = conn.execute("SELECT code FROM locations WHERE code = ?", (code.upper(),)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Location not found")
        conn.execute(f"UPDATE locations SET {sets} WHERE code = ?", values)
        out = conn.execute("SELECT * FROM locations WHERE code = ?", (code.upper(),)).fetchone()
    return dict(out)


@app.delete("/api/locations/{code}", status_code=204)
def api_delete_location(code: str):
    code = code.upper()
    with db_conn() as conn:
        has_nodes = conn.execute("SELECT COUNT(*) c FROM nodes WHERE location_code = ?", (code,)).fetchone()["c"]
        if has_nodes > 0:
            raise HTTPException(status_code=409, detail="Location has nodes")
        conn.execute("DELETE FROM locations WHERE code = ?", (code,))
    return PlainTextResponse("", status_code=204)


@app.get("/api/nodes")
def api_nodes(location_code: str | None = None, status: str | None = None) -> dict[str, Any]:
    return {"items": node_list(location_code=location_code, status=status)}


@app.get("/api/nodes/{node_id}/logs")
def api_node_logs(node_id: str, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    with db_conn() as conn:
        exists = conn.execute("SELECT id FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Node not found")
        rows = conn.execute(
            """
            SELECT id, node_id, phase, command, exit_code, stdout, stderr, created_at
            FROM node_command_logs
            WHERE node_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (node_id, int(limit)),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.patch("/api/nodes/{node_id}")
def api_update_node(node_id: str, body: UpdateNodeRequest) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "location_code" in updates:
        updates["location_code"] = str(updates["location_code"]).upper().strip()
        ensure_location_exists(updates["location_code"])
    if "protocol" in updates:
        updates["protocol"] = str(updates["protocol"]).upper().strip()
    if "enabled" in updates:
        updates["enabled"] = 1 if bool(updates["enabled"]) else 0

    sets = ", ".join([f"{k} = ?" for k in updates.keys()] + ["updated_at = ?"])
    values = [*updates.values(), utc_now(), node_id]

    with db_conn() as conn:
        row = conn.execute("SELECT id FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Node not found")
        conn.execute(f"UPDATE nodes SET {sets} WHERE id = ?", values)

    item = next((n for n in node_list() if n["id"] == node_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Node not found")
    try:
        _refresh_bot_subscriptions_locations(reason="update-node-bot-autolocations")
    except Exception as exc:
        print(f"[bot-autolocations] update-node warning: {exc}")
    return item


@app.delete("/api/nodes/{node_id}", status_code=204)
def api_delete_node(node_id: str):
    with db_conn() as conn:
        conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    try:
        _refresh_bot_subscriptions_locations(reason="delete-node-bot-autolocations")
    except Exception as exc:
        print(f"[bot-autolocations] delete-node warning: {exc}")
    return PlainTextResponse("", status_code=204)


@app.post("/api/nodes/{node_id}/toggle")
def api_toggle_node(node_id: str, body: ToggleNodeRequest) -> dict[str, Any]:
    node = _node_from_db(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if not _has_ssh(node):
        with db_conn() as conn:
            conn.execute("UPDATE nodes SET enabled = ?, updated_at = ? WHERE id = ?", (1 if body.enabled else 0, utc_now(), node_id))
        try:
            _refresh_bot_subscriptions_locations(reason="toggle-node-local-bot-autolocations")
        except Exception as exc:
            print(f"[bot-autolocations] toggle-node(local) warning: {exc}")
        return {"ok": True, "enabled": body.enabled, "mode": "local"}
    try:
        _set_node_service_state(node, body.enabled)
        metrics = _collect_remote_metrics(node)
        _update_node_metrics(node_id, metrics)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    try:
        _refresh_bot_subscriptions_locations(reason="toggle-node-bot-autolocations")
    except Exception as exc:
        print(f"[bot-autolocations] toggle-node warning: {exc}")
    return {"ok": True, "enabled": body.enabled}


@app.post("/api/nodes/{node_id}/restart", status_code=202)
def api_restart_node(node_id: str) -> dict[str, Any]:
    node = _node_from_db(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if not _has_ssh(node):
        return {"ok": True, "mode": "local"}
    try:
        _restart_node_service(node)
        metrics = _collect_remote_metrics(node)
        _update_node_metrics(node_id, metrics)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True}


@app.post("/api/deployments", status_code=201)
def api_create_deployment(body: CreateDeploymentRequest) -> dict[str, str]:
    dep_id = "dep-" + uuid.uuid4().hex[:12]
    now = utc_now()
    payload = body.model_dump()

    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO deployments(id, status, progress, message, error, payload_json, node_id, created_at, updated_at)
            VALUES(?, 'queued', 0, 'Создана задача', NULL, ?, NULL, ?, ?)
            """,
            (dep_id, json.dumps(payload, ensure_ascii=False), now, now),
        )

    deployment_push_event(dep_id, {"status": "queued", "progress": 0, "message": "Задача поставлена в очередь", "level": "info"})
    threading.Thread(target=deployment_worker, args=(dep_id, payload), daemon=True).start()
    return {"deployment_id": dep_id}


@app.get("/api/deployments/{deployment_id}")
def api_get_deployment(deployment_id: str) -> dict[str, Any]:
    dep = deployment_get(deployment_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return dep


@app.get("/api/deployments/{deployment_id}/events")
def api_deployment_events(deployment_id: str, last_seq: int = Query(default=0, ge=0)):
    dep = deployment_get(deployment_id)
    if not dep:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return StreamingResponse(sse_deployment_stream(deployment_id, last_seq=last_seq), media_type="text/event-stream")


@app.get("/api/subscriptions")
def api_subscriptions() -> dict[str, Any]:
    return {"items": list_subscriptions()}


@app.post("/api/subscriptions", status_code=201)
def api_create_subscription(body: CreateSubscriptionRequest) -> dict[str, Any]:
    location_codes = sorted({str(c).upper().strip() for c in body.location_codes if str(c).strip()})
    if not location_codes:
        raise HTTPException(status_code=400, detail="location_codes must not be empty")

    with db_conn() as conn:
        existing_codes = {r["code"] for r in conn.execute("SELECT code FROM locations").fetchall()}
        unknown = [c for c in location_codes if c not in existing_codes]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown locations: {', '.join(unknown)}")

        sid = "sub-" + uuid.uuid4().hex[:12]
        token = uuid.uuid4().hex
        client_uuid = str(uuid.uuid4())
        created = now_dt()
        expires = created + timedelta(days=body.days)
        created_s = created.strftime("%Y-%m-%dT%H:%M:%SZ")
        expires_s = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
        base_url = (body.base_url or f"http://{HOST}:{PORT}").strip().rstrip("/")

        conn.execute(
            """
            INSERT INTO subscriptions(id, name, protocol, days, traffic_gb, device_limit, used_gb, status, token, client_uuid, base_url, created_at, expires_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                VPN_BRAND_NAME,
                body.protocol.upper().strip(),
                body.days,
                body.traffic_gb,
                body.device_limit,
                token,
                client_uuid,
                base_url,
                created_s,
                expires_s,
                created_s,
            ),
        )
        for code in location_codes:
            conn.execute("INSERT INTO subscription_locations(subscription_id, location_code) VALUES(?, ?)", (sid, code))

    sub = get_subscription(sid)
    if not sub:
        raise HTTPException(status_code=500, detail="Failed to create subscription")
    try:
        _sync_locations_clients(location_codes, reason="create-sub")
    except Exception as exc:
        with db_conn() as conn:
            conn.execute("UPDATE subscriptions SET status = 'revoked', updated_at = ? WHERE id = ?", (utc_now(), sid))
        raise HTTPException(status_code=502, detail=f"Subscription created, but node sync failed: {exc}")
    return sub


@app.get("/api/subscriptions/{subscription_id}")
def api_get_subscription(subscription_id: str) -> dict[str, Any]:
    sub = get_subscription(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return sub


@app.patch("/api/subscriptions/{subscription_id}")
def api_update_subscription(subscription_id: str, body: UpdateSubscriptionRequest) -> dict[str, Any]:
    affected_codes: set[str] = set()
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Subscription not found")
        is_bot_subscription = conn.execute(
            "SELECT 1 FROM bot_key_issues WHERE subscription_id = ? LIMIT 1",
            (subscription_id,),
        ).fetchone() is not None
        old_codes = {
            r["location_code"]
            for r in conn.execute(
                "SELECT location_code FROM subscription_locations WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchall()
        }
        affected_codes.update(old_codes)

        updates = body.model_dump(exclude_none=True)
        fields: dict[str, Any] = {}
        for k in ("name", "status"):
            if k in updates and str(updates[k]).strip():
                fields[k] = str(updates[k]).strip()
        for k in ("days", "traffic_gb", "device_limit"):
            if k in updates:
                fields[k] = int(updates[k])

        if "days" in fields:
            created = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            fields["expires_at"] = (created + timedelta(days=fields["days"])).strftime("%Y-%m-%dT%H:%M:%SZ")

        if fields:
            sets = ", ".join([f"{k} = ?" for k in fields.keys()] + ["updated_at = ?"])
            conn.execute(f"UPDATE subscriptions SET {sets} WHERE id = ?", [*fields.values(), utc_now(), subscription_id])

        if "location_codes" in updates or is_bot_subscription:
            if is_bot_subscription:
                codes = _active_vless_location_codes(conn)
            else:
                codes = sorted({str(c).upper().strip() for c in (updates.get("location_codes") or []) if str(c).strip()})
            if not codes:
                raise HTTPException(status_code=400, detail="location_codes must not be empty")
            existing_codes = {r["code"] for r in conn.execute("SELECT code FROM locations").fetchall()}
            unknown = [c for c in codes if c not in existing_codes]
            if unknown:
                raise HTTPException(status_code=422, detail=f"Unknown locations: {', '.join(unknown)}")
            conn.execute("DELETE FROM subscription_locations WHERE subscription_id = ?", (subscription_id,))
            for code in codes:
                conn.execute("INSERT INTO subscription_locations(subscription_id, location_code) VALUES(?, ?)", (subscription_id, code))
            affected_codes.update(codes)

    sub = get_subscription(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if not affected_codes:
        affected_codes = set(sub.get("location_codes") or [])
    _sync_locations_clients(sorted(affected_codes), reason="update-sub")
    return sub


@app.delete("/api/subscriptions/{subscription_id}", status_code=204)
def api_delete_subscription(subscription_id: str):
    codes: list[str] = []
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        if not row:
            return PlainTextResponse("", status_code=204)
        codes = [
            r["location_code"]
            for r in conn.execute(
                "SELECT location_code FROM subscription_locations WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchall()
        ]
        conn.execute("DELETE FROM subscription_locations WHERE subscription_id = ?", (subscription_id,))
        conn.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))
    _sync_locations_clients(codes, reason="delete-sub")
    return PlainTextResponse("", status_code=204)


@app.post("/api/subscriptions/{subscription_id}/revoke")
def api_revoke_subscription(subscription_id: str) -> dict[str, Any]:
    codes: list[str] = []
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Subscription not found")
        codes = [
            r["location_code"]
            for r in conn.execute(
                "SELECT location_code FROM subscription_locations WHERE subscription_id = ?",
                (subscription_id,),
            ).fetchall()
        ]
        conn.execute("UPDATE subscriptions SET status = 'revoked', updated_at = ? WHERE id = ?", (utc_now(), subscription_id))
    sub = get_subscription(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    _sync_locations_clients(codes, reason="revoke-sub")
    return sub


@app.post("/api/subscriptions/{subscription_id}/rotate-token")
def api_rotate_subscription_token(subscription_id: str) -> dict[str, Any]:
    token = uuid.uuid4().hex
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Subscription not found")
        conn.execute("UPDATE subscriptions SET token = ?, updated_at = ? WHERE id = ?", (token, utc_now(), subscription_id))
    sub = get_subscription(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return sub


@app.get("/api/subscriptions/{subscription_id}/link")
def api_subscription_link(subscription_id: str) -> dict[str, str]:
    sub = get_subscription(subscription_id)
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return {"subscription_url": sub["subscription_url"], "happ_url": sub["happ_url"]}


@app.get("/api/stats/summary")
def api_stats_summary(range: str = Query(default="24h", pattern="^(24h|7d|30d)$")) -> dict[str, Any]:
    payload = build_stats_payload(range)
    return {
        "range": payload["range"],
        "dau": payload["dau"],
        "mau": payload["mau"],
        "online_nodes": payload["online_nodes"],
        "total_nodes": payload["total_nodes"],
        "current_gbps": payload["current_gbps"],
        "current_online_connections": payload["current_online_connections"],
        "traffic_tb": payload["traffic_tb"],
        "uptime_percent": payload["uptime_percent"],
        "deploy_errors": payload["deploy_errors"],
    }


@app.get("/api/stats/details")
def api_stats_details(range: str = Query(default="24h", pattern="^(24h|7d|30d)$")) -> dict[str, Any]:
    return build_stats_payload(range)


@app.get("/sub/{subscription_id}")
def get_subscription_payload(subscription_id: str, token: Optional[str] = None, format: str = "b64", request: Request = None):
    remote_ip = request.client.host if (request and request.client) else ""
    user_agent = request.headers.get("user-agent", "") if request else ""
    token_mode = bool(token and token.strip())
    now_dt_utc = now_dt()
    now = iso(now_dt_utc)
    with db_conn() as conn:
        if token_mode:
            srow = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE id = ? AND token = ?
                """,
                (subscription_id, token.strip()),
            ).fetchone()
        else:
            # Backward compatibility for old frontend links: /sub/{token}
            srow = conn.execute(
                """
                SELECT * FROM subscriptions
                WHERE token = ?
                """,
                (subscription_id.strip(),),
            ).fetchone()
        if not srow:
            raise HTTPException(status_code=404, detail="Subscription not found")
        resolved_sub_id = str(srow["id"])

        locs = conn.execute(
            "SELECT location_code FROM subscription_locations WHERE subscription_id = ? ORDER BY location_code",
            (resolved_sub_id,),
        ).fetchall()

        sub = dict(srow)
        sub_status = str(sub.get("status") or "").lower()
        expires_at = str(sub.get("expires_at") or "")
        expires_dt = parse_utc(expires_at) if expires_at else now_dt_utc
        expired = expires_dt <= now_dt_utc
        used_gb = float(sub.get("used_gb") or 0.0)
        traffic_gb = float(sub.get("traffic_gb") or 0.0)
        over_limit = traffic_gb > 0 and used_gb >= traffic_gb
        device_limit = int(sub.get("device_limit") or DEFAULT_DEVICE_LIMIT)
        device_key = _device_fingerprint(request, resolved_sub_id)
        device_row = conn.execute(
            "SELECT id, blocked FROM subscription_devices WHERE subscription_id = ? AND device_key = ?",
            (resolved_sub_id, device_key),
        ).fetchone()
        device_denied = False
        if device_row:
            conn.execute(
                "UPDATE subscription_devices SET last_seen = ?, last_ip = ?, last_user_agent = ? WHERE id = ?",
                (utc_now(), remote_ip, user_agent, int(device_row["id"])),
            )
            device_denied = int(device_row["blocked"] or 0) == 1
        else:
            active_devices = conn.execute(
                "SELECT COUNT(*) c FROM subscription_devices WHERE subscription_id = ? AND blocked = 0",
                (resolved_sub_id,),
            ).fetchone()["c"]
            if active_devices >= device_limit:
                device_denied = True
                conn.execute(
                    """
                    INSERT INTO subscription_devices(subscription_id, device_key, first_seen, last_seen, last_ip, last_user_agent, blocked)
                    VALUES(?, ?, ?, ?, ?, ?, 1)
                    """,
                    (resolved_sub_id, device_key, utc_now(), utc_now(), remote_ip, user_agent),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO subscription_devices(subscription_id, device_key, first_seen, last_seen, last_ip, last_user_agent, blocked)
                    VALUES(?, ?, ?, ?, ?, ?, 0)
                    """,
                    (resolved_sub_id, device_key, utc_now(), utc_now(), remote_ip, user_agent),
                )
        current_devices = conn.execute(
            "SELECT COUNT(*) c FROM subscription_devices WHERE subscription_id = ? AND blocked = 0",
            (resolved_sub_id,),
        ).fetchone()["c"]
        is_active = (sub_status == "active") and (not expired) and (not over_limit) and (not device_denied)

        links: list[str] = []
        if is_active:
            for r in locs:
                code = r["location_code"]
                loc = conn.execute("SELECT * FROM locations WHERE code = ?", (code,)).fetchone()
                node = conn.execute(
                    "SELECT * FROM nodes WHERE location_code = ? AND enabled = 1 ORDER BY updated_at DESC LIMIT 1",
                    (code,),
                ).fetchone()
                if not loc or not node:
                    continue
                links.append(build_vless_link_for_location(sub, dict(loc), dict(node)))

        profile_title = str(VPN_BRAND_NAME or sub.get("name") or "VPN Subscription")
        total_bytes = int(max(0.0, traffic_gb) * (1024 ** 3))
        used_bytes = int(max(0.0, used_gb) * (1024 ** 3))
        expire_unix = int(expires_dt.timestamp())
        info_lines = [
            f"Подписка: {profile_title}",
            f"Статус: {'active' if is_active else 'expired'}",
            f"Истекает: {expires_at}",
            f"Трафик: {used_gb:.2f}GB / {traffic_gb:.2f}GB",
            f"Устройства: {current_devices}/{device_limit}",
        ]
        if device_denied:
            info_lines.append("Лимит устройств достигнут: новое устройство заблокировано")
        if HAPP_SUB_INFO_TEXT:
            info_lines.append(HAPP_SUB_INFO_TEXT)
        if HAPP_SUPPORT_URL:
            info_lines.append(f"Поддержка: {HAPP_SUPPORT_URL}")
        info_text = " | ".join(info_lines)

        meta_lines = [
            f"#profile-title: {profile_title}",
            f"#profile-update-interval: {max(1, HAPP_PROFILE_UPDATE_INTERVAL)}",
            f"#subscription-userinfo: upload=0; download={used_bytes}; total={total_bytes}; expire={expire_unix}",
            f"#sub-expire: {1 if not is_active else 0}",
            f"#sub-info-text: {info_text}",
        ]
        if HAPP_SUPPORT_URL:
            meta_lines.append(f"#support-url: {HAPP_SUPPORT_URL}")
        if HAPP_RENEW_URL:
            meta_lines.append(f"#sub-expire-button-link: {HAPP_RENEW_URL}")

        conn.execute(
            "INSERT INTO subscription_pulls(subscription_id, remote_ip, user_agent, pulled_at) VALUES(?, ?, ?, ?)",
            (resolved_sub_id, remote_ip, user_agent, utc_now()),
        )

    raw_lines = [*meta_lines, *links, *_promo_links(str(sub.get("client_uuid") or ""))]
    raw = "\n".join(raw_lines)
    headers = {
        "profile-title": _header_safe(profile_title),
        "profile-update-interval": _header_safe(str(max(1, HAPP_PROFILE_UPDATE_INTERVAL))),
        "subscription-userinfo": _header_safe(f"upload=0; download={used_bytes}; total={total_bytes}; expire={expire_unix}"),
        "sub-expire": "1" if not is_active else "0",
        "sub-info-text": _header_safe(info_text),
    }
    if HAPP_SUPPORT_URL:
        headers["support-url"] = _header_safe(HAPP_SUPPORT_URL)
    if HAPP_RENEW_URL:
        headers["sub-expire-button-link"] = _header_safe(HAPP_RENEW_URL)
    if format == "raw":
        return PlainTextResponse(raw, headers=headers)
    return PlainTextResponse(base64.b64encode(raw.encode("utf-8")).decode("ascii"), headers=headers)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
