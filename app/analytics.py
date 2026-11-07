"""수익률 집계와 몬테카를로 시뮬레이션."""
from __future__ import annotations

from collections import defaultdict

import numpy as np

PCTS = (5, 25, 50, 75, 95)


def returns(snaps: list[dict]) -> dict:
    """일별 스냅샷 → 전체 계좌 합산 일간/누적 수익률(시간가중).

    그날 외부 입출금(flow)은 수익에서 뺀다. 입금은 장 시작, 출금은 장 마감에 일어난 것으로 본다:
    r = (V_t − V_{t−1} − F) / (V_{t−1} + max(F, 0)).
    """
    total: dict[str, float] = defaultdict(float)
    flow: dict[str, float] = defaultdict(float)
    for s in snaps:
        total[s["date"]] += s["value"] or 0
        flow[s["date"]] += s.get("flow") or 0

    out, cum, prev = [], 1.0, None
    for d, v in sorted(total.items()):
        f = flow[d]
        base = (prev or 0) + max(f, 0)
        r = (v - (prev or 0) - f) / base if prev and base > 0 else 0.0
        cum *= 1 + r
        out.append({"date": d, "value": v, "flow": flow[d], "daily": r, "cumulative": cum - 1})
        prev = v or None
    return {"total": out}


def ytd_return(total_series: list[dict], year: str) -> float | None:
    pts = [p for p in total_series if p["date"].startswith(year)]
    if not pts:
        return None
    # 연초 기준: 전년도 마지막 누적값이 있으면 그것, 없으면 올해 첫 스냅샷
    prev = [p for p in total_series if p["date"] < year + "0101"]
    base = prev[-1]["cumulative"] if prev else pts[0]["cumulative"]
    return (1 + pts[-1]["cumulative"]) / (1 + base) - 1


def annualized(total_series: list[dict]) -> float | None:
    """누적 시간가중 수익률을 기간(달력일) 기준 연 복리로 환산. 30일 미만이면 None."""
    if len(total_series) < 2:
        return None
    from datetime import date
    d = lambda s: date(int(s[:4]), int(s[4:6]), int(s[6:]))
    days = (d(total_series[-1]["date"]) - d(total_series[0]["date"])).days
    if days < 30:
        return None
    return (1 + total_series[-1]["cumulative"]) ** (365 / days) - 1


def monte_carlo(daily: list[float], start: float, horizon: int = 252, paths: int = 2000,
                target: float | None = None, seed: int | None = None) -> dict:
    """계좌 일간 수익률(시간가중, 입출금 제외)의 평균·분산으로 GBM 경로를 만든다.

    실제 거래(매수·매도·비중 변화)가 반영된 계좌 전체 수익률 기준이다. 결과는 현재 가치 대비 배율.
    """
    lr = np.log1p(np.asarray(daily, dtype=float))
    if len(lr) < 20:
        return {"error": "계좌 수익률 기록이 부족해 시뮬레이션할 수 없습니다."}
    mu = lr.mean()
    sd = lr.std(ddof=1)

    rng = np.random.default_rng(seed)
    logcum = np.zeros(paths)
    step = max(1, horizon // 60)  # 차트용으로 60개 남짓만 저장
    days, bands = [], []
    for t in range(1, horizon + 1):
        logcum += mu + sd * rng.standard_normal(paths)
        if t % step == 0 or t == horizon:
            days.append(t)
            bands.append(np.percentile(np.exp(logcum), PCTS))
    final = np.exp(logcum) - 1
    return {
        "horizon": horizon, "paths": paths, "days": days, "percentiles": PCTS,
        "total": {
            "start": start,
            "bands": np.array(bands).T.tolist(),  # (5, steps), 배율
            "expected": float(final.mean()),
            "p_loss": float((final < 0).mean()),
            "p_target": float((final >= target).mean()) if target is not None else None,
            "ann_vol": float(sd * np.sqrt(252)),
            "ann_drift": float(mu * 252),
        },
        "history_days": len(lr),
    }
