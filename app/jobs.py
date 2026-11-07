"""주기 수집, 목표 수익률 알림, 시세 이력 캐시."""
from __future__ import annotations

import logging
import threading
from datetime import datetime

import requests

from . import analytics, backfill, nh, store

log = logging.getLogger("monitor")
_lock = threading.Lock()
state = {"last_update": None, "last_error": None}
_hist_cache: dict[tuple, list] = {}  # (sym, market, date) → history


def notify(text: str) -> bool:
    s = store.get_settings()
    log.warning("ALERT %s", text)
    if not (s["telegram_token"] and s["telegram_chat_id"]):
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{s['telegram_token']}/sendMessage",
                          json={"chat_id": s["telegram_chat_id"], "text": text}, timeout=10)
        return r.ok
    except requests.RequestException as e:
        log.error("telegram 전송 실패: %s", e)
        return False


def check_alerts(accounts: list[dict]) -> None:
    s = store.get_settings()
    year = store.today()[:4]

    tot = analytics.returns(store.snapshots())["total"]
    ytd = analytics.ytd_return(tot, year)
    target = s["annual_target"]
    key = f"total:{year}:{target}"
    if ytd is not None and ytd >= target and not store.alert_sent(key):
        msg = f"[청산 알림] 전체 계좌 {year}년 수익률 {ytd:.1%} — 연간 목표 {target:.0%} 달성"
        notify(msg)
        store.record_alert(key, msg)

    cost = sum(a["cost"] for a in accounts)
    r = sum(a["pnl"] for a in accounts) / cost if cost else None
    t = s["default_account_target"]
    key = f"holding:{t}"
    if r is not None and r >= t and not store.alert_sent(key):
        msg = f"[청산 알림] 전체 계좌 평가수익률 {r:.1%} — 목표 {t:.0%} 달성"
        notify(msg)
        store.record_alert(key, msg)


def refresh() -> dict:
    if not _lock.acquire(blocking=False):
        return state
    try:
        s = store.get_settings()
        src = nh.source(s)
        accounts = src.accounts()
        for a in accounts:  # 오늘 입출금은 수익률에서 뺀다
            try:
                a["flow"] = backfill.today_flows(src, a["acct"], store.today())
            except Exception:
                log.exception("오늘 입출금 조회 실패(%s)", a["acct"])
        store.save_snapshot(store.today(), accounts)
        check_alerts(accounts)
        state.update(last_update=datetime.now(store.KST).isoformat(timespec="seconds"),
                     last_error=None)
    except Exception as e:  # 다음 주기에 재시도
        log.exception("refresh 실패")
        state["last_error"] = str(e)
    finally:
        _lock.release()
    return state


def history(sym: str, market: str) -> list:
    k = (sym, market, store.today())
    if k not in _hist_cache:
        _hist_cache[k] = nh.source().history(sym, market)
    return _hist_cache[k]


def simulate(horizon: int, paths: int, drift: str) -> dict:
    s = store.get_settings()
    all_snaps = store.snapshots()
    latest = max((y["date"] for y in all_snaps), default="")
    snaps = [x for x in all_snaps if x["date"] == latest]
    total_value = sum(x["value"] for x in snaps)
    positions = [{**h, "key": f"{h['market']}:{h['sym']}"} for h in store.holdings()]
    histories, errors = {}, []
    for p in positions:
        try:
            histories[p["key"]] = history(p["sym"], p["market"])
        except Exception as e:
            errors.append(f"{p['name']}: {e}")
    cash = total_value - sum(p["value"] for p in positions)
    # 목표: 연간 목표를 시뮬레이션 기간으로 환산
    target = (1 + s["annual_target"]) ** (horizon / 252) - 1
    res = analytics.monte_carlo(positions, histories, cash, horizon, paths, drift, target)
    res["names"] = {p["key"]: p["name"] for p in positions}
    res["target"] = target
    res["history_errors"] = errors
    return res
