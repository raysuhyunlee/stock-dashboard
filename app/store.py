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
    "default_account_target": 1.00,  # 전체 계좌 합산 보유 평가수익률 목표
    "telegram_token": "",
    "telegram_chat_id": "",
    "tiingo_key": "",           # 해외주식 과거 시세(백필)
}
SECRET_KEYS = {"app_secret", "telegram_token", "tiingo_key"}

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
        -- 거래내역 원본(src: tt=종합거래내역, gb=해외 일별거래내역). 매도 끝난 종목도 남는다
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY, acct TEXT, src TEXT, date TEXT, kind TEXT,
            sym TEXT, market TEXT, name TEXT, currency TEXT, qty REAL, price REAL,
            amount REAL, cash_after REAL, qty_after REAL, raw TEXT);
        CREATE INDEX IF NOT EXISTS transactions_acct_date ON transactions (acct, date);
        -- 일별 종목 잔고(value 는 원화). 현금은 market='CASH' (KRW·USD·CMA, 실측은 CASH 하나)
        CREATE TABLE IF NOT EXISTS positions (
            date TEXT, acct TEXT, sym TEXT, market TEXT, name TEXT,
            qty REAL, value REAL, cost REAL, PRIMARY KEY (date, acct, sym, market));
        """)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(snapshots)")}
        if "flow" not in cols:  # 외부 입출금(원화 환산) — 수익률에서 제외
            c.execute("ALTER TABLE snapshots ADD COLUMN flow REAL DEFAULT 0")


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
            c.execute("INSERT OR REPLACE INTO snapshots (date, acct, value, cost, pnl, flow) "
                      "VALUES (?,?,?,?,?,?)",
                      (date, a["acct"], a["value"], a["cost"], a["pnl"], a.get("flow", 0.0)))
        c.execute("DELETE FROM holdings")
        for a in accounts:
            for h in a["holdings"]:
                c.execute("INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?,?,?)",
                          (a["acct"], h["sym"], h["market"], h["name"],
                           h["qty"], h["value"], h["cost"]))
            c.execute("DELETE FROM positions WHERE date=? AND acct=?", (date, a["acct"]))
            cash = a["value"] - sum(h["value"] for h in a["holdings"])
            rows = [(h["sym"], h["market"], h["name"], h["qty"], h["value"], h["cost"])
                    for h in a["holdings"]] + [("CASH", "CASH", "현금", cash, cash, cash)]
            c.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
                          [(date, a["acct"], *r) for r in rows])


TXN_COLS = ("src", "date", "kind", "sym", "market", "name", "currency", "qty", "price",
            "amount", "cash_after", "qty_after", "raw")


def save_backfill(acct: str, values: dict[str, dict], txns: list[dict], until: str) -> None:
    """재구성한 과거 값(cost·pnl 은 NULL)·종목별 잔고와 거래내역을 넣는다.

    실측 날짜의 값·잔고는 두고, 이전 백필 값은 새로 바꾼다. 거래내역은 처음부터 until 까지
    새로 받은 것이므로 그 범위를 통째로 바꾼다. flow 는 실측 날짜도 갱신한다.
    """
    with _lock, _conn() as c:
        live = {r[0] for r in c.execute(
            "SELECT date FROM snapshots WHERE acct=? AND cost IS NOT NULL", (acct,))}
        for d, x in values.items():
            c.execute("INSERT INTO snapshots (date, acct, value, flow) VALUES (?,?,?,?) "
                      "ON CONFLICT(date, acct) DO UPDATE SET flow=excluded.flow, "
                      "value=CASE WHEN snapshots.cost IS NULL THEN excluded.value "
                      "ELSE snapshots.value END",
                      (d, acct, x["value"], x["flow"]))
            if d in live:
                continue
            c.execute("DELETE FROM positions WHERE date=? AND acct=?", (d, acct))
            c.executemany("INSERT INTO positions VALUES (?,?,?,?,?,?,?,NULL)",
                          [(d, acct, *p) for p in x.get("positions", [])])
        c.execute("DELETE FROM transactions WHERE acct=? AND date<=?", (acct, until))
        c.executemany(
            f"INSERT INTO transactions (acct, {', '.join(TXN_COLS)}) "
            f"VALUES (?{', ?' * len(TXN_COLS)})",
            [(acct, *(t.get(k) for k in TXN_COLS)) for t in txns])


def transactions(acct: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM transactions", ()
    if acct:
        q, args = q + " WHERE acct=?", (acct,)
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY date, id", args)]


def positions(date: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM positions", ()
    if date:
        q, args = q + " WHERE date=?", (date,)
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY date, acct, value DESC", args)]


def snapshots() -> list[dict]:
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM snapshots ORDER BY date, acct")]


def holdings() -> list[dict]:
    """전체 계좌 합산 보유종목."""
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT sym, market, MAX(name) AS name, SUM(qty) AS qty, SUM(value) AS value, "
            "SUM(cost) AS cost FROM holdings GROUP BY sym, market ORDER BY value DESC")]


def clear_data() -> None:
    """앱키·서비스 변경 시 기록 초기화."""
    with _lock, _conn() as c:
        c.execute("DELETE FROM snapshots")
        c.execute("DELETE FROM holdings")
        c.execute("DELETE FROM positions")
        c.execute("DELETE FROM transactions")
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
