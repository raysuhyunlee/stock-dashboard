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


def monte_carlo(positions: list[dict], histories: dict[str, list[tuple[str, float]]],
                cash: float, horizon: int = 252, paths: int = 2000,
                drift: str = "hist", target: float | None = None, seed: int | None = None) -> dict:
    """보유종목 일별 로그수익률의 평균·공분산으로 다변량 GBM 경로를 만든다.

    positions: [{key, value}], histories: {key: [(date, close), ...]}
    결과: 전체 포트폴리오와 종목별 백분위 밴드(가치 대비 배율).
    """
    keys = [p["key"] for p in positions if len(histories.get(p["key"], [])) > 30]
    skipped = [p["key"] for p in positions if p["key"] not in keys]
    if not keys:
        return {"error": "시세 이력이 부족해 시뮬레이션할 수 없습니다.", "skipped": skipped}

    # 공통 거래일만 사용(상관관계 추정용)
    common = set.intersection(*(set(d for d, _ in histories[k]) for k in keys))
    dates = sorted(common)
    px = np.array([[dict(histories[k])[d] for d in dates] for k in keys]).T  # (T, k)
    lr = np.diff(np.log(px), axis=0)
    if len(lr) < 20:
        return {"error": "종목 간 공통 거래일이 부족합니다.", "skipped": skipped}
    mu = lr.mean(axis=0) if drift == "hist" else np.zeros(len(keys))
    cov = np.atleast_2d(np.cov(lr, rowvar=False))
    chol = np.linalg.cholesky(cov + np.eye(len(keys)) * 1e-12)

    values = np.array([next(p["value"] for p in positions if p["key"] == k) for k in keys])
    other = cash + sum(p["value"] for p in positions if p["key"] in skipped)  # 변동 없음으로 가정
    start = values.sum() + other

    rng = np.random.default_rng(seed)
    logcum = np.zeros((paths, len(keys)))
    step = max(1, horizon // 60)  # 차트용으로 60개 남짓만 저장
    days, port_bands, stock_bands = [], [], []
    for t in range(1, horizon + 1):
        logcum += mu + rng.standard_normal((paths, len(keys))) @ chol.T
        if t % step == 0 or t == horizon:
            ratio = np.exp(logcum)
            port = (ratio @ values + other) / start
            days.append(t)
            port_bands.append(np.percentile(port, PCTS))
            stock_bands.append(np.percentile(ratio, PCTS, axis=0))  # (5, k)
    final = (np.exp(logcum) @ values + other) / start - 1

    sb = np.array(stock_bands)  # (steps, 5, k)
    ann_vol = np.sqrt(np.diag(cov) * 252)
    return {
        "horizon": horizon, "paths": paths, "drift": drift, "days": days, "percentiles": PCTS,
        "total": {
            "start": start,
            "bands": np.array(port_bands).T.tolist(),  # (5, steps), 배율
            "expected": float(final.mean()),
            "p_loss": float((final < 0).mean()),
            "p_target": float((final >= target).mean()) if target is not None else None,
        },
        "stocks": {k: {"bands": sb[:, :, i].T.tolist(), "ann_vol": float(ann_vol[i]),
                       "ann_drift": float(mu[i] * 252)} for i, k in enumerate(keys)},
        "skipped": skipped,
        "history_days": len(lr),
    }
