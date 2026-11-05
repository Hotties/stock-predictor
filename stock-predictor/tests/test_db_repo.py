"""DB 층 테스트(SQLite 메모리). SQLAlchemy가 없으면 건너뛴다.

실행: pip install SQLAlchemy pandas 후  python -m unittest discover -s tests -t . -v
"""
import unittest
from datetime import date, datetime, timedelta

try:
    import pandas as pd
    import sqlalchemy  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

if HAVE:
    from app.db import repo
    from app.db.connection import create_all, make_engine, make_session_factory, session_scope


def price_frame(start, n, base=100.0):
    idx = pd.bdate_range(start, periods=n)
    close = [base + i for i in range(n)]
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": [1000 + i for i in range(n)],
         "change_pct": [None] + [1.0] * (n - 1)}, index=idx)


@unittest.skipUnless(HAVE, "SQLAlchemy가 설치되어 있지 않아 건너뜀")
class RepoTest(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine("sqlite://")
        create_all(self.engine)
        self.factory = make_session_factory(self.engine)
        self.addCleanup(self.engine.dispose)  # 테스트가 끝나면 SQLite 연결을 닫는다(ResourceWarning 방지)

    def test_upsert_stocks_keeps_flags(self):
        with session_scope(self.factory) as s:
            repo.upsert_stocks(s, [{"code": "005930", "name": "삼성전자", "market": "KOSPI"}])
            repo.set_stock_flags(s, "005930", is_tracked=True, is_seed=True)
            repo.set_last_loaded_date(s, "005930", date(2026, 10, 27))
        with session_scope(self.factory) as s:  # 이름이 바뀐 목록으로 다시 upsert
            repo.upsert_stocks(s, [{"code": "005930", "name": "삼성전자(변경)", "market": "KOSPI", "is_active": False}])
        with session_scope(self.factory) as s:
            st = repo.get_stock(s, "005930")
            self.assertEqual(st.name, "삼성전자(변경)")
            self.assertFalse(st.is_active)
            self.assertTrue(st.is_tracked)            # 편입 상태는 유지
            self.assertTrue(st.is_seed)
            self.assertEqual(st.last_loaded_date, date(2026, 10, 27))

    def test_list_stocks_filters(self):
        with session_scope(self.factory) as s:
            repo.upsert_stocks(s, [
                {"code": "000010", "name": "A", "market": "KOSPI"},
                {"code": "000020", "name": "B", "market": "KOSDAQ"},
                {"code": "000030", "name": "C", "market": "KOSPI", "is_active": False},
            ])
            repo.set_stock_flags(s, "000010", is_tracked=True)
        with session_scope(self.factory) as s:
            self.assertEqual([x.code for x in repo.list_stocks(s, is_active=True)], ["000010", "000020"])
            self.assertEqual([x.code for x in repo.list_stocks(s, is_tracked=True, is_active=True)], ["000010"])
            self.assertEqual([x.code for x in repo.list_stocks(s, market="KOSDAQ")], ["000020"])
            with self.assertRaises(ValueError):
                repo.set_stock_flags(s, "000010", name="x")

    def test_prices_roundtrip_and_upsert_overwrite(self):
        with session_scope(self.factory) as s:
            repo.upsert_stocks(s, [{"code": "000010", "name": "A", "market": "KOSPI"}])
            n = repo.upsert_prices(s, "000010", price_frame("2026-10-01", 5))
            self.assertEqual(n, 5)
        with session_scope(self.factory) as s:
            df = repo.get_prices(s, "000010")
            self.assertEqual(len(df), 5)
            self.assertEqual(df["close"].iloc[0], 100.0)
            self.assertEqual(repo.get_last_price_date(s, "000010"), date(2026, 10, 7))
            self.assertEqual(repo.count_prices(s, "000010"), 5)
            self.assertEqual(repo.get_close_on(s, "000010", date(2026, 10, 2)), 101.0)
        changed = price_frame("2026-10-05", 3, base=500.0)  # 겹치는 날짜는 덮어쓴다
        with session_scope(self.factory) as s:
            repo.upsert_prices(s, "000010", changed)
        with session_scope(self.factory) as s:
            self.assertEqual(repo.count_prices(s, "000010"), 5)   # 10/5~10/7은 덮어쓰고 행 수는 그대로
            self.assertEqual(repo.get_close_on(s, "000010", date(2026, 10, 5)), 500.0)
            self.assertEqual(repo.get_close_on(s, "000010", date(2026, 10, 2)), 101.0)  # 겹치지 않는 날은 그대로
            part = repo.get_prices(s, "000010", start=date(2026, 10, 6), end=date(2026, 10, 7))
            self.assertEqual(len(part), 2)

    def test_nan_becomes_null_and_missing_close_is_skipped(self):
        df = price_frame("2026-10-01", 3)
        df.loc[df.index[1], "close"] = float("nan")
        df.loc[df.index[2], "volume"] = float("nan")
        with session_scope(self.factory) as s:
            repo.upsert_stocks(s, [{"code": "000010", "name": "A", "market": "KOSPI"}])
            self.assertEqual(repo.upsert_prices(s, "000010", df), 2)  # 종가가 없는 행은 제외
        with session_scope(self.factory) as s:
            out = repo.get_prices(s, "000010")
            self.assertEqual(len(out), 2)
            self.assertTrue(pd.isna(out["volume"].iloc[1]))

    def test_delete_prices(self):
        with session_scope(self.factory) as s:
            repo.upsert_stocks(s, [{"code": "000010", "name": "A", "market": "KOSPI"}])
            repo.upsert_prices(s, "000010", price_frame("2026-10-01", 4))
            self.assertEqual(repo.delete_prices(s, "000010"), 4)
        with session_scope(self.factory) as s:
            self.assertEqual(repo.count_prices(s, "000010"), 0)
            self.assertIsNone(repo.get_last_price_date(s, "000010"))
            self.assertTrue(repo.get_prices(s, "000010").empty)

    def test_large_batch_is_chunked(self):
        df = price_frame("2020-01-01", 2500)
        with session_scope(self.factory) as s:
            repo.upsert_stocks(s, [{"code": "000010", "name": "A", "market": "KOSPI"}])
            self.assertEqual(repo.upsert_prices(s, "000010", df), 2500)
        with session_scope(self.factory) as s:
            self.assertEqual(repo.count_prices(s, "000010"), 2500)

    def test_index_roundtrip(self):
        idx = pd.bdate_range("2026-10-01", periods=4)
        df = pd.DataFrame({"close": [2500.0, 2510.5, 2490.0, 2520.25], "change_pct": [None, 0.42, -0.82, 1.21]}, index=idx)
        with session_scope(self.factory) as s:
            repo.upsert_index(s, "KOSPI", df)
            repo.upsert_index(s, "US500", df)
        with session_scope(self.factory) as s:
            out = repo.get_index(s, "KOSPI", start=date(2026, 10, 2))
            self.assertEqual(len(out), 3)
            self.assertEqual(repo.get_last_index_date(s, "US500"), date(2026, 10, 6))
            self.assertIsNone(repo.get_last_index_date(s, "IXIC"))

    def test_runs_lifecycle_and_stale_detection(self):
        now = datetime(2026, 11, 4, 7, 5)
        stale = timedelta(hours=6)
        with session_scope(self.factory) as s:
            self.assertFalse(repo.is_running(s, "refresh", now, stale))
            run_id = repo.start_run(s, "refresh", now)
            self.assertTrue(repo.is_running(s, "refresh", now + timedelta(minutes=5), stale))
            self.assertFalse(repo.is_running(s, "retrain", now, stale))   # 다른 작업은 영향 없음
            repo.finish_run(s, run_id, "success", processed_rows=123, now=now + timedelta(minutes=2))
            self.assertFalse(repo.is_running(s, "refresh", now + timedelta(minutes=5), stale))
        with session_scope(self.factory) as s:
            repo.start_run(s, "refresh", now)                              # 끝나지 않은 기록
            self.assertTrue(repo.is_running(s, "refresh", now + timedelta(hours=1), stale))
            self.assertFalse(repo.is_running(s, "refresh", now + timedelta(hours=7), stale))  # 오래돼서 무시
            self.assertEqual(repo.close_stale_runs(s, "refresh", now + timedelta(hours=7), stale), 1)
            with self.assertRaises(ValueError):
                repo.finish_run(s, 1, "running")


if __name__ == "__main__":
    unittest.main()
