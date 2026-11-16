"""DB 읽기·쓰기 함수 모음(M1 범위). 다른 층은 SQL을 직접 쓰지 않고 이 파일의 함수만 부른다.

- 쓰기는 모두 UPSERT라서 같은 작업을 다시 해도 결과가 같다.
- 일봉·지수는 pandas 데이터프레임(날짜 인덱스)으로 주고받는다.
- 시각은 시간대 없는(naive) KST로 저장한다(app.clock.now_kst_naive).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select, update, delete
from sqlalchemy.orm import Session

from .models import IndexDaily, PipelineRun, PriceDaily, Stock

CHUNK = 100  # 한 번에 넣는 행 수(컬럼 8개 x 100행 = 변수 800개, 오래된 SQLite의 999개 제한 안)
PRICE_COLS = ["open", "high", "low", "close", "volume", "change_pct"]
INDEX_COLS = ["close", "change_pct"]


# ----------------------------------------------------------------- 공통 도우미
def _clean(value):
    """NaN/NaT를 None으로, numpy 숫자를 파이썬 숫자로 바꾼다."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        return value.item()
    return value


def _upsert(session: Session, model, rows: list[dict], key_cols: list[str], update_cols: list[str]) -> int:
    """같은 키가 있으면 덮어쓰고 없으면 넣는다. MariaDB와 SQLite(테스트)를 지원한다."""
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    table = model.__table__
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i : i + CHUNK]
        if dialect in ("mysql", "mariadb"):
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            stmt = mysql_insert(table).values(chunk)
            stmt = stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in update_cols})
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(table).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=key_cols, set_={c: stmt.excluded[c] for c in update_cols}
            )
        else:
            raise NotImplementedError(f"지원하지 않는 DB: {dialect}")
        session.execute(stmt)
    return len(rows)


def _to_date(value) -> date:
    return pd.Timestamp(value).date()


# ----------------------------------------------------------------- stocks
def upsert_stocks(session: Session, rows: list[dict]) -> int:
    """종목 마스터를 넣거나 갱신한다. rows: code, name, market, is_active.

    이름·시장·지원 여부만 덮어쓰고, 편입·시드·마지막 적재일은 건드리지 않는다.
    """
    cleaned = [
        {
            "code": str(r["code"]),
            "name": r["name"],
            "market": r["market"],
            "is_active": bool(r.get("is_active", True)),
        }
        for r in rows
    ]
    return _upsert(session, Stock, cleaned, ["code"], ["name", "market", "is_active"])


def get_stock(session: Session, code: str) -> Stock | None:
    return session.get(Stock, code)


def list_stocks(
    session: Session,
    *,
    is_active: bool | None = None,
    is_tracked: bool | None = None,
    is_seed: bool | None = None,
    market: str | None = None,
) -> list[Stock]:
    stmt = select(Stock).order_by(Stock.code)
    if is_active is not None:
        stmt = stmt.where(Stock.is_active == is_active)
    if is_tracked is not None:
        stmt = stmt.where(Stock.is_tracked == is_tracked)
    if is_seed is not None:
        stmt = stmt.where(Stock.is_seed == is_seed)
    if market is not None:
        stmt = stmt.where(Stock.market == market)
    return list(session.scalars(stmt))


def set_stock_flags(session: Session, code: str, **flags) -> None:
    """is_active, is_tracked, is_seed 중 넘긴 것만 바꾼다."""
    allowed = {"is_active", "is_tracked", "is_seed"}
    bad = set(flags) - allowed
    if bad:
        raise ValueError(f"바꿀 수 없는 컬럼: {sorted(bad)}")
    if flags:
        session.execute(update(Stock).where(Stock.code == code).values(**flags))


def set_last_loaded_date(session: Session, code: str, d: date | None) -> None:
    session.execute(update(Stock).where(Stock.code == code).values(last_loaded_date=d))


# ----------------------------------------------------------------- prices_daily
def upsert_prices(session: Session, code: str, df: pd.DataFrame) -> int:
    """일봉을 저장한다. df: 날짜 인덱스, 컬럼 open, high, low, close, volume, change_pct."""
    if df is None or len(df) == 0:
        return 0
    rows = []
    for ts, rec in df.iterrows():
        row = {"code": code, "trade_date": _to_date(ts)}
        for c in PRICE_COLS:
            row[c] = _clean(rec[c]) if c in df.columns else None
        if row["close"] is None:
            continue  # 종가가 없는 행은 저장하지 않는다
        rows.append(row)
    return _upsert(session, PriceDaily, rows, ["code", "trade_date"], PRICE_COLS)


def get_prices(session: Session, code: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    stmt = select(
        PriceDaily.trade_date, PriceDaily.open, PriceDaily.high, PriceDaily.low,
        PriceDaily.close, PriceDaily.volume, PriceDaily.change_pct,
    ).where(PriceDaily.code == code).order_by(PriceDaily.trade_date)
    if start is not None:
        stmt = stmt.where(PriceDaily.trade_date >= start)
    if end is not None:
        stmt = stmt.where(PriceDaily.trade_date <= end)
    return _frame(session.execute(stmt).all(), ["trade_date"] + PRICE_COLS)


def get_last_price_date(session: Session, code: str) -> date | None:
    return session.scalar(select(func.max(PriceDaily.trade_date)).where(PriceDaily.code == code))


def get_close_on(session: Session, code: str, d: date) -> float | None:
    return session.scalar(
        select(PriceDaily.close).where(PriceDaily.code == code, PriceDaily.trade_date == d)
    )


def count_prices(session: Session, code: str) -> int:
    return int(session.scalar(select(func.count()).select_from(PriceDaily).where(PriceDaily.code == code)) or 0)


def delete_prices(session: Session, code: str) -> int:
    """수정주가가 바뀐 종목의 일봉을 모두 지운다(전체 재적재 전)."""
    return session.execute(delete(PriceDaily).where(PriceDaily.code == code)).rowcount


# ----------------------------------------------------------------- index_daily
def upsert_index(session: Session, symbol: str, df: pd.DataFrame) -> int:
    """지수·환율 종가를 저장한다. df: 날짜 인덱스, 컬럼 close, change_pct."""
    if df is None or len(df) == 0:
        return 0
    rows = []
    for ts, rec in df.iterrows():
        row = {"symbol": symbol, "trade_date": _to_date(ts)}
        for c in INDEX_COLS:
            row[c] = _clean(rec[c]) if c in df.columns else None
        if row["close"] is None:
            continue
        rows.append(row)
    return _upsert(session, IndexDaily, rows, ["symbol", "trade_date"], INDEX_COLS)


def get_index(session: Session, symbol: str, start: date | None = None, end: date | None = None) -> pd.DataFrame:
    stmt = select(IndexDaily.trade_date, IndexDaily.close, IndexDaily.change_pct).where(
        IndexDaily.symbol == symbol
    ).order_by(IndexDaily.trade_date)
    if start is not None:
        stmt = stmt.where(IndexDaily.trade_date >= start)
    if end is not None:
        stmt = stmt.where(IndexDaily.trade_date <= end)
    return _frame(session.execute(stmt).all(), ["trade_date"] + INDEX_COLS)


def get_last_index_date(session: Session, symbol: str) -> date | None:
    return session.scalar(select(func.max(IndexDaily.trade_date)).where(IndexDaily.symbol == symbol))


def _frame(rows, columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=columns)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date")
    return df.astype("float64") if len(df) else df


# ----------------------------------------------------------------- pipeline_runs
def start_run(session: Session, job: str, now: datetime) -> int:
    run = PipelineRun(job=job, started_at=now, status="running")
    session.add(run)
    session.flush()
    return int(run.id)


def finish_run(
    session: Session, run_id: int, status: str, processed_rows: int | None = None,
    message: str | None = None, now: datetime | None = None,
) -> None:
    if status not in ("success", "failed"):
        raise ValueError("status는 success 또는 failed여야 합니다")
    session.execute(
        update(PipelineRun).where(PipelineRun.id == run_id).values(
            status=status, finished_at=now, processed_rows=processed_rows, message=message
        )
    )


def is_running(session: Session, job: str, now: datetime, stale_after: timedelta) -> bool:
    """같은 작업이 실행 중인지. stale_after보다 오래된 running은 비정상 종료로 보고 무시한다."""
    cutoff = now - stale_after
    n = session.scalar(
        select(func.count()).select_from(PipelineRun).where(
            PipelineRun.job == job, PipelineRun.status == "running", PipelineRun.started_at > cutoff
        )
    )
    return bool(n)


def close_stale_runs(session: Session, job: str, now: datetime, stale_after: timedelta) -> int:
    """오래된 running 기록을 failed로 닫는다."""
    cutoff = now - stale_after
    result = session.execute(
        update(PipelineRun).where(
            PipelineRun.job == job, PipelineRun.status == "running", PipelineRun.started_at <= cutoff
        ).values(status="failed", finished_at=now, message="비정상 종료로 추정(오래된 running 기록)")
    )
    return result.rowcount


def get_last_success_run(session: Session, job: str) -> datetime | None:
    """job의 마지막 성공 실행 시작 시각. 하루 한 번만 하는 작업(종목 마스터 갱신, 백업)을 판단하는 데 쓴다."""
    return session.scalar(
        select(func.max(PipelineRun.started_at)).where(PipelineRun.job == job, PipelineRun.status == "success")
    )
