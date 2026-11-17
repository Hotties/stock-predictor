"""종목 마스터 수집. 상장 종목, 상장일, ETF, 상장폐지 목록을 받아 하나로 합친다.

시장 값 정리(M0 점검 결과): KOSDAQ GLOBAL은 KOSDAQ으로 합치고, KONEX 등은 시장을 비워 둔다(NaN이므로 pd.isna로 확인.
 지원 제외 판정은 services/support.py).
종류(kind): etf, spac, preferred(코드 끝자리가 0이 아님), common.
ETF·상장일·상장폐지 목록은 보조 정보라 받지 못해도 경고만 남기고 계속한다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from ..settings import CollectorSettings
from .common import CollectorError, call_with_retry, load_fdr

MASTER_COLUMNS = ["code", "name", "market", "market_raw", "marcap", "listing_date", "kind"]


@dataclass
class MasterResult:
    stocks: pd.DataFrame                      # MASTER_COLUMNS, 코드순
    etf_codes: set[str] = field(default_factory=set)
    delisted_codes: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def normalize_market(raw) -> str | None:
    """KOSPI -> KOSPI, KOSDAQ와 KOSDAQ GLOBAL -> KOSDAQ, 그 밖(KONEX 등)은 None."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip().upper()
    if text == "KOSPI":
        return "KOSPI"
    if text.startswith("KOSDAQ"):
        return "KOSDAQ"
    return None


def classify_kind(code: str, name: str, etf_codes: set[str]) -> str:
    if code in etf_codes:
        return "etf"
    if "스팩" in name or "기업인수목적" in name:
        return "spac"
    if not code.endswith("0"):
        return "preferred"
    return "common"


def _clean_code(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().upper()
    return text or None


def _listing(fdr, key: str, settings: CollectorSettings, sleep) -> pd.DataFrame:
    df = call_with_retry(
        lambda: fdr.StockListing(key),
        retries=settings.max_retries, wait_seconds=settings.retry_wait_seconds,
        sleep=sleep, what=f"StockListing({key!r})",
    )
    if sleep and settings.sleep_seconds > 0:
        sleep(settings.sleep_seconds)
    return df


def fetch_stock_master(
    *, fdr=None, settings: CollectorSettings | None = None, sleep: Callable[[float], None] = time.sleep
) -> MasterResult:
    settings = settings or CollectorSettings()
    fdr = fdr or load_fdr()
    warnings: list[str] = []

    # 필수: 상장 종목 목록
    base = _listing(fdr, "KRX", settings, sleep)
    missing = [c for c in ("Code", "Name", "Market") if c not in base.columns]
    if missing:
        raise CollectorError(f"StockListing('KRX')에 필요한 컬럼이 없습니다: {missing} (있는 컬럼: {list(base.columns)})")
    if "Marcap" not in base.columns:
        warnings.append("Marcap(시가총액) 컬럼이 없어 시드 종목 선정에 쓸 수 없습니다")

    # 보조: 상장일, ETF, 상장폐지
    listing_dates: dict[str, pd.Timestamp] = {}
    try:
        desc = _listing(fdr, "KRX-DESC", settings, sleep)
        if "Code" in desc.columns and "ListingDate" in desc.columns:
            for code, ld in zip(desc["Code"], desc["ListingDate"]):
                c = _clean_code(code)
                if c and pd.notna(ld):
                    listing_dates[c] = pd.Timestamp(ld)
        else:
            warnings.append("KRX-DESC에 Code/ListingDate 컬럼이 없어 상장일을 쓰지 않습니다")
    except CollectorError as exc:
        warnings.append(f"상장일을 받지 못했습니다: {exc}")

    etf_codes: set[str] = set()
    try:
        etf = _listing(fdr, "ETF/KR", settings, sleep)
        col = "Symbol" if "Symbol" in etf.columns else ("Code" if "Code" in etf.columns else None)
        if col:
            etf_codes = {c for c in (_clean_code(v) for v in etf[col]) if c}
        else:
            warnings.append("ETF 목록에 Symbol 컬럼이 없습니다")
    except CollectorError as exc:
        warnings.append(f"ETF 목록을 받지 못했습니다: {exc}")

    delisted_codes: set[str] = set()
    try:
        dl = _listing(fdr, "KRX-DELISTING", settings, sleep)
        col = "Symbol" if "Symbol" in dl.columns else ("Code" if "Code" in dl.columns else None)
        if col:
            delisted_codes = {c for c in (_clean_code(v) for v in dl[col]) if c}
        else:
            warnings.append("상장폐지 목록에 Symbol 컬럼이 없습니다")
    except CollectorError as exc:
        warnings.append(f"상장폐지 목록을 받지 못했습니다: {exc}")

    rows = []
    seen: set[str] = set()
    marcap_col = "Marcap" if "Marcap" in base.columns else None
    for _, rec in base.iterrows():
        code = _clean_code(rec["Code"])
        name = None if pd.isna(rec["Name"]) else str(rec["Name"]).strip()
        if not code or not name or code in seen:
            continue
        seen.add(code)
        marcap = pd.to_numeric(rec[marcap_col], errors="coerce") if marcap_col else float("nan")
        rows.append(
            {
                "code": code,
                "name": name,
                "market": normalize_market(rec["Market"]),
                "market_raw": None if pd.isna(rec["Market"]) else str(rec["Market"]),
                "marcap": float(marcap) if pd.notna(marcap) else float("nan"),
                "listing_date": listing_dates.get(code, pd.NaT),
                "kind": classify_kind(code, name, etf_codes),
            }
        )
    stocks = pd.DataFrame(rows, columns=MASTER_COLUMNS).sort_values("code").reset_index(drop=True)
    return MasterResult(stocks=stocks, etf_codes=etf_codes, delisted_codes=delisted_codes, warnings=warnings)
