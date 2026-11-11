import unittest
from datetime import date

import pandas as pd

from app.collectors.stock_master import fetch_stock_master
from app.services.support import (
    MIN_PRICE_ROWS, Reason, find_missing_codes, judge_by_master, judge_by_prices,
    judge_supported, judge_unlisted_code, static_support_table,
)
from app.settings import CollectorSettings
from tests.test_collectors import FakeFdr, master_listings

BASE = date(2026, 10, 28)   # 수요일


def prices(n, last=BASE, last_volume=1000.0):
    idx = pd.bdate_range(end=pd.Timestamp(last), periods=n)
    df = pd.DataFrame({"close": [100.0 + i for i in range(n)], "volume": [1000.0] * n}, index=idx)
    df.iloc[-1, df.columns.get_loc("volume")] = last_volume
    return df


def row(**kw):
    base = {"code": "005930", "market": "KOSPI", "market_raw": "KOSPI", "kind": "common"}
    base.update(kw)
    return base


class MasterJudgementTest(unittest.TestCase):
    def test_supported_common_stock(self):
        d = judge_by_master(row())
        self.assertTrue(d.supported)
        self.assertEqual(d.reason, Reason.SUPPORTED)
        self.assertEqual(d.message, "")

    def test_excluded_kinds(self):
        for kind, reason, word in [("preferred", Reason.PREFERRED, "우선주"), ("etf", Reason.ETF, "ETF"), ("spac", Reason.SPAC, "스팩")]:
            d = judge_by_master(row(kind=kind))
            self.assertFalse(d.supported)
            self.assertEqual(d.reason, reason)
            self.assertIn(word, d.message)
            self.assertTrue(d.message.startswith("지원하지 않는 종목입니다"))

    def test_unsupported_market_including_nan(self):
        for market in (None, float("nan"), pd.NA, "KONEX"):
            d = judge_by_master(row(market=market, market_raw="KONEX"))
            self.assertEqual(d.reason, Reason.UNSUPPORTED_MARKET, repr(market))
            self.assertEqual(d.detail, "KONEX")
        d = judge_by_master(row(market=float("nan"), market_raw=None))
        self.assertEqual(d.reason, Reason.UNSUPPORTED_MARKET)
        self.assertEqual(d.detail, "")

    def test_kosdaq_is_supported(self):
        self.assertTrue(judge_by_master(row(market="KOSDAQ")).supported)

    def test_delisted(self):
        d = judge_by_master(row(), in_listing=False, delisted_codes={"005930"})
        self.assertEqual(d.reason, Reason.DELISTED)
        self.assertIn("상장폐지", d.message)
        d2 = judge_by_master(row(), in_listing=False)           # 상장폐지 목록에도 없는 경우(코드 변경 등)
        self.assertEqual(d2.reason, Reason.DELISTED)

    def test_check_order(self):
        # ETF·스팩 > 시장 > 우선주 순서. KONEX 종목이 우선주 코드 규칙에 걸려도 시장 사유로 안내한다
        self.assertEqual(judge_by_master(row(kind="preferred", market=float("nan"))).reason, Reason.UNSUPPORTED_MARKET)
        self.assertEqual(judge_by_master(row(kind="etf", market=float("nan"))).reason, Reason.ETF)
        self.assertEqual(judge_by_master(row(kind="spac")).reason, Reason.SPAC)
        self.assertEqual(judge_by_master(row(kind="preferred")).reason, Reason.PREFERRED)


class PriceJudgementTest(unittest.TestCase):
    def test_boundary_of_new_listing(self):
        self.assertEqual(judge_by_prices(prices(MIN_PRICE_ROWS - 1), BASE).reason, Reason.NEW_LISTING)   # 120일
        d = judge_by_prices(prices(MIN_PRICE_ROWS), BASE)                                               # 121일
        self.assertTrue(d.supported)
        self.assertEqual(MIN_PRICE_ROWS, 121)

    def test_new_listing_detail(self):
        d = judge_by_prices(prices(50), BASE)
        self.assertIn("50", d.detail)
        self.assertIn("121", d.detail)

    def test_no_data(self):
        self.assertEqual(judge_by_prices(None, BASE).reason, Reason.NO_DATA)
        self.assertEqual(judge_by_prices(prices(10).iloc[0:0], BASE).reason, Reason.NO_DATA)

    def test_suspended_by_zero_volume(self):
        d = judge_by_prices(prices(200, last_volume=0.0), BASE)
        self.assertEqual(d.reason, Reason.SUSPENDED)
        self.assertIn("거래량 0", d.detail)

    def test_suspended_by_missing_row_for_base_date(self):
        d = judge_by_prices(prices(200, last=date(2026, 10, 27)), BASE)   # 마지막 일봉이 기준일보다 하루 전
        self.assertEqual(d.reason, Reason.SUSPENDED)

    def test_unknown_volume_is_not_treated_as_suspended(self):
        d = judge_by_prices(prices(200, last_volume=float("nan")), BASE)
        self.assertTrue(d.supported)

    def test_extra_rows_after_base_date_are_ignored_for_volume(self):
        p = prices(200, last=date(2026, 10, 29), last_volume=0.0)         # 기준일 다음 날의 장중 행(거래량 0)
        d = judge_by_prices(p, BASE)
        self.assertTrue(d.supported)                                      # 기준일 행의 거래량은 정상

    def test_new_listing_is_reported_before_suspension(self):
        d = judge_by_prices(prices(30, last_volume=0.0), BASE)
        self.assertEqual(d.reason, Reason.NEW_LISTING)


class CombinedJudgementTest(unittest.TestCase):
    def test_master_exclusion_wins_and_skips_prices(self):
        d = judge_supported(row(kind="preferred"), prices(200), BASE)
        self.assertEqual(d.reason, Reason.PREFERRED)

    def test_prices_none_means_master_only(self):
        self.assertTrue(judge_supported(row()).supported)

    def test_prices_require_base_date(self):
        with self.assertRaises(ValueError):
            judge_supported(row(), prices(200))

    def test_full_pass_and_price_failure(self):
        self.assertTrue(judge_supported(row(), prices(200), BASE).supported)
        self.assertEqual(judge_supported(row(), prices(20), BASE).reason, Reason.NEW_LISTING)

    def test_new_listing_recovers_automatically(self):
        self.assertFalse(judge_supported(row(), prices(120), BASE).supported)
        self.assertTrue(judge_supported(row(), prices(121), BASE).supported)    # 121거래일이 되면 지원 대상


class HelpersTest(unittest.TestCase):
    def test_find_missing_codes(self):
        self.assertEqual(find_missing_codes(["A", "B", "C"], ["B", "C", "D"]), {"A"})
        self.assertEqual(find_missing_codes([], ["B"]), set())

    def test_unlisted_code_messages(self):
        self.assertEqual(judge_unlisted_code("069500", etf_codes={"069500"}).reason, Reason.ETF)
        self.assertEqual(judge_unlisted_code("000020", delisted_codes={"000020"}).reason, Reason.DELISTED)
        self.assertIsNone(judge_unlisted_code("999999", etf_codes={"069500"}, delisted_codes={"000020"}))

    def test_static_table_on_collected_master(self):
        res = fetch_stock_master(fdr=FakeFdr(listings=master_listings()), settings=CollectorSettings(), sleep=lambda s: None)
        table = static_support_table(res.stocks, res.delisted_codes).set_index("code")
        self.assertEqual(table.loc["005930", "reason"], "supported")
        self.assertEqual(table.loc["196170", "reason"], "supported")           # KOSDAQ GLOBAL -> KOSDAQ
        self.assertEqual(table.loc["005935", "reason"], "preferred")
        self.assertEqual(table.loc["123456", "reason"], "unsupported_market")  # KONEX
        self.assertEqual(table.loc["123450", "reason"], "spac")
        self.assertEqual(table.loc["069500", "reason"], "etf")
        self.assertEqual(int(table["supported"].sum()), 3)                     # 삼성전자, 알테오젠, 엑세스바이오
        self.assertEqual(list(table.columns), ["supported", "reason", "message"])


if __name__ == "__main__":
    unittest.main()
