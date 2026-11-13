"""서비스 테스트용 공통 도구.

같은 시험을 두 가지 저장소로 돌린다.
  - FakeBackend  : 메모리 가짜 저장소(항상 실행, SQLAlchemy 불필요)
  - SqliteBackend: 실제 DB 층(app.db.repo)을 메모리 SQLite로 사용(SQLAlchemy가 있을 때만)
"""
import contextlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from app.collectors.prices import PRICE_COLUMNS
from tests.test_collectors import FakeFdr

PRICE_DB_COLS = PRICE_COLUMNS + ["change_pct"]
INDEX_DB_COLS = ["close", "change_pct"]


class RangeFakeFdr(FakeFdr):
    """DataReader가 start/end로 행을 걸러 주는 가짜(진짜 FinanceDataReader처럼)."""

    def DataReader(self, symbol, start=None, end=None):
        df = super().DataReader(symbol, start, end)
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        return df


class FailOnNthFdr(RangeFakeFdr):
    """n번째 DataReader 호출부터 계속 실패한다."""

    def __init__(self, *a, fail_from=2, **kw):
        super().__init__(*a, **kw)
        self.fail_from = fail_from
        self.n = 0

    def DataReader(self, symbol, start=None, end=None):
        self.n += 1
        if self.n >= self.fail_from:
            self.calls.append(("data", symbol, start, end))
            raise RuntimeError("boom")
        return super().DataReader(symbol, start, end)


# ------------------------------------------------------------------ 데이터 만들기
def kr_days(cal, start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if cal.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def price_df(days, first_close=1000.0, volume=1000.0, last_volume=None) -> pd.DataFrame:
    """FinanceDataReader 모양(Open·High·Low·Close·Volume, Date 인덱스)의 일봉. 종가는 first_close + 순번."""
    closes = [first_close + i for i in range(len(days))]
    vols = [volume] * len(days)
    if last_volume is not None and vols:
        vols[-1] = last_volume
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days], name="Date")
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": vols}, index=idx)


def index_df(days, first_close=2500.0) -> pd.DataFrame:
    closes = [first_close + i for i in range(len(days))]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days], name="Date")
    return pd.DataFrame({"Close": closes}, index=idx)


# ------------------------------------------------------------------ 가짜 저장소
@dataclass
class FakeStock:
    code: str
    name: str
    market: str
    is_active: bool = True
    is_tracked: bool = False
    is_seed: bool = False
    last_loaded_date: date | None = None


def _clean(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v.item() if hasattr(v, "item") else v


class FakeRepo:
    """app.db.repo와 같은 이름·같은 동작을 메모리로 흉내 낸 저장소(M1에서 서비스가 쓰는 함수만)."""

    def __init__(self):
        self.stocks: dict[str, FakeStock] = {}
        self._prices: dict[str, dict[date, dict]] = {}
        self._index: dict[str, dict[date, dict]] = {}

    # stocks
    def upsert_stocks(self, session, rows):
        for r in rows:
            s = self.stocks.get(r["code"])
            if s is None:
                self.stocks[r["code"]] = FakeStock(r["code"], r["name"], r["market"], bool(r.get("is_active", True)))
            else:
                s.name, s.market, s.is_active = r["name"], r["market"], bool(r.get("is_active", True))
        return len(rows)

    def get_stock(self, session, code):
        return self.stocks.get(code)

    def list_stocks(self, session, *, is_active=None, is_tracked=None, is_seed=None, market=None):
        out = []
        for code in sorted(self.stocks):
            s = self.stocks[code]
            if (is_active is None or s.is_active == is_active) and (is_tracked is None or s.is_tracked == is_tracked) \
                    and (is_seed is None or s.is_seed == is_seed) and (market is None or s.market == market):
                out.append(s)
        return out

    def set_stock_flags(self, session, code, **flags):
        bad = set(flags) - {"is_active", "is_tracked", "is_seed"}
        if bad:
            raise ValueError(sorted(bad))
        for k, v in flags.items():
            setattr(self.stocks[code], k, bool(v))

    def set_last_loaded_date(self, session, code, d):
        self.stocks[code].last_loaded_date = d

    # prices
    def upsert_prices(self, session, code, df):
        if df is None or len(df) == 0:
            return 0
        store = self._prices.setdefault(code, {})
        n = 0
        for ts, rec in df.iterrows():
            row = {c: (_clean(rec[c]) if c in df.columns else None) for c in PRICE_DB_COLS}
            if row["close"] is None:
                continue
            store[pd.Timestamp(ts).date()] = row
            n += 1
        return n

    def get_prices(self, session, code, start=None, end=None):
        store = self._prices.get(code, {})
        days = sorted(d for d in store if (start is None or d >= start) and (end is None or d <= end))
        df = pd.DataFrame([store[d] for d in days], columns=PRICE_DB_COLS)
        df.index = pd.DatetimeIndex([pd.Timestamp(d) for d in days], name="trade_date")
        return df.astype("float64")

    def get_last_price_date(self, session, code):
        store = self._prices.get(code)
        return max(store) if store else None

    def get_close_on(self, session, code, d):
        row = self._prices.get(code, {}).get(d)
        return None if row is None else row["close"]

    def count_prices(self, session, code):
        return len(self._prices.get(code, {}))

    def delete_prices(self, session, code):
        return len(self._prices.pop(code, {}))

    # index
    def upsert_index(self, session, symbol, df):
        if df is None or len(df) == 0:
            return 0
        store = self._index.setdefault(symbol, {})
        n = 0
        for ts, rec in df.iterrows():
            row = {c: (_clean(rec[c]) if c in df.columns else None) for c in INDEX_DB_COLS}
            if row["close"] is None:
                continue
            store[pd.Timestamp(ts).date()] = row
            n += 1
        return n

    def get_index(self, session, symbol, start=None, end=None):
        store = self._index.get(symbol, {})
        days = sorted(d for d in store if (start is None or d >= start) and (end is None or d <= end))
        df = pd.DataFrame([store[d] for d in days], columns=INDEX_DB_COLS)
        df.index = pd.DatetimeIndex([pd.Timestamp(d) for d in days], name="trade_date")
        return df.astype("float64")

    def get_last_index_date(self, session, symbol):
        store = self._index.get(symbol)
        return max(store) if store else None


# ------------------------------------------------------------------ 저장소 묶음
class FakeBackend:
    name = "fake"
    supports_rollback = False

    def __init__(self):
        self.db = FakeRepo()

    def scope(self):
        return contextlib.nullcontext(None)

    def close(self):
        pass


class SqliteBackend:
    name = "sqlite"
    supports_rollback = True

    def __init__(self):
        from app.db import repo
        from app.db.connection import create_all, make_engine, make_session_factory, session_scope

        self._session_scope = session_scope
        self.engine = make_engine("sqlite://")
        create_all(self.engine)
        self.factory = make_session_factory(self.engine)
        self.db = repo

    def scope(self):
        return self._session_scope(self.factory)

    def close(self):
        self.engine.dispose()


try:
    import sqlalchemy  # noqa: F401
    HAVE_SQLALCHEMY = True
except ImportError:
    HAVE_SQLALCHEMY = False
