"""처음 한 번 학습용 시드 종목(KOSPI 70 + KOSDAQ 30)을 미리 편입하는 작업.

종목 마스터를 받아 stocks에 반영한 뒤 시장별 시가총액 상위 종목을 하나씩 편입한다.
한 종목이 실패해도 계속하고, 다시 실행하면 이미 편입한 종목은 증분 갱신만 하므로 안전하다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..collectors.common import CollectorError
from ..collectors.stock_master import fetch_stock_master
from ..services.onboarding import MasterSyncReport, SeedReport, onboard_seeds, sync_master
from .context import JobContext, build_context, setup_logging
from .locks import claim_run, finish_run, job_lock

JOB = "seed"


@dataclass
class SeedJobReport:
    status: str                                  # success | failed | skipped_running
    run_id: int | None = None
    sync: MasterSyncReport | None = None
    seed: SeedReport | None = None
    error: str | None = None


def run_seed(ctx: JobContext, progress: Callable[[int, int, str], None] | None = None) -> SeedJobReport:
    with job_lock(JOB) as acquired:
        if not acquired:
            return SeedJobReport("skipped_running")
        run_id = claim_run(ctx, JOB)
        if run_id is None:
            return SeedJobReport("skipped_running")
        try:
            master = fetch_stock_master(fdr=ctx.fdr, settings=ctx.settings.collector, sleep=ctx.sleep)
            with ctx.scope() as session:
                sync = sync_master(session, master, db=ctx.db)
            seed = onboard_seeds(ctx.scope, master, ctx.calendar, ctx.now(), ctx.settings,
                                 fdr=ctx.fdr, sleep=ctx.sleep, db=ctx.db, progress=progress)
        except CollectorError as exc:
            finish_run(ctx, run_id, "failed", message=str(exc))
            return SeedJobReport("failed", run_id, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            finish_run(ctx, run_id, "failed", message=repr(exc))
            raise
        status = "failed" if seed.failed else "success"
        message = (f"선정 {seed.selected}, 편입 {seed.onboarded}, 제외 {len(seed.excluded)}, 실패 {len(seed.failed)}"
                   + (f", 부족 {seed.shortfall}" if seed.shortfall else ""))
        finish_run(ctx, run_id, status, seed.onboarded, message)
        ctx.logger(f"시드 편입 {status}: {message}")
        return SeedJobReport(status, run_id, sync, seed)


def main() -> None:
    setup_logging()
    rep = run_seed(build_context(), progress=lambda i, n, c: print(f"[{i}/{n}] {c}", flush=True))
    print(rep.status, rep.error or "")
    if rep.seed:
        print(f"선정 {rep.seed.selected}, 편입 {rep.seed.onboarded}, 실패 {len(rep.seed.failed)}")
        for code, err in rep.seed.failed[:10]:
            print("실패:", code, err)


if __name__ == "__main__":
    main()
