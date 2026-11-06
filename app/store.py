"""SQLite 저장소: 설정(자격증명 포함), 일별 스냅샷, 보유종목, 알림 기록."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "monitor.db"

DEFAULTS = {
    "app_key": "",
    "app_secret": "",
    "brand": "nhplug",          # nhplug(나무) | n2plug(N2)
    "annual_target": 0.20,      # 전체 계좌 연간 목표 수익률
    "default_account_target": 1.00,
    "account_targets": {},      # {acct_no: 목표수익률}
    "telegram_token": "",
    "telegram_chat_id": "",
}
SECRET_KEYS = {"app_secret", "telegram_token"}

_lock = threading.Lock()


def today() -> str:
    return datetime.now(KST).strftime("%Y%m%d")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    """데이터 폴더·DB 부트스트랩. 앱키·시크릿이 들어가므로 소유자만 접근 가능하게 만든다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA_DIR, 0o700)
    ignore = DATA_DIR / ".gitignore"  # DATA_DIR 를 어디에 두든 git 에 올라가지 않게
    if not ignore.exists():
        ignore.write_text("*\n")
    DB_PATH.touch(mode=0o600, exist_ok=True)
    os.chmod(DB_PATH, 0o600)
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS snapshots (
            date TEXT, acct TEXT, value REAL, cost REAL, pnl REAL,
            PRIMARY KEY (date, acct));
        CREATE TABLE IF NOT EXISTS holdings (
            acct TEXT, sym TEXT, market TEXT, name TEXT,
            qty REAL, value REAL, cost REAL, PRIMARY KEY (acct, sym, market));
        CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, sent_at TEXT, message TEXT);
        """)


# ---------------------------------------------------------------- settings

def get_settings() -> dict:
    with _lock, _conn() as c:
        rows = c.execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULTS)
    s.update({r["key"]: json.loads(r["value"]) for r in rows if r["key"] in DEFAULTS})
    return s


def update_settings(patch: dict) -> dict:
    patch = {k: v for k, v in patch.items() if k in DEFAULTS}
    with _lock, _conn() as c:
        for k, v in patch.items():
            c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, json.dumps(v)))
    return get_settings()


def public_settings() -> dict:
    """UI 로 내보낼 설정. 시크릿은 값 대신 설정 여부만."""
    s = get_settings()
    for k in SECRET_KEYS:
        s[k + "_set"] = bool(s.pop(k))
    key = s["app_key"]
    s["app_key"] = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("…" if key else "")
    return s


# ---------------------------------------------------------------- snapshots

def save_snapshot(date: str, accounts: list[dict]) -> None:
    with _lock, _conn() as c:
        for a in accounts:
            c.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
                      (date, a["acct"], a["value"], a["cost"], a["pnl"]))
        c.execute("DELETE FROM holdings")
        for a in accounts:
            for h in a["holdings"]:
                c.execute("INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?,?,?)",
                          (a["acct"], h["sym"], h["market"], h["name"],
                           h["qty"], h["value"], h["cost"]))


def snapshots() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM snapshots ORDER BY date, acct")]


def holdings() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM holdings ORDER BY value DESC")]


def clear_data() -> None:
    """앱키·서비스 변경 시 기록 초기화."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM snapshots")
        c.execute("DELETE FROM holdings")
        c.execute("DELETE FROM alerts")


# ---------------------------------------------------------------- alerts

def alert_sent(key: str) -> bool:
    with _lock, _conn() as c:
        return c.execute("SELECT 1 FROM alerts WHERE key=?", (key,)).fetchone() is not None


def record_alert(key: str, message: str) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?)",
                  (key, datetime.now(KST).isoformat(timespec="seconds"), message))


def alerts() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY sent_at DESC")]
