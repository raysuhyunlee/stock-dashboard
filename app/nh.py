"""데이터 소스: NH PLUG REST API 실계좌 조회.

주문 API 는 이 모듈에서 호출하지 않는다 (모니터링 전용).
"""
from __future__ import annotations

import os

from . import store


def _f(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


class ApiError(Exception):
    pass


# ================================================================ 실계좌

class NHSource:
    def __init__(self, s: dict):
        if not (s["app_key"] and s["app_secret"]):
            raise ApiError("API 키가 설정되지 않았습니다. 설정에서 앱키와 시크릿을 입력하세요.")
        brand = s["brand"]
        os.environ.update({
            "NHPLUG_APP_KEY": s["app_key"],
            "NHPLUG_APP_SECRET": s["app_secret"],
            "NHPLUG_BASE_URL": f"https://api.{brand}.com:8443",
            "NHPLUG_AUTH_URL": f"https://api.{brand}.com:8443",
            "NHPLUG_TOKEN_CACHE_DIR": str(store.DATA_DIR),
        })

    def _call(self, path: str, body: dict) -> dict:
        from nhplug import NhplugError, call
        try:
            return call(path, body, timeout=15)
        except NhplugError as e:
            raise ApiError(f"{path}: {e}") from e

    def account_numbers(self) -> list[str]:
        data = self._call("/n2/acctinfo", {})
        # 01 일반 · 02 주문대리인 (03 모의투자 계좌는 제외)
        out = [a.get("acct_no") for a in data.get("Output_0") or []
               if (a.get("acct_type") or "").strip() in {"01", "02"} and a.get("acct_no")]
        if not out:
            raise ApiError(f"사용 가능한 계좌가 없습니다: {data.get('rsp_msg') or data}")
        return out

    def account(self, acct: str) -> dict:
        kr = self._call("/krstock/inquiry/v1/balance", {
            "act_no": acct, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "9",
            "aet_bse": "2", "qut_dit_cd": "UNT", "aly_qut_cd": "1",
        })
        if not isinstance(kr.get("Output_0"), dict):
            raise ApiError(f"국내 잔고 조회 실패({acct}): {kr.get('rsp_msg')}")
        k0 = kr["Output_0"]
        holdings = [{
            "sym": h["iem_cd"].strip(), "market": "KR", "name": h.get("iem_nm", "").strip(),
            "qty": _f(h.get("itg_bnc_qty")), "value": _f(h.get("eal_amt")),
            "cost": _f(h.get("eal_amt")) - _f(h.get("eal_pls_amt")),
        } for h in kr.get("Output_1") or [] if _f(h.get("itg_bnc_qty")) > 0]
        value, cost, pnl = _f(k0.get("tot_aet_amt")), _f(k0.get("tot_byn_amt")), _f(k0.get("tot_eal_pls"))

        # 해외(미국) — 해외 거래가 없는 계좌는 빈 응답일 수 있다
        try:
            us = self._call("/gbstock/inquiry/v1/balance", {
                "act_no": acct, "qut_iqr_dit_cd": "9", "fc_sec_trd_nat_cd": "200",
                "cur_cd": "USD", "xns_dit_cd": "1",
            })
        except ApiError:
            us = {}
        u0 = us.get("Output_0") if isinstance(us.get("Output_0"), dict) else {}
        for h in us.get("Output_1") or []:
            qty = _f(h.get("cns_bse_bnc_qty"))
            if qty <= 0:
                continue
            v = _f(h.get("krw_eal_amt"))
            holdings.append({
                "sym": h["iem_cd"].strip(), "market": "US", "name": h.get("iem_nm", "").strip(),
                "qty": qty, "value": v, "cost": v - _f(h.get("krw_eal_pls_amt")),
            })
        value += _f(u0.get("eal_amt_sum"))
        cost += _f(u0.get("abk_amt"))
        pnl += _f(u0.get("eal_pls_sum_amt"))
        return {"acct": acct, "value": value, "cost": cost, "pnl": pnl, "holdings": holdings}

    def accounts(self) -> list[dict]:
        return [self.account(a) for a in self.account_numbers()]

    def history(self, sym: str, market: str, n: int = 250) -> list[tuple[str, float]]:
        end = store.today()
        if market == "KR":
            d = self._call("/krstock/quote/v1/period", {
                "market_cd": "KRX", "iem_cd": sym, "edate": end, "array_cnt": str(n),
                "gubun": "1", "view_main_yn": "Y",
            })
            rows = [(r.get("bsop_date"), _f(r.get("stck_prpr"))) for r in d.get("Output_1") or []]
        else:
            d = self._call("/gbstock/quote/v1/period", {
                "iem_cd": sym, "end_dt": end, "count": str(n), "gubun": "3",
                "xtick": "0001", "today_cls": "0", "market_cls": "1",
            })
            rows = [(r.get("trade_date") or r.get("bsop_date"), _f(r.get("close_prc")))
                    for r in d.get("Output_1") or []]
        return sorted((dt, p) for dt, p in rows if dt and p > 0)


def source(s: dict | None = None) -> NHSource:
    return NHSource(s or store.get_settings())
