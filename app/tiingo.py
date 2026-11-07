"""Tiingo 미국 주식 일별 종가(백필용).

NH 해외 일봉은 약 3년치뿐이라 과거 해외 시세는 Tiingo 를 쓴다. 거래내역 수량은 당시
실제 수량이므로 분할 조정 전 원래 종가(close)를 쓴다(adjClose 아님).
"""
from __future__ import annotations

import requests

URL = "https://api.tiingo.com/tiingo/daily/{}/prices"


class NotFound(Exception):
    pass


class TiingoError(Exception):
    pass


def history(sym: str, since: str, key: str) -> list[tuple[str, float]]:
    """since(YYYYMMDD)부터 오늘까지 [(YYYYMMDD, 원래 종가)]."""
    ticker = sym.replace(".", "-")  # BRK.B → BRK-B
    try:
        r = requests.get(URL.format(ticker), timeout=30, params={
            "startDate": f"{since[:4]}-{since[4:6]}-{since[6:]}", "token": key})
    except requests.RequestException as e:
        raise TiingoError(f"Tiingo 연결 실패: {e}") from e
    if r.status_code == 404:
        raise NotFound(sym)
    if r.status_code in (401, 403):
        raise TiingoError("Tiingo 키가 올바르지 않습니다. 설정에서 키를 확인하세요.")
    if r.status_code == 429:
        raise TiingoError("Tiingo 호출 한도를 넘었습니다. 한 시간쯤 뒤에 다시 시도하세요.")
    if not r.ok:
        raise TiingoError(f"Tiingo 시세 조회 실패({sym}): HTTP {r.status_code}")
    return sorted((row["date"][:10].replace("-", ""), float(row["close"]))
                  for row in r.json() if row.get("close"))
