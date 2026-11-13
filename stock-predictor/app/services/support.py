"""지원 대상 판정. 신규상장, 거래정지, 상장폐지, 우선주, ETF, 스팩, KONEX는 별도 처리를 만들지 않고 지원 대상에서 뺀다(설계 문서 11절).

판정은 두 단계로 나뉜다.
  1) judge_by_master  : 종목 목록의 정보만으로 판정(종류, 시장, 상장폐지). 일봉이 없어도 할 수 있다.
  2) judge_by_prices  : 일봉으로 판정(신규상장, 거래정지, 데이터 없음). 일봉을 받은 뒤에 한다.
judge_supported가 둘을 순서대로 합친다. 제외된 종목은 stocks.is_active = 0으로 저장하고, 갱신할 때마다 다시 판정하므로
신규상장은 121거래일이 되면 자동으로 지원 대상이 된다.

stocks.market에는 KOSPI와 KOSDAQ만 저장하므로 KONEX 종목은 stocks 테이블에 넣지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Iterable, Mapping

import pandas as pd

# 120일 이평선 + 수익률 계산에 필요한 최소 일봉 수(설계 문서 11절: 121거래일 미만은 신규상장으로 제외)
MIN_PRICE_ROWS = 121


class Reason(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED_MARKET = "unsupported_market"   # KONEX 등
    PREFERRED = "preferred"                     # 우선주
    ETF = "etf"
    SPAC = "spac"
    DELISTED = "delisted"                       # 상장폐지·종목코드 변경
    NEW_LISTING = "new_listing"                 # 일봉 121거래일 미만
    SUSPENDED = "suspended"                     # 최근 거래일에 거래 없음
    NO_DATA = "no_data"                         # 일봉을 받지 못함
    EXCLUDED = "excluded"                       # 제외 상태이지만 사유는 모를 때(종목 목록 정보가 없는 경우)


@dataclass(frozen=True)
class SupportDecision:
    supported: bool
    reason: Reason
    message: str          # 화면에 보여 줄 문구
    detail: str = ""      # 로그용 부가 정보


def _ok(detail: str = "") -> SupportDecision:
    return SupportDecision(True, Reason.SUPPORTED, "", detail)


def _no(reason: Reason, text: str, detail: str = "") -> SupportDecision:
    return SupportDecision(False, reason, f"지원하지 않는 종목입니다({text})", detail)


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT or (
        not isinstance(value, str) and pd.isna(value)
    )


# ------------------------------------------------------------------ 1단계: 종목 목록 기준
def judge_by_master(
    row: Mapping, *, in_listing: bool = True, delisted_codes: Iterable[str] = ()
) -> SupportDecision:
    """종목 목록의 한 행(code, market, market_raw, kind)으로 판정한다.

    in_listing: 지금의 상장 목록에 이 종목이 있는지. 없고 상장폐지 목록에 있으면 상장폐지로 본다.
    """
    code = str(row.get("code", ""))
    if not in_listing:
        if code in set(delisted_codes):
            return _no(Reason.DELISTED, "상장폐지", code)
        return _no(Reason.DELISTED, "상장폐지되었거나 종목코드가 바뀜", code)
    kind = row.get("kind")
    if kind == "etf":
        return _no(Reason.ETF, "ETF")
    if kind == "spac":
        return _no(Reason.SPAC, "스팩")
    # 시장을 우선주보다 먼저 본다: KONEX 종목의 코드가 0으로 끝나지 않아도 "우선주"로 잘못 안내하지 않기 위해서다
    market = row.get("market")
    if _is_blank(market) or market not in ("KOSPI", "KOSDAQ"):
        raw = row.get("market_raw")
        return _no(Reason.UNSUPPORTED_MARKET, "KOSPI·KOSDAQ 종목만 지원", "" if _is_blank(raw) else str(raw))
    if kind == "preferred":
        return _no(Reason.PREFERRED, "우선주")
    return _ok()


def judge_unlisted_code(code: str, *, etf_codes: Iterable[str] = (), delisted_codes: Iterable[str] = ()) -> SupportDecision | None:
    """종목 목록에 없는 코드에 대한 안내. ETF나 상장폐지 코드면 이유를 알려 주고, 모르는 코드면 None."""
    if code in set(etf_codes):
        return _no(Reason.ETF, "ETF")
    if code in set(delisted_codes):
        return _no(Reason.DELISTED, "상장폐지", code)
    return None


def find_missing_codes(db_codes: Iterable[str], listing_codes: Iterable[str]) -> set[str]:
    """DB에는 있는데 지금의 상장 목록에서 사라진 코드(상장폐지·코드 변경 후보)."""
    return set(db_codes) - set(listing_codes)


# ------------------------------------------------------------------ 2단계: 일봉 기준
def judge_by_prices(prices: pd.DataFrame | None, base_date: date) -> SupportDecision:
    """일봉(날짜 인덱스, close·volume 컬럼)과 기준일(마지막 확정 종가일)로 판정한다.

    - 일봉이 하나도 없으면 NO_DATA
    - 121거래일 미만이면 NEW_LISTING
    - 기준일의 행이 없거나 기준일의 거래량이 0이면 SUSPENDED (거래량 값이 비어 있으면 알 수 없으므로 정지로 보지 않는다)
    """
    if prices is None or len(prices) == 0:
        return _no(Reason.NO_DATA, "일봉 데이터를 받지 못함")
    n = len(prices)
    if n < MIN_PRICE_ROWS:
        return _no(Reason.NEW_LISTING, "신규상장 등으로 데이터 부족", f"일봉 {n}일 / 필요 {MIN_PRICE_ROWS}일")
    last_day = pd.Timestamp(prices.index.max()).date()
    if last_day < base_date:
        return _no(Reason.SUSPENDED, "거래정지로 보임", f"마지막 일봉 {last_day} < 기준일 {base_date}")
    row = prices.loc[prices.index == pd.Timestamp(base_date)]
    if len(row) and "volume" in prices.columns:
        vol = row["volume"].iloc[-1]
        if pd.notna(vol) and float(vol) == 0.0:
            return _no(Reason.SUSPENDED, "거래정지로 보임", f"{base_date} 거래량 0")
    return _ok(f"일봉 {n}일")


# ------------------------------------------------------------------ 합치기
def judge_supported(
    row: Mapping,
    prices: pd.DataFrame | None = None,
    base_date: date | None = None,
    *,
    in_listing: bool = True,
    delisted_codes: Iterable[str] = (),
) -> SupportDecision:
    """종목 목록 기준 -> 일봉 기준 순서로 판정한다. prices가 None이면 일봉 기준 판정은 건너뛴다(아직 받기 전)."""
    first = judge_by_master(row, in_listing=in_listing, delisted_codes=delisted_codes)
    if not first.supported or prices is None:
        return first
    if base_date is None:
        raise ValueError("일봉으로 판정하려면 기준일(base_date)이 필요합니다")
    return judge_by_prices(prices, base_date)


def static_support_table(master: pd.DataFrame, delisted_codes: Iterable[str] = ()) -> pd.DataFrame:
    """종목 목록 전체를 종목 목록 기준으로 판정한다. 갱신 때 stocks.is_active를 한 번에 정하는 데 쓴다.

    반환 컬럼: code, supported, reason, message
    """
    delisted = set(delisted_codes)
    rows = []
    for rec in master.to_dict("records"):
        d = judge_by_master(rec, delisted_codes=delisted)
        rows.append({"code": rec["code"], "supported": d.supported, "reason": d.reason.value, "message": d.message})
    return pd.DataFrame(rows, columns=["code", "supported", "reason", "message"])


def generic_excluded() -> SupportDecision:
    """제외된 종목인데 종목 목록 정보가 없어 사유를 알 수 없을 때의 안내."""
    return SupportDecision(False, Reason.EXCLUDED, "지원하지 않는 종목입니다", "사유 미상")
