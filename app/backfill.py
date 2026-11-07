"""과거 일별 총자산 재구성(백필)과 외부 입출금(flow) 집계.

NH API 에는 과거 일별 총자산 조회가 없어서 종합거래내역(8203)·해외 일별거래내역의
거래 후 잔고·수량으로 날짜별 보유 상태를 만들고, 기간별 시세·환율 종가로 평가한다.
근사치다: 장중 체결 순서·미결제 금액·수수료 정산 시점은 반영하지 않는다.

백필은 사용자가 버튼으로 요청할 때만 백그라운드 작업으로 돈다(진행률은 job 참고).
"""
from __future__ import annotations

import bisect
import json
import logging
import os
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from . import nh, store, tiingo

log = logging.getLogger("monitor")
START = "20150101"  # 보유 상태를 만들려면 선택 기간 이전 거래내역도 필요하다


def _ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def _shift(date: str, days: int) -> str:
    return _ymd(datetime.strptime(date, "%Y%m%d") + timedelta(days=days))


def _is_cma(r: dict) -> bool:
    return (r.get("iem_cd") or "").startswith("NHKRCMA") or "발행어음" in (r.get("iem_nm") or "")


def _flow_sign(name: str) -> int:
    """외부 입출금이면 +1/-1, 아니면 0. 배당·이자·수수료는 수익으로 본다."""
    if not any(w in name for w in ("대체", "이체", "현금입금", "현금출금")):
        return 0
    return 1 if ("입금" in name or "입고" in name) else -1


class _Series:
    """날짜 → 값, 직전 값으로 채움. 첫 값 이전은 before."""

    def __init__(self, points: list[tuple[str, float]], before: float | None = None):
        self.d = [p[0] for p in points]
        self.v = [p[1] for p in points]
        self.before = before

    def at(self, date: str) -> float | None:
        i = bisect.bisect_right(self.d, date) - 1
        return self.v[i] if i >= 0 else self.before


def _ledger(src: nh.NHSource, acct: str, end: str, progress) -> tuple[list, list]:
    """거래내역을 1년 단위로 끊어 받는다(연속조회는 pages 가 처리). progress(0~1)."""
    windows, y = [], int(START[:4])
    while f"{y}0101" <= end:
        windows.append((f"{y}0101", min(f"{y}1231", end)))
        y += 1
    tt, gb = [], []
    for i, (a, b) in enumerate(windows):
        tt += src.pages("/common/inquiry/v1/totalTransaction", {
            "iqr_tp_cd": "1", "iqr_rge_cd": "2", "act_no": acct, "iqr_sta_dt": a,
            "iqr_end_dt": b, "iem_llf_cd": "00", "act_trd_dtl_cd": "00"})
        if tt:  # 첫 거래 이전에는 해외 내역도 없다
            gb += src.pages("/gbstock/inquiry/v1/dailyTransaction", {
                "act_no": acct, "iqr_sta_dt": a, "iqr_end_dt": b,
                "act_trd_cfc_cd": "00", "iem_mlf_cd": "00001"})
        progress((i + 1) / len(windows), f"거래내역 {a[:4]}년")
    return tt, gb


def _flows(tt: list, px, fx: _Series) -> dict[str, float]:
    """날짜별 외부 입출금(원화 환산). 현물 대체입고는 그날 종가로 평가."""
    out: dict[str, float] = defaultdict(float)
    for r in tt:
        name = r.get("sps_cd_krl_anm") or ""
        sign = _flow_sign(name)
        if not sign:
            continue
        d, usd = r["trd_dt"], r.get("cur_cd") == "USD"
        rate = (fx.at(d) or 0) if usd else 1.0
        if nh._f(r.get("trd_qty")) and r.get("iem_cd"):  # 현물 입출고
            p = px(r, d)
            amt = nh._f(r["trd_qty"]) * (p if p is not None else nh._f(r.get("trd_uit_pr")) * rate)
        else:
            amt = nh._f(r.get("trd_amt")) * rate
        out[d] += sign * amt
    return dict(out)


def _kr_sym(code: str) -> str:
    code = code.strip()
    return code[3:9] if code.startswith("KR7") else code.lstrip("A")


def reconstruct(src: nh.NHSource, acct: str, start: str, end: str,
                progress=lambda f, m: None, tiingo_key: str = "") -> tuple[dict, int, list, list]:
    """start~end 날짜별 {value, flow, positions}, 시세가 없어 건너뛴 날 수,
    Tiingo 에 없는 해외 종목, 처음부터 end 까지의 거래내역."""
    tt, gb = _ledger(src, acct, end, lambda f, m: progress(0.6 * f, m))
    if not tt:
        return {}, 0, [], []
    txns = _transactions(tt, gb)
    start = max(start, tt[0]["trd_dt"])
    if start > end:
        return {}, 0, [], txns

    # --- 상태 변화 이벤트(날짜순) -----------------------------------------
    krw_ev: list[tuple[str, float]] = []           # 원화 예수금
    cma_ev: list[tuple[str, float]] = []           # 발행어음 잔고
    qty_ev: dict[tuple, list] = defaultdict(list)  # (sym, market) → [(date, qty)]
    for r in tt:
        d, af = r["trd_dt"], nh._f(r.get("trd_af_dca"))
        if _is_cma(r):
            cma_ev.append((d, nh._f(r.get("trd_af_bnc_qty"))))
            krw_ev.append((d, af))
        elif not r.get("cur_cd"):
            krw_ev.append((d, af))
            if (r.get("iem_cd") or "").strip() and nh._f(r.get("trd_qty")):
                qty_ev[(_kr_sym(r["iem_cd"]), "KR")].append((d, nh._f(r.get("trd_af_bnc_qty"))))
        elif af > 0:  # 환전·배당 원천세 등 원화 예수금이 바뀐 외화 거래
            krw_ev.append((d, af))
    # 시세가 없는 국내 보유분(펀드·RP·상장폐지 종목 등)용: 마지막 거래 단가
    trade_px: dict[tuple, list] = defaultdict(list)
    for r in tt:
        if not r.get("cur_cd") and not _is_cma(r) and (r.get("iem_cd") or "").strip() \
                and nh._f(r.get("trd_qty")) and nh._f(r.get("trd_uit_pr")) > 0:
            trade_px[(_kr_sym(r["iem_cd"]), "KR")].append((r["trd_dt"], nh._f(r["trd_uit_pr"])))
    for r in gb:
        if nh._f(r.get("trd_qty")):
            sym = (r.get("iem_cd") or "").split()[0]
            qty_ev[(sym, "US")].append((r["trd_dt"], nh._f(r.get("trd_af_bnc_qty"))))
    qty = {k: _Series(v, 0.0) for k, v in qty_ev.items()}
    fallback = {k: _Series(v, v[0][1]) for k, v in trade_px.items() if v}  # 국내만
    missing: set[str] = set()  # Tiingo 에 없는 해외 종목(그 종목을 보유한 날은 건너뜀)
    approx: dict[str, int] = defaultdict(int)  # 거래 단가로 대신 평가한 종목 → 일수

    # --- 시세: 기간 안에 보유한 종목만 ------------------------------------
    since = _shift(start, -10)
    held = [k for k, q in qty.items()
            if (q.at(start) or 0) > 0 or any(start <= d <= end and v > 0 for d, v in zip(q.d, q.v))]
    fx = _Series(src.fx_history("KRW", since=since))
    hist: dict[tuple, _Series] = {}
    for i, (sym, market) in enumerate(held):
        pts = None
        if market == "US":  # 해외는 Tiingo 원래 종가만 쓴다(대체 소스 없음)
            if not tiingo_key:
                raise nh.ApiError("해외주식 과거 시세에 Tiingo 키가 필요합니다. 설정 > API 에서 입력하세요.")
            try:
                pts = tiingo.history(sym, since, tiingo_key)
            except tiingo.NotFound:
                missing.add(sym)
            except tiingo.TiingoError as e:
                raise nh.ApiError(str(e)) from e
        else:
            try:
                pts = src.history(sym, market, since=since)
            except nh.ApiError:  # 시세 조회가 안 되는 국내 상품 — 거래 단가로 대신한다
                log.warning("시세 없음: %s %s", market, sym)
        if pts:
            hist[(sym, market)] = _Series(pts)
        progress(0.6 + 0.4 * (i + 1) / len(held), f"시세 {sym}")

    def price_krw(sym: str, market: str, d: str, count: bool = False) -> float | None:
        s = hist.get((sym, market))
        p = s.at(d) if s else None
        if p is None and (sym, market) in fallback:
            p = fallback[(sym, market)].at(d)
            if count:
                approx[sym] += 1
        if p is None:
            return None
        rate = fx.at(d)
        return (p * rate if rate else None) if market == "US" else p

    # 외화 예수금: 해외 일별거래내역의 거래 전·후 잔고가 기준점. 그 내역에 없는
    # 환전·외화대체는 종합거래내역 금액으로 추정해 더하고, 다음 기준점에서 바로잡는다.
    steps: list[tuple[str, int, str, float]] = []  # (date, 순서, set|add, 값)
    for r in tt:
        name = r.get("sps_cd_krl_anm") or ""
        if r.get("cur_cd") != "USD" or nh._f(r.get("trd_qty")) or "배당" in name:
            continue
        amt, d = nh._f(r.get("trd_amt")), r["trd_dt"]
        if "외화매수" in name or "외화매도" in name:  # 금액이 원화
            amt = amt / (fx.at(d) or 1)
        sign = -1 if ("매도" in name or "출금" in name) else 1
        steps.append((d, 0, "add", sign * amt))
    for i, r in enumerate(gb):
        steps.append((r["trd_dt"], 1 + 2 * i, "set", nh._f(r.get("trd_bf_fc_dca"))))
        steps.append((r["trd_dt"], 2 + 2 * i, "set", nh._f(r.get("trd_af_fc_dca"))))
    usd_ev, cur = [], 0.0
    for d, _, op, x in sorted(steps):
        cur = x if op == "set" else cur + x
        usd_ev.append((d, cur))
    usd_now = src.usd_cash(acct) if gb else None  # 마지막 해외 거래 이후 환전분까지 맞춘다
    if usd_now is not None:
        usd_ev.append((store.today(), usd_now))
    krw, cma, usd = _Series(krw_ev, 0.0), _Series(cma_ev, 0.0), _Series(usd_ev, 0.0)

    # 대체입고 평가용: 종합거래내역의 해외종목은 ISIN 이라 해외 내역의 티커와 이어 준다
    isin_to_ticker = {}
    for r in tt:
        if r.get("cur_cd") == "USD" and r.get("iem_cd") and nh._f(r.get("trd_qty")):
            for g in gb:
                if g["trd_dt"] == r["trd_dt"] and nh._f(g.get("trd_qty")) == nh._f(r["trd_qty"]):
                    isin_to_ticker[r["iem_cd"]] = (g.get("iem_cd") or "").split()[0]

    def px(r: dict, d: str) -> float | None:
        if r.get("cur_cd") == "USD":
            t = isin_to_ticker.get(r["iem_cd"])
            return price_krw(t, "US", d) if t else None
        return price_krw(_kr_sym(r["iem_cd"]), "KR", d)

    flows = _flows([r for r in tt if start <= r["trd_dt"] <= end], px, fx)
    names = {(t["sym"], t["market"]): t["name"] for t in txns if t["sym"] and t["name"]}

    # --- 평가일: 환율 기준 영업일 + 입출금일 ------------------------------
    days = sorted({d for d in fx.d if start <= d <= end} | set(flows))
    out, skipped = {}, 0
    for d in days:
        rate = fx.at(d)
        k, c, u = krw.at(d) or 0, cma.at(d) or 0, usd.at(d) or 0
        pos = [[s_, "CASH", n_, q_, q_ * r_] for s_, n_, q_, r_ in (
            ("KRW", "원화 예수금", k, 1), ("CMA", "발행어음", c, 1), ("USD", "외화 예수금", u, rate or 0))
            if q_]
        ok = rate is not None or not u
        for (sym, market), q in qty.items():
            if n_ := q.at(d):
                p = price_krw(sym, market, d, count=True)
                ok = ok and p is not None
                pos.append([sym, market, names.get((sym, market), ""), n_, n_ * (p or 0)])
        v = sum(x[4] for x in pos)
        if not ok:  # 시세가 없으면 값이 틀리므로 건너뛴다
            skipped += 1
        elif v > 0 or flows.get(d):  # 빈 계좌였던 기간은 건너뛴다
            out[d] = {"value": v, "flow": flows.get(d, 0.0), "positions": pos}
    if approx:
        log.info("거래 단가로 평가: %s", dict(approx))
    if missing:
        log.warning("Tiingo 에 없는 해외 종목: %s", sorted(missing))
    return out, skipped, sorted(missing), txns


def _transactions(tt: list, gb: list) -> list[dict]:
    """저장용 거래내역. 해외 종목은 종합거래내역에선 ISIN 이라 해외 내역 티커로 바꾼다."""
    isin = {}
    for r in tt:
        if r.get("cur_cd") == "USD" and r.get("iem_cd") and nh._f(r.get("trd_qty")):
            for g in gb:
                if g["trd_dt"] == r["trd_dt"] and nh._f(g.get("trd_qty")) == nh._f(r["trd_qty"]):
                    isin[r["iem_cd"]] = (g.get("iem_cd") or "").split()[0]
    out = []
    for r in tt:
        code, usd = (r.get("iem_cd") or "").strip(), r.get("cur_cd") == "USD"
        if _is_cma(r):
            sym, market = "CMA", "CASH"
        elif not code:
            sym = market = None
        else:
            sym, market = (isin.get(code, code), "US") if usd else (_kr_sym(code), "KR")
        out.append({"src": "tt", "date": r["trd_dt"], "kind": r.get("sps_cd_krl_anm") or "",
                    "sym": sym, "market": market, "name": (r.get("iem_nm") or "").strip(),
                    "currency": r.get("cur_cd") or "KRW", "qty": nh._f(r.get("trd_qty")),
                    "price": nh._f(r.get("trd_uit_pr")), "amount": nh._f(r.get("trd_amt")),
                    "cash_after": nh._f(r.get("trd_af_dca")),
                    "qty_after": nh._f(r.get("trd_af_bnc_qty")),
                    "raw": json.dumps(r, ensure_ascii=False)})
    for r in gb:
        sym = (r.get("iem_cd") or "").split()
        out.append({"src": "gb", "date": r["trd_dt"],
                    "kind": r.get("sps_cd_nm") or r.get("act_trd_tp_nm") or "",
                    "sym": sym[0] if sym else None, "market": "US" if sym else None,
                    "name": (r.get("iem_krl_nm") or r.get("oss_iem_nm") or "").strip(),
                    "currency": "USD", "qty": nh._f(r.get("trd_qty")),
                    "price": nh._f(r.get("trd_uit_pr")), "amount": nh._f(r.get("fc_trd_amt")),
                    "cash_after": nh._f(r.get("trd_af_fc_dca")),
                    "qty_after": nh._f(r.get("trd_af_bnc_qty")),
                    "raw": json.dumps(r, ensure_ascii=False)})
    return sorted(out, key=lambda t: t["date"])  # 같은 날 안의 원래 순서는 유지


def today_flows(src: nh.NHSource, acct: str, date: str) -> float:
    """주기 수집용: 오늘 외부 입출금 합계(원화 환산). 현물 입고분은 거래 단가로 평가."""
    tt = src.pages("/common/inquiry/v1/totalTransaction", {
        "iqr_tp_cd": "1", "iqr_rge_cd": "2", "act_no": acct, "iqr_sta_dt": date,
        "iqr_end_dt": date, "iem_llf_cd": "00", "act_trd_dtl_cd": "00"})
    if not any(_flow_sign(r.get("sps_cd_krl_anm") or "") for r in tt):
        return 0.0
    fx = _Series(src.fx_history("KRW", 5))
    return _flows(tt, lambda r, d: None, fx).get(date, 0.0)


# ================================================================ 실행

def _collect(src: nh.NHSource, start: str, end: str, progress,
             tiingo_key: str = "") -> dict[str, dict]:
    accts = src.account_numbers()
    out = {}
    for i, acct in enumerate(accts):
        def sub(f, m, i=i):
            progress((i + f) / len(accts), f"계좌 {i + 1}/{len(accts)} · {m}")
        rows, skipped, missing, txns = reconstruct(src, acct, start, end, sub, tiingo_key)
        out[acct] = {"rows": rows, "skipped": skipped, "missing": missing,
                     "txns": txns, "until": end}
    return out


def _save(collected: dict[str, dict]) -> dict:
    """실측 스냅샷 값은 덮어쓰지 않고 flow 만 갱신."""
    result = {}
    for acct, c in collected.items():
        rows = c["rows"]
        store.save_backfill(acct, rows, c["txns"], c["until"])
        result[acct] = {"days": len(rows), "since": min(rows, default=None),
                        "skipped": c["skipped"], "missing": c.get("missing", [])}
        log.info("backfill %s: %d일 (%s~), 건너뜀 %d", acct, len(rows),
                 result[acct]["since"], c["skipped"])
    return result


def _external(creds: dict, start: str, end: str, progress) -> dict[str, dict]:
    """다른 계정(예: 이전한 N2 계좌)의 기록을 일회성으로 가져온다.

    자격증명은 DB·환경변수·토큰 캐시 어디에도 남기지 않도록 별도 프로세스에 stdin 으로만
    넘긴다. 그 프로세스는 토큰 파일 캐시를 끈 채 stderr 로 진행률, stdout 으로 결과를 준다.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NHPLUG_", "APP_"))}
    env.update(NHPLUG_TOKEN_CACHE="0", NHPLUG_ENV_FILE=os.devnull)
    p = subprocess.Popen([sys.executable, "-m", "app.backfill"], env=env,
                         cwd=Path(__file__).resolve().parent.parent, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(json.dumps({**creds, "start": start, "end": end}))
    p.stdin.close()
    # 결과(stdout)가 파이프 버퍼보다 크면 자식이 쓰기에서 막히므로 따로 읽는다
    out_buf: list[str] = []
    reader = threading.Thread(target=lambda: out_buf.append(p.stdout.read()), daemon=True)
    reader.start()
    error = None
    for line in p.stderr:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "error" in msg:
            error = msg["error"]
        else:
            progress(msg["f"], msg["m"])
    reader.join()
    out = out_buf[0] if out_buf else ""
    if p.wait() or error:
        raise nh.ApiError(error or "다른 계정 조회 실패")
    return json.loads(out)


job: dict = {"running": False, "progress": 0.0, "message": "", "result": None, "error": None}
_job_lock = threading.Lock()


def start(start_date: str | None, end_date: str | None, creds: dict | None = None) -> dict:
    """백필 작업을 백그라운드로 시작한다. 기간 기본값: 처음부터 어제까지."""
    yesterday = _shift(store.today(), -1)
    s, e = start_date or START, min(end_date or yesterday, yesterday)
    if s > e:
        raise ValueError("시작일이 종료일보다 늦습니다(종료일은 어제까지).")
    with _job_lock:
        if job["running"]:
            raise RuntimeError("이미 불러오는 중입니다.")
        job.update(running=True, progress=0.0, message="시작", result=None, error=None)

    def progress(f: float, m: str):
        job.update(progress=round(min(f, 1.0), 3), message=m)

    def work():
        try:
            key = store.get_settings()["tiingo_key"]
            collected = (_external({**creds, "tiingo_key": key}, s, e, progress) if creds
                         else _collect(nh.source(), s, e, progress, key))
            job.update(result=_save(collected), progress=1.0, message="완료")
        except Exception as ex:  # 화면에 그대로 보여 준다
            log.exception("backfill 실패")
            job["error"] = str(ex)
        finally:
            job["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return job


if __name__ == "__main__":  # _external 의 하위 프로세스
    req = json.load(sys.stdin)
    secret = req["app_secret"]

    def report(f, m):
        print(json.dumps({"f": f, "m": m}, ensure_ascii=False), file=sys.stderr, flush=True)
    try:
        out = _collect(nh.NHSource(req), req["start"], req["end"], report, req.get("tiingo_key", ""))
    except Exception as ex:
        msg = str(ex)
        for k in (secret, req.get("tiingo_key", "")):
            if len(k) >= 8:
                msg = msg.replace(k, "***")
        print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr, flush=True)
        sys.exit(1)
    json.dump(out, sys.stdout)
