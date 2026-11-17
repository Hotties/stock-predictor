"""지수와 종목 일봉의 증분 갱신 (파이프라인 B1, B3).

- 지수: 마지막 날짜부터 이어서 받는다. 한국 지수는 기준일(마지막 확정 종가일)까지만, 미국 지수는 종가가 확정된 세션까지만 넣는다.
- 종목: last_loaded_date 다음 날부터 받고, 겹치는 최근 날짜의 종가가 DB와 다르면 액면분할·배당으로 보고 전체 재적재한다.
- 받은 데이터가 DB에 들어가는 시점은 한 종목(또는 한 지수)을 마친 뒤이며, 호출하는 쪽이 종목마다 하나의 트랜잭션으로 감싸야 한다
  (전체 재적재 도중 실패하면 삭제가 함께 되돌려진다).
- DB 함수는 db 인자(기본: app.db.repo)로 받아서, 테스트에서 가짜 저장소를 끼울 수 있다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from ..collectors.indexes import fetch_index_daily
from ..collectors.prices import fetch_daily_prices, trim_to_base_date, with_change_pct
from ..settings import Settings
from .calendar import TradingCalendar, _naive_kst
from .support import SupportDecision, judge_by_prices

INDEX_SYMBOLS = ("KOSPI", "KOSDAQ", "US500")
KR_INDEX_SYMBOLS = ("KOSPI", "KOSDAQ")
PRICE_OVERLAP_DAYS = 10     # 종목: 마지막 적재일 앞쪽으로 이만큼 겹쳐 받아 수정주가 변경을 확인한다
INDEX_OVERLAP_DAYS = 7      # 지수: 등락률 계산용으로 겹쳐 받는다
DATA_DELAY_GRACE = timedelta(hours=3)   # 장 마감 확정 후 이 시간 안에는 당일 일봉이 없어도 거래정지로 단정하지 않는다


def _fmt(dates, limit: int = 5) -> str:
    """경고 문구용으로 날짜를 YYYY-MM-DD 목록으로 쓴다(많으면 앞쪽만)."""
    shown = ", ".join(d.isoformat() for d in dates[:limit])
    return shown + (f" 외 {len(dates) - limit}일" if len(dates) > limit else "")


def _db(db):
    if db is not None:
        return db
    from ..db import repo  # SQLAlchemy가 필요하므로 실제로 쓸 때만 불러온다

    return repo


# ------------------------------------------------------------------ 지수
@dataclass
class IndexRefreshReport:
    symbol: str
    rows_written: int
    last_date: date | None
    warnings: list[str] = field(default_factory=list)


def refresh_indexes(
    session, calendar: TradingCalendar, now: datetime, settings: Settings,
    *, symbols=INDEX_SYMBOLS, fdr=None, sleep=time.sleep, db=None,
) -> list[IndexRefreshReport]:
    db = _db(db)
    base = calendar.latest_base_date(now)
    reports: list[IndexRefreshReport] = []
    for sym in symbols:
        upper = base if sym in KR_INDEX_SYMBOLS else calendar.latest_us_session_confirmed(now)
        last = db.get_last_index_date(session, sym)
        if last is not None and last >= upper:
            reports.append(IndexRefreshReport(sym, 0, last))     # 이미 최신이라 받지 않는다
            continue
        if last is None:
            start = calendar.start_date_for_history(base, settings.history.load_trading_days)
        else:
            start = last - timedelta(days=INDEX_OVERLAP_DAYS)
        frame = fetch_index_daily(sym, start, fdr=fdr, settings=settings.collector, sleep=sleep)
        frame = frame[frame.index <= pd.Timestamp(upper)]               # 장중·진행 중인 세션 값은 넣지 않는다
        if last is not None:
            frame = frame[frame.index >= pd.Timestamp(last)]            # 앞쪽 겹친 행은 등락률 계산에만 썼다
        written = db.upsert_index(session, sym, frame)
        rep = IndexRefreshReport(sym, written, frame.index.max().date() if len(frame) else last)
        if sym in KR_INDEX_SYMBOLS and len(frame):
            first = frame.index.min().date()
            mismatch = calendar.compare_with_index([d.date() for d in frame.index], first, upper)
            if mismatch.only_in_calendar:
                rep.warnings.append(f"{sym}: 달력상 거래일인데 지수 행이 없음(휴장일 누락 의심) {_fmt(mismatch.only_in_calendar)}")
            if mismatch.only_in_index:
                rep.warnings.append(f"{sym}: 지수에는 있는데 달력에는 거래일이 아님 {_fmt(mismatch.only_in_index)}")
        reports.append(rep)
    return reports


# ------------------------------------------------------------------ 종목 일봉
@dataclass
class StockRefreshReport:
    code: str
    rows_written: int
    full_reload: bool
    adjusted_reload: bool                 # 수정주가 변경으로 전체 재적재했는지
    last_loaded_date: date | None
    decision: SupportDecision | None      # 일봉 기준 지원 판정. 기준일 일봉이 아직 없어 판정을 미룬 경우 None
    pending: bool = False
    warnings: list[str] = field(default_factory=list)


def changed_dates(db_closes: pd.Series, fetched_closes: pd.Series) -> list[date]:
    """겹치는 날짜 중 종가가 DB와 달라진 날. 반올림 오차(0.01원, 0.01%)는 무시한다."""
    out = []
    for d in db_closes.index.intersection(fetched_closes.index):
        a, b = float(fetched_closes[d]), float(db_closes[d])
        if abs(a - b) > max(0.01, abs(b) * 1e-4):
            out.append(d.date())
    return out


def refresh_stock(
    session, code: str, calendar: TradingCalendar, now: datetime, settings: Settings,
    *, fdr=None, sleep=time.sleep, db=None,
) -> StockRefreshReport:
    db = _db(db)
    stock = db.get_stock(session, code)
    if stock is None:
        raise KeyError(f"stocks에 없는 종목입니다: {code}")
    now_n = _naive_kst(now)
    today = now_n.date()
    base = calendar.latest_base_date(now_n)
    hist_start = calendar.start_date_for_history(base, settings.history.load_trading_days)
    last = stock.last_loaded_date or db.get_last_price_date(session, code)
    warnings: list[str] = []
    full = last is None
    adjusted = False
    fetched = None
    # 이미 기준일까지 받았으면 다시 받지 않는다. 수정주가 변경은 다음 거래일에 새 행을 받을 때 겹친 구간으로 확인된다.
    up_to_date = (not full) and last >= base

    if not full and not up_to_date:
        start = last - timedelta(days=PRICE_OVERLAP_DAYS)
        fetched = trim_to_base_date(
            fetch_daily_prices(code, start, fdr=fdr, settings=settings.collector, sleep=sleep), base
        )
        overlap = db.get_prices(session, code, start=start, end=last)
        if len(overlap) and len(fetched):
            diff = changed_dates(overlap["close"], fetched["close"])
            if diff:
                adjusted = full = True
                warnings.append(f"겹치는 날짜의 종가가 DB와 달라 전체 재적재합니다(액면분할·배당 추정): {_fmt(diff, 3)}")

    if up_to_date:
        written, new_last = 0, last
    elif full:
        if adjusted:
            db.delete_prices(session, code)
        fetched = trim_to_base_date(
            fetch_daily_prices(code, hist_start, fdr=fdr, settings=settings.collector, sleep=sleep), base
        )
        written = db.upsert_prices(session, code, with_change_pct(fetched))
        new_last = fetched.index.max().date() if len(fetched) else None
    else:
        if len(fetched) and fetched.index.min().date() > last:
            frame = with_change_pct(fetched, prev_close=db.get_close_on(session, code, last))
        else:
            frame = with_change_pct(fetched)
        new_rows = frame[frame.index > pd.Timestamp(last)]
        written = db.upsert_prices(session, code, new_rows)
        new_last = new_rows.index.max().date() if len(new_rows) else last

    if new_last is not None and new_last != stock.last_loaded_date:
        db.set_last_loaded_date(session, code, new_last)

    # 일봉 기준 지원 판정. 장 마감 직후에는 데이터 소스가 늦을 수 있어 당일 일봉이 없다고 바로 거래정지로 보지 않는다.
    grace_end = datetime.combine(today, settings.schedule.close_confirm_time) + DATA_DELAY_GRACE
    pending = base == today and now_n < grace_end and (new_last is None or new_last < base)
    decision = None
    if pending:
        warnings.append("기준일 일봉이 아직 없습니다(데이터 지연 가능). 지원 여부는 바꾸지 않습니다")
    else:
        decision = judge_by_prices(db.get_prices(session, code), base)
        db.set_stock_flags(session, code, is_active=decision.supported)
    return StockRefreshReport(code, written, full, adjusted, new_last, decision, pending, warnings)
