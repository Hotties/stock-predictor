"""지수·환율 수집. DB에는 우리 이름(KOSPI 등)으로 저장하고, FinanceDataReader 심볼과 이어 준다."""
from __future__ import annotations

import time
from typing import Callable

import pandas as pd

from ..settings import CollectorSettings
from .common import download, load_fdr, normalize_date_index
from .prices import with_change_pct

# DB 심볼 -> FinanceDataReader 심볼 (M0 점검에서 모두 조회됨을 확인)
FDR_SYMBOLS = {
    "KOSPI": "KS11",
    "KOSDAQ": "KQ11",
    "US500": "US500",
    "IXIC": "IXIC",
    "USDKRW": "USD/KRW",
}
INDEX_COLUMNS = ["close", "change_pct"]


def fetch_index_daily(
    symbol: str,
    start,
    end=None,
    *,
    prev_close: float | None = None,
    fdr=None,
    settings: CollectorSettings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """지수·환율 종가를 받는다. 반환: 날짜 인덱스, 컬럼 close, change_pct.

    change_pct는 원천의 Change 컬럼(미국·환율에는 없음)을 쓰지 않고 종가로 직접 계산한다.
    첫 행의 등락률은 prev_close(DB에 있는 직전 종가)를 주면 계산한다.
    """
    if symbol not in FDR_SYMBOLS:
        raise ValueError(f"알 수 없는 지수 심볼입니다: {symbol!r} (가능: {sorted(FDR_SYMBOLS)})")
    settings = settings or CollectorSettings()
    fdr = fdr or load_fdr()
    raw = download(fdr, FDR_SYMBOLS[symbol], start, end, settings, sleep)
    if raw is None or len(raw) == 0 or "Close" not in raw.columns:
        empty = pd.DataFrame(columns=INDEX_COLUMNS, dtype="float64")
        empty.index = pd.DatetimeIndex([], name="trade_date")
        return empty
    df = normalize_date_index(raw)
    df = pd.DataFrame({"close": pd.to_numeric(df["Close"], errors="coerce")}, index=df.index)
    df = df[df["close"].notna() & (df["close"] > 0)]
    return with_change_pct(df, prev_close)[INDEX_COLUMNS]
