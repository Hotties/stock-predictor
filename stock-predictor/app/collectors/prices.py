"""종목 일봉 수집. 수정주가를 받는다(M0에서 기본 소스가 수정주가임을 확인)."""
from __future__ import annotations

import time
from datetime import date
from typing import Callable

import pandas as pd

from ..settings import CollectorSettings
from .common import download, load_fdr, normalize_date_index

PRICE_COLUMNS = ["open", "high", "low", "close", "volume"]
_RENAME = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}


def _empty() -> pd.DataFrame:
    df = pd.DataFrame(columns=PRICE_COLUMNS, dtype="float64")
    df.index = pd.DatetimeIndex([], name="trade_date")
    return df


def fetch_daily_prices(
    code: str,
    start,
    end=None,
    *,
    fdr=None,
    settings: CollectorSettings | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> pd.DataFrame:
    """code의 일봉을 받는다. 반환: 날짜 인덱스, 컬럼 open, high, low, close, volume.

    - 종가가 없거나 0 이하인 행은 버린다. 거래량 0인 행(거래정지 의심)은 남긴다.
    - 오늘 장중 값이 섞이는 것은 호출한 쪽에서 trim_to_base_date로 잘라 낸다.
    - 한 번 부를 때마다 settings.sleep_seconds만큼 쉰다(호출 간격).
    """
    settings = settings or CollectorSettings()
    fdr = fdr or load_fdr()
    raw = download(fdr, code, start, end, settings, sleep)
    if raw is None or len(raw) == 0:
        return _empty()
    df = normalize_date_index(raw)
    df = df.rename(columns=_RENAME)
    for col in PRICE_COLUMNS:
        if col not in df.columns:
            df[col] = float("nan")
    df = df[PRICE_COLUMNS].apply(pd.to_numeric, errors="coerce")
    df = df[df["close"].notna() & (df["close"] > 0)]
    return df


def trim_to_base_date(df: pd.DataFrame, base_date: date) -> pd.DataFrame:
    """기준일(마지막 확정 종가일) 이후 행(오늘 장중 값 등)을 잘라 낸다."""
    return df[df.index <= pd.Timestamp(base_date)]


def with_change_pct(df: pd.DataFrame, prev_close: float | None = None) -> pd.DataFrame:
    """전일 대비 등락률(%) 컬럼을 붙인다. 첫 행은 prev_close(DB에 있는 직전 종가)로 계산하고, 없으면 비워 둔다."""
    out = df.copy()
    prev = out["close"].shift(1)
    if prev_close is not None and len(out):
        prev.iloc[0] = prev_close
    pct = (out["close"] / prev - 1.0) * 100.0
    pct = pct.where(prev > 0)  # 직전 종가가 없거나 0 이하면 계산하지 않는다
    out["change_pct"] = pct.round(4)
    return out
