"""작업(jobs) 테스트: 갱신, 편입, 시드, 백업, 잠금. 가짜 저장소 + 가능하면 SQLite로 돌린다.

달력은 test_calendar의 가짜 달력(한국 휴장 2026-10-09, 미국 휴장 2026-11-04)이다. 2026-11-04는 수요일.
"""
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from app.jobs.context import JobContext
from app.jobs.locks import claim_run, finish_run, job_lock, stock_lock
from app.jobs.run_backup import BackupResult, run_backup
from app.jobs.run_onboard import run_onboard
from app.jobs.run_refresh import run_refresh
from app.jobs.run_seed import run_seed
from app.collectors.stock_master import fetch_stock_master
from app.services.onboarding import onboard_stock, sync_master
from app.settings import CollectorSettings, DatabaseSettings, PathSettings, SeedSettings, Settings
from tests.backends import HAVE_SQLALCHEMY, FakeBackend, RangeFakeFdr, SqliteBackend, index_df, kr_days, price_df
from tests.test_calendar import make
from tests.test_collectors import master_listings

CAL = make()
SETTINGS = Settings()
NOW1 = datetime(2026, 11, 4, 17, 0)      # 수요일 장 마감 후
NOW2 = datetime(2026, 11, 5, 17, 0)      # 목요일 장 마감 후
BASE1, BASE2 = CAL.latest_base_date(NOW1), CAL.latest_base_date(NOW2)
DAYS1 = kr_days(CAL, CAL.start_date_for_history(BASE1, 870), BASE1)
DAYS2 = DAYS1 + [BASE2]
START2 = CAL.start_date_for_history(BASE2, 870)
KR2 = kr_days(CAL, START2, BASE2)
US2 = [d for d in (START2 + timedelta(days=i) for i in range((BASE2 - START2).days + 1)) if d.weekday() < 5 and d != date(2026, 11, 4)]
TRACKED = ("005930", "196170")


def noop(_s):
    pass


def frames(codes=TRACKED, days=DAYS2):
    prices = {c: price_df(days) for c in codes}
    prices.update({"KS11": index_df(KR2), "KQ11": index_df(KR2, 800.0), "US500": index_df(US2, 5000.0)})
    return prices


def make_fdr(prices=None, **kw):
    return RangeFakeFdr(listings=master_listings(), prices=prices if prices is not None else frames(), **kw)


def data_symbols(fdr):
    return {c[1] for c in fdr.calls if c[0] == "data"}


class JobCases:
    def make_backend(self):
        raise NotImplementedError

    def setUp(self):
        self.be = self.make_backend()
        self.addCleanup(self.be.close)
        self.db = self.be.db
        self.logs = []

    def ctx(self, fdr, now=NOW2, settings=SETTINGS):
        return JobContext(settings=settings, calendar=CAL, scope=self.be.scope, db=self.db, fdr=fdr, sleep=noop,
                          now_fn=lambda: now, logger=self.logs.append)

    def initial_state(self, codes=TRACKED):
        """수요일(11/4) 장 마감 후에 종목 마스터를 반영하고 codes를 편입해 둔 상태."""
        master = fetch_stock_master(fdr=make_fdr(), settings=CollectorSettings(), sleep=noop)
        fdr1 = RangeFakeFdr(prices={c: price_df(DAYS1) for c in codes})
        with self.be.scope() as s:
            sync_master(s, master, db=self.db)
        for code in codes:
            with self.be.scope() as s:
                onboard_stock(s, code, CAL, NOW1, SETTINGS, fdr=fdr1, sleep=noop, db=self.db)

    def stock(self, code):
        with self.be.scope() as s:
            return self.db.get_stock(s, code)

    def last_success(self, job):
        with self.be.scope() as s:
            return self.db.get_last_success_run(s, job)

    # ---------------------------------------------------------------- run_refresh
    def test_refresh_full_flow_and_second_run(self):
        self.initial_state()
        seen = []
        rep = run_refresh(self.ctx(make_fdr()), backup_fn=None, hooks=[lambda ctx, s, r: seen.append((r.code, r.rows_written))])
        self.assertEqual(rep.status, "success")
        self.assertEqual((rep.stocks_ok, rep.stocks_failed, rep.index_failed), (2, [], []))
        self.assertEqual(sorted(seen), [("005930", 1), ("196170", 1)])   # 종목마다 훅이 불리고 새 행은 하루치
        self.assertTrue(rep.master_synced)
        self.assertEqual({r.symbol for r in rep.index_reports}, {"KOSPI", "KOSDAQ", "US500"})
        with self.be.scope() as s:
            self.assertEqual(self.db.get_last_index_date(s, "KOSPI"), BASE2)
            self.assertEqual(self.db.get_last_index_date(s, "US500"), date(2026, 11, 3))
        self.assertEqual(self.stock("005930").last_loaded_date, BASE2)
        self.assertIsNotNone(self.last_success("refresh"))
        self.assertIsNotNone(self.last_success("master"))
        again = run_refresh(self.ctx(make_fdr()), backup_fn=None)          # 같은 날 다시 실행
        self.assertEqual(again.status, "success")
        self.assertFalse(again.master_synced)                              # 마스터는 하루 한 번
        self.assertEqual(again.stocks_ok, 2)

    def test_skips_when_already_running_and_closes_stale(self):
        self.initial_state()
        with self.be.scope() as s:
            self.db.start_run(s, "refresh", NOW2)                          # 방금 시작된 실행이 있음
        fdr = make_fdr()
        rep = run_refresh(self.ctx(fdr), backup_fn=None)
        self.assertEqual(rep.status, "skipped_running")
        self.assertEqual(data_symbols(fdr), set())                         # 아무것도 받지 않는다
        self.assertIsNone(self.last_success("refresh"))

    def test_stale_running_record_does_not_block(self):
        self.initial_state()
        with self.be.scope() as s:
            self.db.start_run(s, "refresh", NOW2 - timedelta(hours=7))     # 7시간 전 비정상 종료 흔적
        rep = run_refresh(self.ctx(make_fdr()), backup_fn=None)
        self.assertEqual(rep.status, "success")
        self.assertTrue(any("비정상 종료" in m for m in self.logs))

    def test_failures_are_isolated(self):
        self.initial_state()
        rep = run_refresh(self.ctx(make_fdr(fail_times={"196170": 99, "US500": 99})), backup_fn=None)
        self.assertEqual(rep.status, "failed")
        self.assertEqual(rep.stocks_ok, 1)
        self.assertEqual([c for c, _ in rep.stocks_failed], ["196170"])
        self.assertEqual([s for s, _ in rep.index_failed], ["US500"])
        with self.be.scope() as s:
            self.assertEqual(self.db.get_last_index_date(s, "KOSPI"), BASE2)    # 다른 지수는 갱신됨
            self.assertIsNone(self.db.get_last_index_date(s, "US500"))
        self.assertEqual(self.stock("005930").last_loaded_date, BASE2)
        self.assertEqual(self.stock("196170").last_loaded_date, BASE1)         # 실패한 종목은 그대로
        self.assertIsNone(self.last_success("refresh"))                        # 실행 기록은 failed
        self.assertTrue(any("종목 실패" in m for m in self.logs))

    def test_master_failure_continues_and_retries_next_time(self):
        self.initial_state()
        fdr = make_fdr(fail_times={"KRX": 99})
        rep = run_refresh(self.ctx(fdr), backup_fn=None)
        self.assertFalse(rep.master_synced)
        self.assertEqual(rep.stocks_ok, 2)                                     # 기존 종목 목록으로 계속
        self.assertTrue(any("종목 마스터를 갱신하지 못했습니다" in w for w in rep.warnings))
        self.assertIsNone(self.last_success("master"))
        retry = run_refresh(self.ctx(make_fdr()), backup_fn=None)
        self.assertTrue(retry.master_synced)

    def test_only_tracked_active_stocks_are_refreshed(self):
        self.initial_state()
        with self.be.scope() as s:                                             # 우선주를 편입해 둠(마스터 동기화에서 제외 상태가 됨)
            self.db.set_stock_flags(s, "005935", is_tracked=True)
        fdr = make_fdr()
        rep = run_refresh(self.ctx(fdr), backup_fn=None)
        self.assertEqual(data_symbols(fdr), {"KS11", "KQ11", "US500", "005930", "196170"})   # 950130(미편입), 005935(제외)는 받지 않음
        self.assertEqual(rep.stocks_ok, 2)

    def test_backup_result_recorded_and_backup_errors_do_not_stop(self):
        self.initial_state()
        calls = []
        rep = run_refresh(self.ctx(make_fdr()), backup_fn=lambda c: calls.append(1) or BackupResult("created", message="ok"))
        self.assertEqual((rep.status, rep.backup.status, len(calls)), ("success", "created", 1))

        def boom(_ctx):
            raise RuntimeError("disk full")

        rep2 = run_refresh(self.ctx(make_fdr()), backup_fn=boom)
        self.assertEqual(rep2.status, "success")
        self.assertTrue(any("백업 중 오류" in w for w in rep2.warnings))

    def test_unexpected_error_in_one_stock_is_isolated(self):
        self.initial_state()

        def hook(ctx, session, report):
            if report.code == "196170":
                raise ValueError("bug")

        rep = run_refresh(self.ctx(make_fdr()), backup_fn=None, hooks=[hook])
        self.assertEqual(rep.status, "failed")
        self.assertEqual(rep.stocks_ok, 1)
        self.assertEqual(rep.stocks_failed[0][0], "196170")
        self.assertIn("예상 밖 오류", rep.stocks_failed[0][1])
        if self.be.supports_rollback:
            self.assertEqual(self.stock("196170").last_loaded_date, BASE1)    # 그 종목의 쓰기는 되돌려진다

    # ---------------------------------------------------------------- run_onboard
    def test_onboard_job_success_failure_and_not_found(self):
        master = fetch_stock_master(fdr=make_fdr(), settings=CollectorSettings(), sleep=noop)
        with self.be.scope() as s:
            sync_master(s, master, db=self.db)
        out = run_onboard(self.ctx(make_fdr(), now=NOW1), "005930")
        self.assertEqual((out.status, out.result.status), ("success", "onboarded"))
        self.assertTrue(out.result.decision.supported)
        self.assertIsNotNone(self.last_success("onboard:005930"))
        self.assertTrue(self.stock("005930").is_tracked)
        bad = run_onboard(self.ctx(make_fdr(fail_times={"196170": 99}), now=NOW1), "196170")
        self.assertEqual(bad.status, "failed")
        self.assertIn("196170", bad.error)
        self.assertFalse(self.stock("196170").is_tracked)
        self.assertIsNone(self.last_success("onboard:196170"))
        missing = run_onboard(self.ctx(make_fdr(), now=NOW1), "999999")
        self.assertEqual((missing.status, missing.result.status), ("success", "not_found"))

    def test_concurrent_requests_for_same_stock_are_serialized(self):
        if self.be.name != "fake":
            self.skipTest("한 연결을 공유하는 메모리 SQLite는 스레드 동시 사용 시험에 맞지 않는다")
        master = fetch_stock_master(fdr=make_fdr(), settings=CollectorSettings(), sleep=noop)
        with self.be.scope() as s:
            sync_master(s, master, db=self.db)

        state = {"now": 0, "max": 0}
        guard = threading.Lock()

        class SlowFdr(RangeFakeFdr):
            def DataReader(self, symbol, start=None, end=None):
                with guard:
                    state["now"] += 1
                    state["max"] = max(state["max"], state["now"])
                time.sleep(0.05)
                try:
                    return super().DataReader(symbol, start, end)
                finally:
                    with guard:
                        state["now"] -= 1

        fdr = SlowFdr(listings=master_listings(), prices={"005930": price_df(DAYS1)})
        results = []
        threads = [threading.Thread(target=lambda: results.append(run_onboard(self.ctx(fdr, now=NOW1), "005930"))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(state["max"], 1)                                         # 동시에 받지 않는다
        self.assertEqual(sorted(r.result.status for r in results), ["already_tracked", "onboarded"])

    # ---------------------------------------------------------------- run_seed
    def seed_settings(self):
        return replace(SETTINGS, seed=SeedSettings(kospi_count=1, kosdaq_count=2))

    def test_seed_job_failure_then_rerun(self):
        seeds = ("005930", "196170", "950130")
        fdr = make_fdr(frames(seeds, DAYS1), fail_times={"950130": 99})
        progress = []
        rep = run_seed(self.ctx(fdr, now=NOW1, settings=self.seed_settings()), progress=lambda i, n, c: progress.append(c))
        self.assertEqual(progress, ["005930", "196170", "950130"])
        self.assertEqual(rep.status, "failed")
        self.assertEqual((rep.seed.selected, rep.seed.onboarded), (3, 2))
        self.assertEqual(rep.sync.total, 6)
        self.assertIsNone(self.last_success("seed"))
        fdr.fail_times.clear()
        again = run_seed(self.ctx(fdr, now=NOW1, settings=self.seed_settings()))
        self.assertEqual((again.status, again.seed.onboarded), ("success", 3))
        self.assertIsNotNone(self.last_success("seed"))
        self.assertTrue(all(self.stock(c).is_seed and self.stock(c).is_tracked for c in seeds))

    def test_seed_job_skips_when_running(self):
        with self.be.scope() as s:
            self.db.start_run(s, "seed", NOW1)
        rep = run_seed(self.ctx(make_fdr(frames(("005930",), DAYS1)), now=NOW1, settings=self.seed_settings()))
        self.assertEqual(rep.status, "skipped_running")

    # ---------------------------------------------------------------- 잠금과 실행 기록
    def test_claim_and_finish_run(self):
        ctx = self.ctx(make_fdr(), now=NOW1)
        first = claim_run(ctx, "x")
        self.assertIsNotNone(first)
        self.assertIsNone(claim_run(ctx, "x"))                                  # 실행 중이면 None
        self.assertIsNotNone(claim_run(ctx, "y"))                               # 다른 작업은 영향 없음
        finish_run(ctx, first, "success", 5, "끝")
        self.assertIsNotNone(claim_run(ctx, "x"))                               # 끝난 뒤에는 다시 시작 가능
        finish_run(ctx, None, "success")                                         # run_id가 없으면 아무것도 하지 않는다


class JobFakeTest(JobCases, unittest.TestCase):
    def make_backend(self):
        return FakeBackend()


@unittest.skipUnless(HAVE_SQLALCHEMY, "SQLAlchemy가 설치되어 있지 않아 건너뜀")
class JobSqliteTest(JobCases, unittest.TestCase):
    def make_backend(self):
        return SqliteBackend()


# ---------------------------------------------------------------- 잠금(DB 불필요)
class LockTest(unittest.TestCase):
    def test_job_lock_is_non_blocking_and_released(self):
        with job_lock("t-job") as first:
            self.assertTrue(first)
            with job_lock("t-job") as second:
                self.assertFalse(second)                                        # 이미 실행 중
            with job_lock("t-other") as other:
                self.assertTrue(other)                                          # 다른 이름은 독립
        with job_lock("t-job") as again:
            self.assertTrue(again)                                              # 끝나면 다시 얻을 수 있다

    def test_stock_lock_identity(self):
        self.assertIs(stock_lock("005930"), stock_lock("005930"))
        self.assertIsNot(stock_lock("005930"), stock_lock("000660"))


# ---------------------------------------------------------------- 백업(DB 불필요)
class BackupTest(unittest.TestCase):
    TODAY = datetime(2026, 11, 4, 8, 0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.settings = replace(
            SETTINGS,
            database=DatabaseSettings(host="db.local", port=3307, user="stockuser", name="stockdb", password="pw!secret"),
            paths=PathSettings(backup_dir=str(self.dir), backup_keep_days=7),
        )
        self.ctx = JobContext(settings=self.settings, calendar=CAL, scope=FakeBackend().scope, now_fn=lambda: self.TODAY)
        self.calls = []

    def runner_ok(self, cmd, stdout=None, stderr=None, env=None, check=None):
        self.calls.append((cmd, env))
        stdout.write(b"-- dump\n")

    def test_creates_file_with_password_in_environment_only(self):
        res = run_backup(self.ctx, runner=self.runner_ok, which=lambda n: "/usr/bin/" + n)
        self.assertEqual(res.status, "created")
        self.assertEqual(res.path, self.dir / "stockdb_2026-11-04.sql")
        self.assertEqual(res.path.read_bytes(), b"-- dump\n")
        self.assertEqual(list(self.dir.glob("*.part")), [])
        cmd, env = self.calls[0]
        self.assertEqual(cmd[0], "/usr/bin/mariadb-dump")                        # mariadb-dump를 먼저 찾는다
        self.assertIn("--host=db.local", cmd)
        self.assertIn("--port=3307", cmd)
        self.assertIn("--user=stockuser", cmd)
        self.assertEqual(cmd[-1], "stockdb")
        self.assertFalse(any("pw!secret" in c for c in cmd))                    # 명령줄에 비밀번호를 쓰지 않는다
        self.assertEqual(env["MYSQL_PWD"], "pw!secret")

    def test_falls_back_to_mysqldump(self):
        res = run_backup(self.ctx, runner=self.runner_ok, which=lambda n: "/x/mysqldump" if n == "mysqldump" else None)
        self.assertEqual(res.status, "created")
        self.assertEqual(self.calls[0][0][0], "/x/mysqldump")

    def test_skips_when_todays_file_exists(self):
        run_backup(self.ctx, runner=self.runner_ok, which=lambda n: n)
        res = run_backup(self.ctx, runner=self.runner_ok, which=lambda n: n)
        self.assertEqual(res.status, "skipped_exists")
        self.assertEqual(len(self.calls), 1)

    def test_cleanup_keeps_seven_days_and_ignores_other_files(self):
        for name in ("stockdb_2026-10-20.sql", "stockdb_2026-10-28.sql", "stockdb_2026-10-29.sql",
                     "otherdb_2026-10-01.sql", "notes.txt"):
            (self.dir / name).write_text("x")
        res = run_backup(self.ctx, runner=self.runner_ok, which=lambda n: n)
        self.assertEqual(sorted(p.name for p in res.removed), ["stockdb_2026-10-20.sql", "stockdb_2026-10-28.sql"])   # 7일 이상 지난 것
        remaining = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(remaining, ["notes.txt", "otherdb_2026-10-01.sql", "stockdb_2026-10-29.sql", "stockdb_2026-11-04.sql"])

    def test_failure_leaves_no_partial_file(self):
        def failing(cmd, stdout=None, stderr=None, env=None, check=None):
            stdout.write(b"partial")
            raise subprocess.CalledProcessError(2, cmd, stderr=b"access denied")

        res = run_backup(self.ctx, runner=failing, which=lambda n: n)
        self.assertEqual(res.status, "failed")
        self.assertIn("access denied", res.message)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_unavailable_when_no_dump_command(self):
        res = run_backup(self.ctx, runner=self.runner_ok, which=lambda n: None)
        self.assertEqual(res.status, "unavailable")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
