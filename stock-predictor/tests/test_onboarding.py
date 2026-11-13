"""종목 마스터 동기화·편입·시드 종목 테스트(가짜 저장소 + 가능하면 SQLite)."""
import unittest
from dataclasses import replace
from datetime import date, datetime

import pandas as pd

from app.collectors.stock_master import fetch_stock_master
from app.services.onboarding import onboard_seeds, onboard_stock, select_seed_codes, sync_master
from app.services.support import Reason
from app.settings import CollectorSettings, SeedSettings, Settings
from tests.backends import HAVE_SQLALCHEMY, FakeBackend, RangeFakeFdr, SqliteBackend, kr_days, price_df
from tests.test_calendar import make
from tests.test_collectors import FakeFdr, master_listings

CAL = make()
NOW = datetime(2026, 11, 4, 17, 0)           # 수요일 장 마감 후
BASE = CAL.latest_base_date(NOW)
START = CAL.start_date_for_history(BASE, 870)
DAYS = kr_days(CAL, START, BASE)


def noop(_seconds):
    pass


def build_master(listings=None):
    fdr = FakeFdr(listings=listings or master_listings())
    return fetch_stock_master(fdr=fdr, settings=CollectorSettings(), sleep=noop)


class OnboardingCases:
    def make_backend(self):
        raise NotImplementedError

    def setUp(self):
        self.be = self.make_backend()
        self.addCleanup(self.be.close)
        self.db = self.be.db
        self.master = build_master()

    def sync(self, master=None):
        with self.be.scope() as s:
            return sync_master(s, master or self.master, db=self.db)

    def stock(self, code):
        with self.be.scope() as s:
            return self.db.get_stock(s, code)

    def onboard(self, code, fdr, master_row=None, settings=None):
        with self.be.scope() as s:
            return onboard_stock(s, code, CAL, NOW, settings or Settings(), master_row=master_row, fdr=fdr, sleep=noop, db=self.db)

    def row_of(self, code):
        return self.master.stocks.set_index("code", drop=False).loc[code].to_dict()

    # ---- 종목 마스터 동기화
    def test_sync_master_first_run(self):
        rep = self.sync()
        self.assertEqual((rep.total, rep.inserted, rep.updated, rep.excluded, rep.delisted, rep.skipped_market), (6, 6, 0, 3, 0, 1))
        active = {c: self.stock(c).is_active for c in ("005930", "196170", "950130", "005935", "123450", "069500")}
        self.assertEqual(active, {"005930": True, "196170": True, "950130": True, "005935": False, "123450": False, "069500": False})
        self.assertEqual(self.stock("196170").market, "KOSDAQ")      # KOSDAQ GLOBAL이 KOSDAQ으로 저장됨
        self.assertIsNone(self.stock("123456"))                       # KONEX는 저장하지 않는다

    def test_sync_master_updates_delists_and_keeps_flags(self):
        self.sync()
        with self.be.scope() as s:
            self.db.set_stock_flags(s, "005930", is_tracked=True, is_seed=True)
            self.db.set_last_loaded_date(s, "005930", date(2026, 11, 3))
            self.db.set_stock_flags(s, "950130", is_active=False)     # 일봉 기준으로 꺼져 있던 종목
        listings = master_listings()
        krx = listings["KRX"]
        krx = krx[krx["Code"] != "196170"].copy()                      # 상장 목록에서 사라짐
        krx.loc[krx["Code"] == "950130", "Name"] = "엑세스바이오(변경)"
        listings["KRX"] = krx
        rep = self.sync(build_master(listings))
        self.assertEqual((rep.inserted, rep.updated, rep.delisted), (0, 5, 1))
        self.assertFalse(self.stock("196170").is_active)               # 상장폐지·코드 변경 후보
        self.assertEqual(self.stock("950130").name, "엑세스바이오(변경)")
        self.assertTrue(self.stock("950130").is_active)                # 종목 목록 기준 OK면 True로 두고, 일봉 판정은 갱신이 다시 한다
        st = self.stock("005930")
        self.assertTrue(st.is_tracked and st.is_seed)                  # 편입·시드 표시와 마지막 적재일은 유지
        self.assertEqual(st.last_loaded_date, date(2026, 11, 3))

    # ---- 시드 선정(순수 함수)
    def test_select_seed_codes(self):
        df = pd.DataFrame({
            "code": ["A0", "B0", "C0", "D0", "E0", "F0", "G0", "H0", "I5"],
            "market": ["KOSPI"] * 5 + ["KOSDAQ"] * 3 + ["KOSPI"],
            "kind": ["common", "common", "preferred", "common", "common", "common", "common", "common", "common"],
            "marcap": [100.0, 300.0, 900.0, float("nan"), 150.0, 50.0, 70.0, 60.0, 400.0],
        })
        sel = select_seed_codes(df, 2, 3)
        self.assertEqual(sel.codes, ["I5", "B0", "G0", "H0", "F0"])    # 우선주·시가총액 없음 제외, 시장별 시가총액 순
        self.assertEqual(sel.shortfall, {})
        sel2 = select_seed_codes(df, 2, 4)
        self.assertEqual(sel2.shortfall, {"KOSDAQ": 1})
        tie = pd.DataFrame({"code": ["B0", "A0"], "market": ["KOSPI", "KOSPI"], "kind": ["common"] * 2, "marcap": [1.0, 1.0]})
        self.assertEqual(select_seed_codes(tie, 2, 0).codes, ["A0", "B0"])   # 같으면 코드순

    # ---- 종목 편입
    def test_onboard_not_found_and_excluded(self):
        self.sync()
        fdr = RangeFakeFdr(prices={})
        self.assertEqual(self.onboard("999999", fdr).status, "not_found")
        generic = self.onboard("005935", fdr)                          # 우선주(제외, 아직 편입 안 됨)
        self.assertEqual(generic.status, "excluded")
        self.assertEqual(generic.decision.reason, Reason.EXCLUDED)
        specific = self.onboard("005935", fdr, master_row=self.row_of("005935"))
        self.assertEqual(specific.decision.reason, Reason.PREFERRED)
        self.assertFalse([c for c in fdr.calls if c[0] == "data"])     # 일봉은 받지 않는다
        self.assertFalse(self.stock("005935").is_tracked)

    def test_onboard_normal_stock_then_already_tracked(self):
        self.sync()
        fdr = RangeFakeFdr(prices={"005930": price_df(DAYS)})
        res = self.onboard("005930", fdr)
        self.assertEqual(res.status, "onboarded")
        self.assertTrue(res.decision.supported)
        st = self.stock("005930")
        self.assertTrue(st.is_tracked)
        self.assertEqual(st.last_loaded_date, BASE)
        again = self.onboard("005930", fdr)
        self.assertEqual(again.status, "already_tracked")
        self.assertEqual(again.refresh.rows_written, 0)

    def test_onboard_new_listing_is_tracked_but_not_active(self):
        self.sync()
        fdr = RangeFakeFdr(prices={"196170": price_df(DAYS[-50:])})
        res = self.onboard("196170", fdr)
        self.assertEqual(res.status, "onboarded")
        self.assertEqual(res.decision.reason, Reason.NEW_LISTING)
        st = self.stock("196170")
        self.assertTrue(st.is_tracked)                                  # 편입해 두어야 매일 갱신에서 다시 판정된다
        self.assertFalse(st.is_active)

    # ---- 시드 종목
    def seed_settings(self):
        return replace(Settings(), seed=SeedSettings(kospi_count=1, kosdaq_count=2))

    def run_seeds(self, fdr, progress=None):
        return onboard_seeds(self.be.scope, self.master, CAL, NOW, self.seed_settings(), fdr=fdr, sleep=noop, db=self.db, progress=progress)

    def test_onboard_seeds_continues_after_failure_and_is_rerunnable(self):
        self.sync()
        seeds = ["005930", "196170", "950130"]                          # KOSPI 1개 + KOSDAQ 2개(시가총액 순)
        fdr = RangeFakeFdr(prices={c: price_df(DAYS) for c in seeds}, fail_times={"950130": 99})
        seen = []
        rep = self.run_seeds(fdr, progress=lambda i, n, code: seen.append((i, n, code)))
        self.assertEqual(seen, [(1, 3, "005930"), (2, 3, "196170"), (3, 3, "950130")])
        self.assertEqual((rep.selected, rep.onboarded), (3, 2))
        self.assertEqual([c for c, _ in rep.failed], ["950130"])
        self.assertEqual((rep.excluded, rep.not_found, rep.shortfall), ([], [], {}))
        for c in seeds:
            self.assertTrue(self.stock(c).is_seed, c)                   # 실패한 종목도 시드 표시는 남는다
        self.assertTrue(self.stock("005930").is_tracked and self.stock("196170").is_tracked)
        self.assertFalse(self.stock("950130").is_tracked)
        fdr.fail_times.clear()
        rep2 = self.run_seeds(fdr)                                      # 다시 실행하면 실패했던 종목만 새로 편입
        self.assertEqual((rep2.onboarded, rep2.failed), (3, []))
        self.assertTrue(self.stock("950130").is_tracked)

    def test_seeds_before_sync_are_reported_not_found(self):
        fdr = RangeFakeFdr(prices={})
        rep = self.run_seeds(fdr)
        self.assertEqual(sorted(rep.not_found), ["005930", "196170", "950130"])
        self.assertEqual(rep.onboarded, 0)


class OnboardingFakeTest(OnboardingCases, unittest.TestCase):
    def make_backend(self):
        return FakeBackend()


@unittest.skipUnless(HAVE_SQLALCHEMY, "SQLAlchemy가 설치되어 있지 않아 건너뜀")
class OnboardingSqliteTest(OnboardingCases, unittest.TestCase):
    def make_backend(self):
        return SqliteBackend()


if __name__ == "__main__":
    unittest.main()
