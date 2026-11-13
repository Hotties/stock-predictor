"""종목 마스터 동기화, 종목 편입, 시드 종목 선정 (파이프라인 A).

- sync_master   : 종목 목록을 stocks에 반영한다. 종목 목록 기준 제외(우선주, 스팩, ETF 등)는 is_active = 0으로 둔다.
                  일봉 기준 판정(신규상장, 거래정지)은 refresh_stock이 맡고, 여기서는 건드리지 않는다.
- onboard_stock : 처음 검색된 종목의 일봉을 받아 편입(is_tracked = 1)한다.
- select_seed_codes / onboard_seeds : 학습용 기준 종목(시장별 시가총액 상위)을 정하고 미리 편입한다.
"""
from __future__ import annotations

import time
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import pandas as pd

from ..collectors.common import CollectorError
from ..collectors.stock_master import MasterResult
from ..settings import Settings
from .calendar import TradingCalendar
from .refresh import StockRefreshReport, _db, refresh_stock
from .support import SupportDecision, find_missing_codes, generic_excluded, judge_by_master, static_support_table

STORED_MARKETS = ("KOSPI", "KOSDAQ")   # stocks.market에 저장하는 시장(KONEX 등은 저장하지 않는다)


# ------------------------------------------------------------------ 종목 마스터 동기화
@dataclass
class MasterSyncReport:
    total: int = 0             # 저장 대상 종목 수(KOSPI·KOSDAQ)
    inserted: int = 0
    updated: int = 0
    excluded: int = 0          # 종목 목록 기준으로 제외되어 is_active = 0인 종목
    delisted: int = 0          # DB에는 있는데 상장 목록에서 사라져 is_active = 0이 된 종목
    skipped_market: int = 0    # KONEX 등 저장하지 않은 종목


def sync_master(session, master: MasterResult, *, db=None) -> MasterSyncReport:
    db = _db(db)
    existing = {s.code for s in db.list_stocks(session)}
    table = static_support_table(master.stocks, master.delisted_codes).set_index("code")
    rep = MasterSyncReport()
    rows = []
    for rec in master.stocks.to_dict("records"):
        if rec["market"] not in STORED_MARKETS:
            rep.skipped_market += 1
            continue
        static_ok = bool(table.loc[rec["code"], "supported"])
        # 종목 목록 기준으로 지원 가능하면 True로 둔다. 일봉 기준 판정은 갱신(refresh_stock)이 바로 이어서 다시 한다.
        rows.append({"code": rec["code"], "name": rec["name"], "market": rec["market"], "is_active": static_ok})
        rep.inserted += rec["code"] not in existing
        rep.updated += rec["code"] in existing
        rep.excluded += not static_ok
    db.upsert_stocks(session, rows)
    rep.total = len(rows)
    for code in sorted(find_missing_codes(existing, master.stocks["code"])):
        db.set_stock_flags(session, code, is_active=False)
        rep.delisted += 1
    return rep


# ------------------------------------------------------------------ 종목 편입
@dataclass
class OnboardResult:
    code: str
    status: str                              # onboarded | already_tracked | excluded | not_found
    decision: SupportDecision | None = None  # 지원 여부와 화면용 문구
    refresh: StockRefreshReport | None = None


def onboard_stock(
    session, code: str, calendar: TradingCalendar, now: datetime, settings: Settings,
    *, master_row=None, fdr=None, sleep=time.sleep, db=None,
) -> OnboardResult:
    """종목을 편입한다. 일봉을 받은 종목은 일봉 기준 판정이 지원 대상이 아니어도 편입(is_tracked = 1)해 둔다.

    그래야 매일 갱신에서 다시 판정해, 신규상장 종목이 121거래일이 되면 자동으로 지원 대상이 된다.
    master_row(종목 목록의 한 행)를 주면 제외 사유를 구체적으로 알려 준다.
    """
    db = _db(db)
    stock = db.get_stock(session, code)
    if stock is None:
        return OnboardResult(code, "not_found")
    if stock.is_tracked:
        rep = refresh_stock(session, code, calendar, now, settings, fdr=fdr, sleep=sleep, db=db)
        return OnboardResult(code, "already_tracked", rep.decision, rep)
    if not stock.is_active:
        decision = judge_by_master(master_row) if master_row is not None else generic_excluded()
        return OnboardResult(code, "excluded", decision)
    rep = refresh_stock(session, code, calendar, now, settings, fdr=fdr, sleep=sleep, db=db)
    db.set_stock_flags(session, code, is_tracked=True)
    return OnboardResult(code, "onboarded", rep.decision, rep)


# ------------------------------------------------------------------ 시드 종목
@dataclass
class SeedSelection:
    codes: list[str]
    shortfall: dict[str, int] = field(default_factory=dict)   # 목표보다 모자란 시장과 부족한 수


def select_seed_codes(master: pd.DataFrame, kospi_count: int, kosdaq_count: int) -> SeedSelection:
    """시장별 시가총액 상위 종목(보통주만)을 고른다. 시가총액이 비어 있는 종목은 제외한다."""
    common = master[(master["kind"] == "common") & master["marcap"].notna()]
    codes: list[str] = []
    shortfall: dict[str, int] = {}
    for market, n in (("KOSPI", kospi_count), ("KOSDAQ", kosdaq_count)):
        top = common[common["market"] == market].sort_values(["marcap", "code"], ascending=[False, True]).head(n)
        codes.extend(top["code"].tolist())
        if len(top) < n:
            shortfall[market] = n - len(top)
    return SeedSelection(codes, shortfall)


@dataclass
class SeedReport:
    selected: int = 0
    onboarded: int = 0
    excluded: list[str] = field(default_factory=list)        # 편입은 했지만 일봉 기준으로 지원 대상이 아닌 종목
    failed: list[tuple[str, str]] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)       # sync_master 전이라 stocks에 없는 종목
    shortfall: dict[str, int] = field(default_factory=dict)


def onboard_seeds(
    scope: Callable[[], AbstractContextManager],
    master: MasterResult,
    calendar: TradingCalendar,
    now: datetime,
    settings: Settings,
    *,
    fdr=None,
    sleep=time.sleep,
    db=None,
    progress: Callable[[int, int, str], None] | None = None,
) -> SeedReport:
    """시드 종목을 정하고 하나씩 편입한다. scope()는 세션을 주는 컨텍스트 매니저(종목마다 커밋)이다.

    한 종목이 실패해도 다음 종목으로 넘어가고, 실패 목록에 남긴다. 이미 편입한 종목은 다시 해도 안전하다(증분 갱신).
    """
    db = _db(db)
    sel = select_seed_codes(master.stocks, settings.seed.kospi_count, settings.seed.kosdaq_count)
    rep = SeedReport(selected=len(sel.codes), shortfall=sel.shortfall)
    rows = {r["code"]: r for r in master.stocks.to_dict("records")}
    for i, code in enumerate(sel.codes, start=1):
        if progress:
            progress(i, len(sel.codes), code)
        with scope() as session:                                    # 시드 표시는 별도 트랜잭션: 일봉을 받다 실패해도 남는다
            if db.get_stock(session, code) is None:
                rep.not_found.append(code)
                continue
            db.set_stock_flags(session, code, is_seed=True)
        try:
            with scope() as session:
                result = onboard_stock(session, code, calendar, now, settings, master_row=rows.get(code), fdr=fdr, sleep=sleep, db=db)
        except CollectorError as exc:
            rep.failed.append((code, str(exc)))
            continue
        if result.status in ("onboarded", "already_tracked"):
            rep.onboarded += 1
            if result.decision is not None and not result.decision.supported:
                rep.excluded.append(code)
    return rep
