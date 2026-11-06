"""FastAPI 서버: 대시보드 API + 주기 수집 스케줄러."""
from __future__ import annotations

import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import analytics, jobs, nh, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
REFRESH_MIN = int(os.environ.get("REFRESH_MINUTES", "10"))
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_):
    store.init()
    logging.info("DB: %s", store.DB_PATH)
    if not PASSWORD:
        logging.warning("DASHBOARD_PASSWORD 미설정 — 인증 없이 열립니다. 서버에서는 반드시 설정하세요.")
    sched = BackgroundScheduler(timezone=store.KST)
    sched.add_job(jobs.refresh, "interval", minutes=REFRESH_MIN, next_run_time=None)
    sched.start()
    threading.Thread(target=jobs.refresh, daemon=True).start()
    yield
    sched.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
basic = HTTPBasic(auto_error=False)


def auth(cred: HTTPBasicCredentials | None = Depends(basic)):
    if not PASSWORD:
        return
    if not cred or not secrets.compare_digest(cred.password.encode(), PASSWORD.encode()):
        raise HTTPException(401, "인증 필요", headers={"WWW-Authenticate": "Basic"})


api = Depends(auth)


class SettingsIn(BaseModel):
    app_key: str | None = None
    app_secret: str | None = None
    brand: str | None = Field(None, pattern="^(nhplug|n2plug)$")
    annual_target: float | None = Field(None, gt=0)
    default_account_target: float | None = Field(None, gt=0)
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


class TargetIn(BaseModel):
    target: float | None = Field(None, gt=0)  # None 이면 기본값으로 되돌림


@app.get("/api/settings", dependencies=[api])
def get_settings():
    return store.public_settings()


@app.put("/api/settings", dependencies=[api])
def put_settings(body: SettingsIn):
    patch = body.model_dump(exclude_none=True)
    # 빈 문자열 시크릿은 "변경 안 함"으로 처리
    patch = {k: v for k, v in patch.items() if not (k in store.SECRET_KEYS | {"app_key"} and v == "")}
    before = store.get_settings()
    after = store.update_settings(patch)
    source_keys = ("app_key", "brand")
    if any(before[k] != after[k] for k in source_keys):
        store.clear_data()  # 다른 사용자의 계좌 데이터가 섞이지 않게
        threading.Thread(target=jobs.refresh, daemon=True).start()
    return store.public_settings()


@app.post("/api/settings/test", dependencies=[api])
def test_connection():
    try:
        src = nh.NHSource(store.get_settings())
        return {"ok": True, "accounts": src.account_numbers()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/notify/test", dependencies=[api])
def test_notify():
    return {"ok": jobs.notify("[테스트] 주식 모니터 알림이 정상적으로 설정되었습니다.")}


@app.put("/api/accounts/{acct}/target", dependencies=[api])
def put_target(acct: str, body: TargetIn):
    t = dict(store.get_settings()["account_targets"])
    if body.target is None:
        t.pop(acct, None)
    else:
        t[acct] = body.target
    store.update_settings({"account_targets": t})
    return {"acct": acct, "target": body.target}


@app.post("/api/refresh", dependencies=[api])
def refresh():
    return jobs.refresh()


@app.get("/api/summary", dependencies=[api])
def summary():
    s = store.get_settings()
    snaps = store.snapshots()
    ret = analytics.returns(snaps)
    latest = max((x["date"] for x in snaps), default=None)
    accts = [x for x in snaps if x["date"] == latest]
    tot = ret["total"]
    value = sum(a["value"] for a in accts)
    cost = sum(a["cost"] for a in accts)
    pnl = sum(a["pnl"] for a in accts)
    return {
        "status": jobs.state,
        "date": latest,
        "total": {
            "value": value, "cost": cost, "pnl": pnl,
            "pnl_rate": pnl / cost if cost else None,          # 보유 평가수익률
            "daily": tot[-1]["daily"] if len(tot) > 1 else None,
            "period": tot[-1]["cumulative"] if tot else None,  # 기록 시작 이후 누적
            "since": tot[0]["date"] if tot else None,
            "ytd": analytics.ytd_return(tot, store.today()[:4]),
            "annual_target": s["annual_target"],
        },
        "accounts": [{
            **a,
            "pnl_rate": a["pnl"] / a["cost"] if a["cost"] else None,
            "daily": (ret["accounts"][a["acct"]][-1]["daily"]
                      if len(ret["accounts"][a["acct"]]) > 1 else None),
            "target": s["account_targets"].get(a["acct"], s["default_account_target"]),
            "target_custom": a["acct"] in s["account_targets"],
        } for a in accts],
        "holdings": store.holdings(),
        "alerts": store.alerts(),
    }


@app.get("/api/returns", dependencies=[api])
def returns():
    return analytics.returns(store.snapshots())


@app.get("/api/montecarlo", dependencies=[api])
def montecarlo(horizon: int = 252, paths: int = 2000, drift: str = "hist"):
    if not (5 <= horizon <= 756 and 100 <= paths <= 10000 and drift in ("hist", "zero")):
        raise HTTPException(400, "horizon 5–756, paths 100–10000, drift hist|zero")
    return jobs.simulate(horizon, paths, drift)


@app.get("/", dependencies=[api])
def index():
    page = STATIC / "index.html"
    if not page.exists():
        return {"message": "UI 준비 중 — /docs 에서 API 를 확인하세요."}
    return FileResponse(page)


def run():
    """uv run stock-monitor — HOST/PORT 환경변수로 바꿀 수 있다."""
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "7777")))
