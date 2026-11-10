"""수집기 테스트. 가짜 FinanceDataReader로 로직을 확인한다(네트워크 없음).

실제 접속 테스트는 기본으로 건너뛴다. 본인 PC에서 확인하려면:
  PowerShell:  $env:STOCK_NETWORK_TESTS = "1"; python -m unittest tests.test_collectors -v
  cmd:         set STOCK_NETWORK_TESTS=1 && python -m unittest tests.test_collectors -v
"""
import os
import unittest
from datetime import date

import pandas as pd

from app.collectors.common import CollectorError, call_with_retry
from app.collectors.indexes import fetch_index_daily
from app.collectors.prices import PRICE_COLUMNS, fetch_daily_prices, trim_to_base_date, with_change_pct
from app.collectors.stock_master import classify_kind, fetch_stock_master, normalize_market
from app.settings import CollectorSettings

try:
    import FinanceDataReader  # noqa: F401
    HAVE_FDR = True
except ImportError:
    HAVE_FDR = False

SETTINGS = CollectorSettings(sleep_seconds=0.3, max_retries=3, retry_wait_seconds=1.0)


class FakeFdr:
    """StockListing과 DataReader를 흉내 낸다. fail_times[키] = 앞으로 실패할 횟수."""

    def __init__(self, listings=None, prices=None, fail_times=None):
        self.listings = listings or {}
        self.prices = prices or {}
        self.fail_times = dict(fail_times or {})
        self.calls = []

    def _maybe_fail(self, key):
        if self.fail_times.get(key, 0) > 0:
            self.fail_times[key] -= 1
            raise RuntimeError(f"boom {key}")

    def StockListing(self, key):
        self.calls.append(("listing", key))
        self._maybe_fail(key)
        return self.listings[key]

    def DataReader(self, symbol, start=None, end=None):
        self.calls.append(("data", symbol, start, end))
        self._maybe_fail(symbol)
        return self.prices[symbol]


def raw_prices(rows, tz=None):
    """rows: (날짜, 시가, 고가, 저가, 종가, 거래량) 목록으로 FinanceDataReader 모양의 프레임을 만든다."""
    idx = pd.DatetimeIndex([r[0] for r in rows], name="Date")
    if tz:
        idx = idx.tz_localize(tz)
    return pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows], "Low": [r[3] for r in rows],
         "Close": [r[4] for r in rows], "Volume": [r[5] for r in rows], "Change": [999.0] * len(rows)},
        index=idx,
    )


class RetryTest(unittest.TestCase):
    def test_succeeds_after_failures(self):
        state, sleeps = {"n": 0}, []

        def flaky():
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("temporary")
            return "ok"

        self.assertEqual(call_with_retry(flaky, retries=3, wait_seconds=1.5, sleep=sleeps.append), "ok")
        self.assertEqual(sleeps, [1.5, 1.5])   # 실패한 뒤에만 쉰다

    def test_gives_up_after_retries(self):
        sleeps = []
        with self.assertRaises(CollectorError) as cm:
            call_with_retry(lambda: 1 / 0, retries=3, wait_seconds=1.0, sleep=sleeps.append, what="시험")
        self.assertIn("3회", str(cm.exception))
        self.assertEqual(len(sleeps), 2)       # 마지막 실패 뒤에는 쉬지 않는다
        self.assertIsInstance(cm.exception.__cause__, ZeroDivisionError)


class PricesTest(unittest.TestCase):
    def test_normalization(self):
        raw = raw_prices([
            ("2026-10-27", 10, 12, 9, 11, 500),
            ("2026-10-23", 8, 9, 7, 0, 100),            # 종가 0 -> 버림
            ("2026-10-26", 9, 10, 8, 10, 0),            # 거래량 0 -> 남김(거래정지 의심 판단에 쓴다)
            ("2026-10-26", 9, 10, 8, 10.5, 300),        # 같은 날짜가 겹치면 마지막 행
            ("2026-10-22", 7, 8, 6, float("nan"), 50),  # 종가 없음 -> 버림
        ])
        fdr, sleeps = FakeFdr(prices={"005930": raw}), []
        df = fetch_daily_prices("005930", date(2026, 10, 1), fdr=fdr, settings=SETTINGS, sleep=sleeps.append)
        self.assertEqual(list(df.columns), PRICE_COLUMNS)
        self.assertEqual(df.index.name, "trade_date")
        self.assertEqual([d.date() for d in df.index], [date(2026, 10, 26), date(2026, 10, 27)])
        self.assertEqual(df["close"].tolist(), [10.5, 11.0])
        self.assertTrue(df.index.is_monotonic_increasing)
        self.assertEqual(sleeps, [0.3])             # 호출 간격

    def test_zero_volume_row_is_kept(self):
        raw = raw_prices([("2026-10-26", 9, 10, 8, 10, 0), ("2026-10-27", 9, 10, 8, 10, 100)])
        df = fetch_daily_prices("005930", "2026-10-01", fdr=FakeFdr(prices={"005930": raw}), settings=SETTINGS, sleep=lambda s: None)
        self.assertEqual(df["volume"].tolist(), [0.0, 100.0])

    def test_timezone_index_is_made_naive(self):
        raw = raw_prices([("2026-10-26", 9, 10, 8, 10, 1), ("2026-10-27", 9, 10, 8, 11, 1)], tz="Asia/Seoul")
        df = fetch_daily_prices("005930", "2026-10-01", fdr=FakeFdr(prices={"005930": raw}), settings=SETTINGS, sleep=lambda s: None)
        self.assertIsNone(df.index.tz)
        self.assertEqual(df.index[0], pd.Timestamp("2026-10-26"))

    def test_missing_columns_become_nan_and_empty_result(self):
        raw = raw_prices([("2026-10-26", 9, 10, 8, 10, 1)])[["Close"]]
        df = fetch_daily_prices("005930", "2026-10-01", fdr=FakeFdr(prices={"005930": raw}), settings=SETTINGS, sleep=lambda s: None)
        self.assertTrue(df[["open", "high", "low", "volume"]].isna().all().all())
        empty = fetch_daily_prices("005930", "2026-10-01", fdr=FakeFdr(prices={"005930": raw.iloc[0:0]}), settings=SETTINGS, sleep=lambda s: None)
        self.assertEqual(list(empty.columns), PRICE_COLUMNS)
        self.assertEqual(len(empty), 0)

    def test_dates_are_passed_as_iso_strings(self):
        fdr = FakeFdr(prices={"005930": raw_prices([("2026-10-26", 9, 10, 8, 10, 1)])})
        fetch_daily_prices("005930", date(2023, 4, 1), date(2026, 10, 27), fdr=fdr, settings=SETTINGS, sleep=lambda s: None)
        fetch_daily_prices("005930", date(2023, 4, 1), fdr=fdr, settings=SETTINGS, sleep=lambda s: None)
        self.assertEqual(fdr.calls[0], ("data", "005930", "2023-04-01", "2026-10-27"))
        self.assertEqual(fdr.calls[1], ("data", "005930", "2023-04-01", None))

    def test_retry_then_success_and_final_failure(self):
        raw = raw_prices([("2026-10-26", 9, 10, 8, 10, 1)])
        fdr, sleeps = FakeFdr(prices={"005930": raw}, fail_times={"005930": 2}), []
        df = fetch_daily_prices("005930", "2026-10-01", fdr=fdr, settings=SETTINGS, sleep=sleeps.append)
        self.assertEqual(len(df), 1)
        self.assertEqual(sleeps, [1.0, 1.0, 0.3])     # 실패 2번 뒤 재시도 대기 + 호출 간격
        fdr = FakeFdr(prices={"005930": raw}, fail_times={"005930": 99})
        with self.assertRaises(CollectorError):
            fetch_daily_prices("005930", "2026-10-01", fdr=fdr, settings=SETTINGS, sleep=lambda s: None)
        self.assertEqual(len([c for c in fdr.calls if c[0] == "data"]), 3)   # 최대 3회

    def test_trim_to_base_date(self):
        raw = raw_prices([("2026-10-26", 9, 10, 8, 10, 1), ("2026-10-27", 9, 10, 8, 11, 1), ("2026-10-28", 9, 10, 8, 12, 1)])
        df = fetch_daily_prices("005930", "2026-10-01", fdr=FakeFdr(prices={"005930": raw}), settings=SETTINGS, sleep=lambda s: None)
        out = trim_to_base_date(df, date(2026, 10, 27))
        self.assertEqual([d.date() for d in out.index], [date(2026, 10, 26), date(2026, 10, 27)])

    def test_with_change_pct(self):
        df = pd.DataFrame({"close": [100.0, 110.0, 99.0]}, index=pd.bdate_range("2026-10-26", periods=3))
        out = with_change_pct(df)
        self.assertTrue(pd.isna(out["change_pct"].iloc[0]))
        self.assertEqual(out["change_pct"].iloc[1:].tolist(), [10.0, -10.0])
        self.assertEqual(with_change_pct(df, prev_close=50.0)["change_pct"].iloc[0], 100.0)
        self.assertTrue(pd.isna(with_change_pct(df, prev_close=0.0)["change_pct"].iloc[0]))
        thirds = with_change_pct(pd.DataFrame({"close": [3.0, 4.0]}, index=pd.bdate_range("2026-10-26", periods=2)))
        self.assertEqual(thirds["change_pct"].iloc[1], 33.3333)   # DB 자릿수(소수 4자리)로 반올림
        self.assertNotIn("change_pct", df.columns)                # 원본은 바뀌지 않는다


class IndexesTest(unittest.TestCase):
    def setUp(self):
        self.raw = raw_prices([("2026-10-26", 0, 0, 0, 2500.0, 0), ("2026-10-27", 0, 0, 0, 2525.0, 0), ("2026-10-28", 0, 0, 0, 2500.0, 0)])

    def test_symbol_mapping(self):
        fdr = FakeFdr(prices={"KS11": self.raw, "KQ11": self.raw, "US500": self.raw, "IXIC": self.raw, "USD/KRW": self.raw})
        for ours, theirs in [("KOSPI", "KS11"), ("KOSDAQ", "KQ11"), ("US500", "US500"), ("IXIC", "IXIC"), ("USDKRW", "USD/KRW")]:
            fetch_index_daily(ours, "2026-10-01", fdr=fdr, settings=SETTINGS, sleep=lambda s: None)
            self.assertEqual(fdr.calls[-1][1], theirs)
        with self.assertRaises(ValueError):
            fetch_index_daily("NASDAQ", "2026-10-01", fdr=fdr, settings=SETTINGS, sleep=lambda s: None)

    def test_change_pct_is_computed_from_close(self):
        fdr = FakeFdr(prices={"KS11": self.raw})          # 원천의 Change 컬럼(999)은 쓰지 않는다
        df = fetch_index_daily("KOSPI", "2026-10-01", fdr=fdr, settings=SETTINGS, sleep=lambda s: None)
        self.assertEqual(list(df.columns), ["close", "change_pct"])
        self.assertTrue(pd.isna(df["change_pct"].iloc[0]))
        self.assertEqual(df["change_pct"].iloc[1], 1.0)
        self.assertEqual(df["change_pct"].iloc[2], -0.9901)
        df2 = fetch_index_daily("KOSPI", "2026-10-01", prev_close=2000.0, fdr=fdr, settings=SETTINGS, sleep=lambda s: None)
        self.assertEqual(df2["change_pct"].iloc[0], 25.0)

    def test_timezone_and_empty(self):
        tz_raw = raw_prices([("2026-10-26", 0, 0, 0, 100.0, 0), ("2026-10-27", 0, 0, 0, 101.0, 0)], tz="America/New_York")
        fdr = FakeFdr(prices={"US500": tz_raw, "IXIC": tz_raw.iloc[0:0], "KS11": self.raw.drop(columns=["Close"])})
        df = fetch_index_daily("US500", "2026-10-01", fdr=fdr, settings=SETTINGS, sleep=lambda s: None)
        self.assertIsNone(df.index.tz)
        for symbol in ("IXIC", "KOSPI"):
            empty = fetch_index_daily(symbol, "2026-10-01", fdr=fdr, settings=SETTINGS, sleep=lambda s: None)
            self.assertEqual((len(empty), list(empty.columns)), (0, ["close", "change_pct"]))


def krx_listing():
    return pd.DataFrame({
        "Code": ["005930", "005935", "196170", "950130", "123456", "123450", "069500", "005930", None, "000020"],
        "Name": ["삼성전자", "삼성전자우", "알테오젠", "엑세스바이오", "코넥스종목", "엔에이치스팩30호", "KODEX 200", "삼성전자(중복)", "코드없음", None],
        "Market": ["KOSPI", "KOSPI", "KOSDAQ GLOBAL", "KOSDAQ", "KONEX", "KOSDAQ", "KOSPI", "KOSPI", "KOSPI", "KOSPI"],
        "Marcap": [1.0e15, 1.0e14, 5.0e12, 1.0e12, 1.0e10, 5.0e10, 5.0e12, 1.0, 1.0, 1.0],
    })


def master_listings():
    return {
        "KRX": krx_listing(),
        "KRX-DESC": pd.DataFrame({"Code": ["005930", "005935"], "ListingDate": [pd.Timestamp("1975-06-11"), pd.NaT]}),
        "ETF/KR": pd.DataFrame({"Symbol": ["069500", "102110"]}),
        "KRX-DELISTING": pd.DataFrame({"Symbol": ["000020", "000030"]}),
    }


class StockMasterTest(unittest.TestCase):
    def fetch(self, fdr):
        return fetch_stock_master(fdr=fdr, settings=SETTINGS, sleep=lambda s: None)

    def test_market_and_kind_rules(self):
        self.assertEqual([normalize_market(x) for x in ("KOSPI", "KOSDAQ", "KOSDAQ GLOBAL", "kosdaq", "KONEX", None, float("nan"))],
                         ["KOSPI", "KOSDAQ", "KOSDAQ", "KOSDAQ", None, None, None])
        self.assertEqual(classify_kind("005930", "삼성전자", set()), "common")
        self.assertEqual(classify_kind("005935", "삼성전자우", set()), "preferred")
        self.assertEqual(classify_kind("123450", "엔에이치스팩30호", set()), "spac")
        self.assertEqual(classify_kind("069500", "KODEX 200", {"069500"}), "etf")

    def test_merge_and_cleaning(self):
        res = self.fetch(FakeFdr(listings=master_listings()))
        df = res.stocks.set_index("code")
        self.assertEqual(list(df.index), sorted(df.index))                 # 코드순
        self.assertEqual(len(df), 7)                                      # 중복·코드없음·이름없음 행 제거
        self.assertEqual(df.loc["005930", "name"], "삼성전자")              # 중복은 첫 행
        self.assertEqual(df.loc["196170", "market"], "KOSDAQ")             # KOSDAQ GLOBAL 합침
        self.assertEqual(df.loc["196170", "market_raw"], "KOSDAQ GLOBAL")
        self.assertTrue(pd.isna(df.loc["123456", "market"]))               # KONEX는 시장이 비어 있다(NaN)
        self.assertEqual(df.loc["005935", "kind"], "preferred")
        self.assertEqual(df.loc["123450", "kind"], "spac")
        self.assertEqual(df.loc["069500", "kind"], "etf")
        self.assertEqual(df.loc["005930", "kind"], "common")
        self.assertEqual(df.loc["005930", "listing_date"], pd.Timestamp("1975-06-11"))
        self.assertTrue(pd.isna(df.loc["005935", "listing_date"]))         # 상장일이 비어 있는 우선주
        self.assertEqual(df.loc["005930", "marcap"], 1.0e15)
        self.assertEqual(res.etf_codes, {"069500", "102110"})
        self.assertEqual(res.delisted_codes, {"000020", "000030"})
        self.assertEqual(res.warnings, [])

    def test_optional_listings_failing_only_warn(self):
        fdr = FakeFdr(listings=master_listings(), fail_times={"KRX-DESC": 99, "ETF/KR": 99, "KRX-DELISTING": 99})
        res = self.fetch(fdr)
        self.assertEqual(len(res.stocks), 7)
        self.assertTrue(res.stocks["listing_date"].isna().all())
        self.assertEqual(res.etf_codes, set())
        self.assertEqual(len(res.warnings), 3)
        self.assertIn("상장일", res.warnings[0])
        self.assertEqual(res.stocks.set_index("code").loc["069500", "kind"], "common")   # ETF 목록이 없으면 구분 못 함

    def test_required_listing_failing_raises(self):
        with self.assertRaises(CollectorError):
            self.fetch(FakeFdr(listings=master_listings(), fail_times={"KRX": 99}))
        bad = master_listings()
        bad["KRX"] = bad["KRX"].drop(columns=["Market"])
        with self.assertRaises(CollectorError) as cm:
            self.fetch(FakeFdr(listings=bad))
        self.assertIn("Market", str(cm.exception))

    def test_missing_marcap_warns(self):
        listings = master_listings()
        listings["KRX"] = listings["KRX"].drop(columns=["Marcap"])
        res = self.fetch(FakeFdr(listings=listings))
        self.assertTrue(res.stocks["marcap"].isna().all())
        self.assertTrue(any("Marcap" in w for w in res.warnings))


@unittest.skipUnless(HAVE_FDR and os.environ.get("STOCK_NETWORK_TESTS") == "1",
                     "실제 접속 테스트는 STOCK_NETWORK_TESTS=1일 때만 실행")
class RealNetworkTest(unittest.TestCase):
    """실제 FinanceDataReader에 접속한다. 본인 PC에서만 실행."""

    def test_index_and_prices(self):
        start = date.today().replace(year=date.today().year - 1)
        kospi = fetch_index_daily("KOSPI", start, settings=SETTINGS)
        self.assertGreater(len(kospi), 200)
        self.assertTrue((kospi["close"] > 0).all())
        samsung = fetch_daily_prices("005930", start, settings=SETTINGS)
        self.assertGreater(len(samsung), 200)
        self.assertEqual(list(samsung.columns), PRICE_COLUMNS)

    def test_master(self):
        res = fetch_stock_master(settings=SETTINGS)
        s = res.stocks
        self.assertGreater(len(s), 2000)
        self.assertEqual(set(s["market"].dropna()), {"KOSPI", "KOSDAQ"})
        self.assertTrue(s["market"].isna().any())              # KONEX 등
        self.assertEqual(s.set_index("code").loc["005930", "kind"], "common")
        self.assertGreater(len(s[s["kind"] == "preferred"]), 50)
        self.assertEqual(res.warnings, [])


if __name__ == "__main__":
    unittest.main()
