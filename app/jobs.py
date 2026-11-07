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


def simulate(paths: int, years: int = 0) -> dict:
    """years=0 이면 기록 전체 기간."""
    s = store.get_settings()
    series = analytics.returns(store.snapshots())["total"]
    if not series:
        return {"error": "계좌 기록이 없습니다."}
    since = "00000000"
    if years:
        today = datetime.strptime(store.today(), "%Y%m%d")
        since = today.replace(year=today.year - years, day=min(today.day, 28)).strftime("%Y%m%d")
    daily = [p["daily"] for p in series[1:] if p["date"] >= since]  # 첫날은 수익률 0이라 제외
    target = s["annual_target"]  # 시뮬레이션 기간은 1년(252거래일) 고정
    res = analytics.monte_carlo(daily, series[-1]["value"], 252, paths, target)
    res["target"] = target
    res["years"] = years
    res["since"] = max(since, series[1]["date"]) if len(series) > 1 else since
    return res
