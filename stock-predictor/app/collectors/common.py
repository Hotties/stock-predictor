"""수집기 공통 도우미: 재시도, 호출 간격, 날짜 인덱스 정리.

외부(FinanceDataReader)와의 접속은 collectors 폴더에서만 한다. 테스트에서는 fdr 인자로 가짜를 넣는다.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Callable

import pandas as pd

from ..settings import CollectorSettings


class CollectorError(RuntimeError):
    """외부 데이터를 받지 못했거나 형식이 예상과 다를 때."""



def load_fdr():
    """FinanceDataReader를 불러온다(설치되어 있지 않으면 안내 메시지와 함께 오류)."""
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:
        raise CollectorError("FinanceDataReader가 설치되어 있지 않습니다: pip install finance-datareader") from exc
    return fdr


def call_with_retry(
    fn: Callable, *, retries: int, wait_seconds: float, sleep: Callable[[float], None] = time.sleep, what: str = "외부 호출"
):
    """fn()을 최대 retries번 시도한다. 모두 실패하면 CollectorError."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001  (외부 라이브러리의 오류 종류가 다양함)
            last = exc
            if attempt < retries:
                sleep(wait_seconds)
    raise CollectorError(f"{what} 실패({retries}회 시도): {last!r}") from last


def to_iso(d: date | datetime | str | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)


def download(fdr, symbol: str, start, end, settings: CollectorSettings, sleep: Callable[[float], None]) -> pd.DataFrame:
    """DataReader를 재시도와 호출 간격을 지켜 부른다."""
    s, e = to_iso(start), to_iso(end)

    def call():
        return fdr.DataReader(symbol, s, e) if e else fdr.DataReader(symbol, s)

    df = call_with_retry(
        call, retries=settings.max_retries, wait_seconds=settings.retry_wait_seconds,
        sleep=sleep, what=f"{symbol} 조회",
    )
    if settings.sleep_seconds > 0:
        sleep(settings.sleep_seconds)  # 다음 호출과의 간격
    return df


def normalize_date_index(df: pd.DataFrame) -> pd.DataFrame:
    """날짜 인덱스를 시간대 없는 날짜(00:00)로 맞추고, 정렬하고, 같은 날짜가 겹치면 마지막 행만 남긴다."""
    out = df.copy()
    idx = pd.to_datetime(out.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    out.index.name = "trade_date"
    out = out.sort_index()
    return out[~out.index.duplicated(keep="last")]
