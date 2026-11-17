"""지수·종목 일봉 갱신 테스트. 같은 시험을 가짜 저장소와(가능하면) 실제 DB 층(SQLite)으로 돌린다.

달력은 test_calendar의 가짜 달력: 한국 휴장 2026-10-09, 미국 휴장 2026-11-04(수), 추가 휴장일 2026-06-03.
2026-11-04는 수요일이다.
"""
import unittest
from datetime import date, datetime, timedelta

import pandas as pd

from app.collectors.common import CollectorError
from app.services.calendar import TradingCalendar
from app.services.refresh import changed_dates, refresh_indexes, refresh_stock
from app.services.support import Reason
from app.settings import Settings
from tests.backends import (
    HAVE_SQLALCHEMY, FailOnNthFdr, FakeBackend, RangeFakeFdr, SqliteBackend, index_df, kr_days, price_df,
)
from tests.test_calendar import make

CAL: TradingCalendar = make()
SETTINGS = Settings()
CODE = "005930"


def noop(_seconds):
    pass


def dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm)


def window(now):
    base = CAL.latest_base_date(now)
    return base, CAL.start_date_for_history(base, SETTINGS.history.load_trading_days)


class StockRefreshCases:
    def make_backend(self):
        raise NotImplementedError

    def setUp(self):
        self.be = self.make_backend()
        self.addCleanup(self.be.close)
        self.db = self.be.db

    # ---- 도우미
    def add_stock(self, code=CODE, market="KOSPI", is_active=True):
        with self.be.scope() as s:
            self.db.upsert_stocks(s, [{"code": code, "name": f"종목{code}", "market": market, "is_active": is_active}])

    def refresh(self, now, fdr, code=CODE):
        with self.be.scope() as s:
            return refresh_stock(s, code, CAL, now, SETTINGS, fdr=fdr, sleep=noop, db=self.db)

    def stock(self, code=CODE):
        with self.be.scope() as s:
            return self.db.get_stock(s, code)

    def prices(self, code=CODE):
        with self.be.scope() as s:
            return self.db.get_prices(s, code)

    def load_initial(self):
        """수요일 장 마감 후(11/4 17:00)에 870거래일을 처음 적재한다."""
        self.add_stock()
        now = dt(2026, 11, 4, 17, 0)
        base, start = window(now)
        days = kr_days(CAL, start, base)
        self.assertEqual(len(days), 870)
        fdr = RangeFakeFdr(prices={CODE: price_df(days)})
        self.refresh(now, fdr)
        return days, fdr

    # ---- 시험
    def test_full_load(self):
        self.add_stock()
        now = dt(2026, 11, 4, 17, 0)
        base, start = window(now)
        days = kr_days(CAL, start, base)
        fdr = RangeFakeFdr(prices={CODE: price_df(days)})
        rep = self.refresh(now, fdr)
        self.assertEqual((rep.rows_written, rep.full_reload, rep.adjusted_reload, rep.pending), (870, True, False, False))
        self.assertEqual(rep.last_loaded_date, base)
        self.assertTrue(rep.decision.supported)
        st = self.stock()
        self.assertEqual(st.last_loaded_date, base)
        self.assertTrue(st.is_active)
        df = self.prices()
        self.assertEqual(len(df), 870)
        self.assertEqual(df["close"].iloc[0], 1000.0)
        self.assertTrue(pd.isna(df["change_pct"].iloc[0]))
        self.assertAlmostEqual(df["change_pct"].iloc[1], 0.1, places=4)
        self.assertEqual(fdr.calls[0], ("data", CODE, start.isoformat(), None))

    def test_incremental_writes_only_new_rows(self):
        days, fdr = self.load_initial()
        now2 = dt(2026, 11, 5, 17, 0)
        days2 = days + [date(2026, 11, 5)]
        fdr.prices[CODE] = price_df(days2)
        rep = self.refresh(now2, fdr)
        self.assertEqual((rep.rows_written, rep.full_reload), (1, False))
        self.assertEqual(rep.last_loaded_date, date(2026, 11, 5))
        df = self.prices()
        self.assertEqual(len(df), 871)
        self.assertAlmostEqual(df["change_pct"].iloc[-1], round((1870 / 1869 - 1) * 100, 4), places=4)
        self.assertAlmostEqual(df["change_pct"].iloc[-2], round((1869 / 1868 - 1) * 100, 4), places=4)  # 기존 행은 그대로
        self.assertEqual(fdr.calls[-1], ("data", CODE, (date(2026, 11, 4) - timedelta(days=10)).isoformat(), None))
        again = self.refresh(now2, fdr)                                  # 새 데이터가 없으면 아무것도 쓰지 않는다
        self.assertEqual((again.rows_written, again.last_loaded_date), (0, date(2026, 11, 5)))

    def test_up_to_date_stock_is_not_fetched_again(self):
        days, _ = self.load_initial()
        fdr2 = RangeFakeFdr(prices={CODE: price_df(days)})
        rep = self.refresh(dt(2026, 11, 4, 17, 0), fdr2)                  # 같은 기준일에 다시 실행
        self.assertEqual(fdr2.calls, [])                                   # 네트워크를 쓰지 않는다
        self.assertEqual((rep.rows_written, rep.full_reload), (0, False))
        self.assertTrue(rep.decision.supported)                            # 지원 판정은 DB의 일봉으로 계속 한다
        self.assertEqual(self.stock().last_loaded_date, date(2026, 11, 4))

    def test_split_triggers_full_reload(self):
        days, fdr = self.load_initial()
        now2 = dt(2026, 11, 5, 17, 0)
        days2 = days + [date(2026, 11, 5)]
        adjusted = price_df(days2)
        adjusted[["Open", "High", "Low", "Close"]] = adjusted[["Open", "High", "Low", "Close"]] / 50   # 수정주가가 바뀜
        fdr.prices[CODE] = adjusted
        rep = self.refresh(now2, fdr)
        self.assertTrue(rep.adjusted_reload and rep.full_reload)
        self.assertTrue(any("전체 재적재" in w for w in rep.warnings))
        df = self.prices()
        self.assertEqual(len(df), 870)                                    # 새 기준일 기준 870거래일로 다시 적재
        self.assertAlmostEqual(df["close"].iloc[0], 1001 / 50, places=2)
        self.assertEqual(rep.last_loaded_date, date(2026, 11, 5))

    def test_only_confirmed_rows_are_stored(self):
        self.add_stock()
        now = dt(2026, 11, 4, 15, 0)                                      # 장중: 기준일은 11/3
        base, start = window(now)
        self.assertEqual(base, date(2026, 11, 3))
        days = kr_days(CAL, start, date(2026, 11, 4))                     # 오늘(장중) 행이 섞여 있음
        rep = self.refresh(now, RangeFakeFdr(prices={CODE: price_df(days)}))
        self.assertEqual(rep.rows_written, 870)
        self.assertEqual(rep.last_loaded_date, date(2026, 11, 3))
        self.assertEqual(self.prices().index.max().date(), date(2026, 11, 3))
        self.assertFalse(rep.pending)

    def test_new_listing_recovers_when_data_reaches_121_days(self):
        self.add_stock()
        base2 = date(2026, 11, 5)
        seq = kr_days(CAL, base2 - timedelta(days=200), base2)[-121:]
        fdr = RangeFakeFdr(prices={CODE: price_df(seq[:120])})
        rep1 = self.refresh(dt(2026, 11, 4, 17, 0), fdr)
        self.assertEqual(rep1.decision.reason, Reason.NEW_LISTING)
        self.assertFalse(self.stock().is_active)
        fdr.prices[CODE] = price_df(seq)
        rep2 = self.refresh(dt(2026, 11, 5, 17, 0), fdr)
        self.assertEqual(rep2.rows_written, 1)
        self.assertTrue(rep2.decision.supported)
        self.assertTrue(self.stock().is_active)

    def test_suspended_by_zero_volume(self):
        self.add_stock()
        now = dt(2026, 11, 4, 17, 0)
        base, start = window(now)
        rep = self.refresh(now, RangeFakeFdr(prices={CODE: price_df(kr_days(CAL, start, base), last_volume=0.0)}))
        self.assertEqual(rep.decision.reason, Reason.SUSPENDED)
        self.assertFalse(self.stock().is_active)

    def test_missing_base_row_is_pending_then_suspended(self):
        self.add_stock()
        base, start = window(dt(2026, 11, 4, 17, 0))
        through_prev = kr_days(CAL, start, date(2026, 11, 3))              # 11/4 일봉이 아직 없음
        fdr = RangeFakeFdr(prices={CODE: price_df(through_prev)})
        early = self.refresh(dt(2026, 11, 4, 17, 0), fdr)                  # 장 마감 직후: 데이터 지연일 수 있음
        self.assertTrue(early.pending)
        self.assertIsNone(early.decision)
        self.assertTrue(any("아직" in w for w in early.warnings))
        self.assertTrue(self.stock().is_active)                            # 지원 여부를 바꾸지 않는다
        late = self.refresh(dt(2026, 11, 4, 20, 0), fdr)                   # 19:30 이후에도 없으면 거래정지로 본다
        self.assertFalse(late.pending)
        self.assertEqual(late.decision.reason, Reason.SUSPENDED)
        self.assertFalse(self.stock().is_active)

    def test_no_data(self):
        self.add_stock()
        rep = self.refresh(dt(2026, 11, 4, 20, 0), RangeFakeFdr(prices={CODE: price_df([])}))
        self.assertEqual(rep.rows_written, 0)
        self.assertEqual(rep.decision.reason, Reason.NO_DATA)
        self.assertIsNone(self.stock().last_loaded_date)

    def test_collector_failure_changes_nothing(self):
        self.add_stock()
        fdr = RangeFakeFdr(prices={CODE: price_df([])}, fail_times={CODE: 99})
        with self.assertRaises(CollectorError):
            self.refresh(dt(2026, 11, 4, 17, 0), fdr)
        with self.be.scope() as s:
            self.assertIsNone(self.db.get_last_price_date(s, CODE))
        self.assertIsNone(self.stock().last_loaded_date)

    def test_unknown_stock(self):
        with self.assertRaises(KeyError):
            self.refresh(dt(2026, 11, 4, 17, 0), RangeFakeFdr(), code="999999")

    def test_failure_in_adjusted_reload_rolls_back_the_delete(self):
        if not self.be.supports_rollback:
            self.skipTest("가짜 저장소는 트랜잭션을 흉내 내지 않는다")
        days, _ = self.load_initial()
        adjusted = price_df(days + [date(2026, 11, 5)])
        adjusted[["Open", "High", "Low", "Close"]] = adjusted[["Open", "High", "Low", "Close"]] / 50
        flaky = FailOnNthFdr(prices={CODE: adjusted}, fail_from=2)       # 겹침 확인은 되고, 전체 재적재 호출에서 실패
        with self.assertRaises(CollectorError):
            self.refresh(dt(2026, 11, 5, 17, 0), flaky)
        df = self.prices()
        self.assertEqual(len(df), 870)                                    # 삭제가 되돌려져 원래 데이터가 남는다
        self.assertEqual(df["close"].iloc[0], 1000.0)

    def test_changed_dates_tolerance(self):
        idx = pd.bdate_range("2026-10-26", periods=3)
        db_c = pd.Series([100.0, 20000.0, 51620.0], index=idx)
        same = pd.Series([100.004, 20001.0, 51620.0], index=idx)           # 0.01원 이하, 0.01% 이하 차이는 무시
        self.assertEqual(changed_dates(db_c, same), [])
        halved = pd.Series([50.0, 10000.0, 25810.0], index=idx)
        self.assertEqual(len(changed_dates(db_c, halved)), 3)


class IndexRefreshCases:
    def make_backend(self):
        raise NotImplementedError

    def setUp(self):
        self.be = self.make_backend()
        self.addCleanup(self.be.close)
        self.db = self.be.db
        self.now1 = dt(2026, 11, 4, 5, 0)       # 새벽: 한국 기준일 11/3, 미국은 월요일(11/2) 세션까지 확정
        self.now2 = dt(2026, 11, 4, 17, 0)      # 오후: 한국 기준일 11/4, 미국은 화요일(11/3) 세션까지 확정
        base, self.start = window(self.now1)
        self.kr = kr_days(CAL, self.start, date(2026, 11, 4))               # 11/4(장중) 행이 섞여 있음
        d, self.us = self.start, []
        while d <= date(2026, 11, 3):                                       # 11/3(화) 미국 세션은 진행 중인 값
            if d.weekday() < 5:
                self.us.append(d)
            d += timedelta(days=1)

    def fdr(self, kr=None):
        kr = self.kr if kr is None else kr
        return RangeFakeFdr(prices={"KS11": index_df(kr), "KQ11": index_df(kr, 800.0), "US500": index_df(self.us, 5000.0)})

    def run_indexes(self, now, fdr):
        with self.be.scope() as s:
            reps = refresh_indexes(s, CAL, now, SETTINGS, fdr=fdr, sleep=noop, db=self.db)
        return {r.symbol: r for r in reps}

    def last(self, sym):
        with self.be.scope() as s:
            return self.db.get_last_index_date(s, sym)

    def test_first_load_trims_unconfirmed_rows(self):
        reps = self.run_indexes(self.now1, self.fdr())
        self.assertEqual(reps["KOSPI"].last_date, date(2026, 11, 3))        # 장중 행(11/4)은 제외
        self.assertEqual(reps["KOSPI"].rows_written, 870)
        self.assertEqual(reps["US500"].last_date, date(2026, 11, 2))        # 진행 중인 11/3 세션은 제외
        self.assertEqual(self.last("US500"), date(2026, 11, 2))
        self.assertTrue(all(not r.warnings for r in reps.values()))

    def test_incremental_run_and_change_pct(self):
        fdr = self.fdr()
        self.run_indexes(self.now1, fdr)
        reps = self.run_indexes(self.now2, fdr)
        self.assertEqual((reps["KOSPI"].rows_written, reps["KOSPI"].last_date), (2, date(2026, 11, 4)))
        self.assertEqual((reps["US500"].rows_written, reps["US500"].last_date), (2, date(2026, 11, 3)))
        with self.be.scope() as s:
            kospi = self.db.get_index(s, "KOSPI")
        self.assertEqual(len(kospi), 871)
        self.assertAlmostEqual(kospi["change_pct"].iloc[-1], round((3370 / 3369 - 1) * 100, 4), places=4)
        self.assertFalse(pd.isna(kospi["change_pct"].iloc[-2]))               # 겹쳐 받은 덕에 마지막 적재일의 등락률도 유지
        before = kospi["close"].tolist()
        self.run_indexes(self.now2, fdr)                                              # 다시 실행해도 결과가 같다(멱등)
        with self.be.scope() as s:
            self.assertEqual(self.db.get_index(s, "KOSPI")["close"].tolist(), before)

    def test_up_to_date_indexes_are_not_fetched_again(self):
        self.run_indexes(self.now1, self.fdr())
        fdr2 = self.fdr()
        reps = self.run_indexes(self.now1, fdr2)                          # 같은 시각에 다시 실행
        self.assertEqual(fdr2.calls, [])
        self.assertEqual({k: r.rows_written for k, r in reps.items()}, {"KOSPI": 0, "KOSDAQ": 0, "US500": 0})
        self.assertEqual(reps["KOSPI"].last_date, date(2026, 11, 3))

    def test_calendar_mismatch_warnings(self):
        missing = [d for d in self.kr if d != date(2026, 10, 28)] + [date(2026, 10, 31)]   # 거래일 하나 빠지고 토요일이 하나 있음
        reps = self.run_indexes(self.now1, self.fdr(sorted(missing)))
        text = " ".join(reps["KOSPI"].warnings)
        self.assertIn("휴장일 누락", text)
        self.assertIn("2026-10-28", text)
        self.assertIn("달력에는 거래일이 아님", text)
        self.assertEqual(reps["US500"].warnings, [])                          # 미국 지수는 한국 달력과 비교하지 않는다


class RefreshFakeTest(StockRefreshCases, unittest.TestCase):
    def make_backend(self):
        return FakeBackend()


class IndexFakeTest(IndexRefreshCases, unittest.TestCase):
    def make_backend(self):
        return FakeBackend()


@unittest.skipUnless(HAVE_SQLALCHEMY, "SQLAlchemy가 설치되어 있지 않아 건너뜀")
class RefreshSqliteTest(StockRefreshCases, unittest.TestCase):
    def make_backend(self):
        return SqliteBackend()


@unittest.skipUnless(HAVE_SQLALCHEMY, "SQLAlchemy가 설치되어 있지 않아 건너뜀")
class IndexSqliteTest(IndexRefreshCases, unittest.TestCase):
    def make_backend(self):
        return SqliteBackend()


if __name__ == "__main__":
    unittest.main()
