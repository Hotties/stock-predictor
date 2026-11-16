"""프로그램을 실행할 때 데이터를 최신으로 맞추는 작업 (파이프라인 B).

순서: DB 백업(하루 첫 실행, 실패해도 계속) -> 실행 기록 시작(이미 실행 중이면 종료)
      -> B1 지수 갱신 -> B2 종목 마스터 갱신(하루 한 번) -> B3 편입 종목마다 일봉 갱신·수정주가 검사·지원 판정 -> 실행 기록 마감.
M2에서 종목마다 실제값 채우기와 점수·예측 생성을 hooks로 이 흐름에 끼운다.

한 지수나 한 종목이 실패해도 나머지는 계속하고, 실패 목록을 실행 기록에 남긴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..collectors.common import CollectorError
from ..collectors.stock_master import fetch_stock_master
from ..services.onboarding import MasterSyncReport, sync_master
from ..services.refresh import INDEX_SYMBOLS, IndexRefreshReport, StockRefreshReport, _db, refresh_indexes, refresh_stock
from .context import JobContext, build_context, setup_logging
from .locks import claim_run, finish_run, job_lock
from .run_backup import BackupResult, run_backup

JOB = "refresh"
MASTER_JOB = "master"


@dataclass
class RefreshJobReport:
    status: str                                              # success | failed | skipped_running
    run_id: int | None = None
    backup: BackupResult | None = None
    index_reports: list[IndexRefreshReport] = field(default_factory=list)
    index_failed: list[tuple[str, str]] = field(default_factory=list)
    master_synced: bool = False
    master: MasterSyncReport | None = None
    stocks_ok: int = 0
    stocks_failed: list[tuple[str, str]] = field(default_factory=list)
    rows_written: int = 0
    warnings: list[str] = field(default_factory=list)


def _master_due(ctx: JobContext, now) -> bool:
    db = _db(ctx.db)
    with ctx.scope() as session:
        last = db.get_last_success_run(session, MASTER_JOB)
    return last is None or last.date() < now.date()


def _master_step(ctx: JobContext, now, rep: RefreshJobReport) -> None:
    if not _master_due(ctx, now):
        return
    run_id = claim_run(ctx, MASTER_JOB)
    if run_id is None:
        return
    try:
        master = fetch_stock_master(fdr=ctx.fdr, settings=ctx.settings.collector, sleep=ctx.sleep)
        with ctx.scope() as session:
            sync = sync_master(session, master, db=ctx.db)
    except CollectorError as exc:
        finish_run(ctx, run_id, "failed", message=str(exc))
        rep.warnings.append(f"종목 마스터를 갱신하지 못했습니다(기존 목록으로 계속): {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        finish_run(ctx, run_id, "failed", message=repr(exc))
        raise
    rep.master_synced, rep.master = True, sync
    rep.warnings.extend(master.warnings)
    finish_run(ctx, run_id, "success", sync.total,
               f"신규 {sync.inserted}, 갱신 {sync.updated}, 제외 {sync.excluded}, 상장폐지 {sync.delisted}, KONEX 등 {sync.skipped_market}")


def run_refresh(ctx: JobContext, *, backup_fn: Callable | None = run_backup, hooks: Sequence[Callable] = ()) -> RefreshJobReport:
    """hooks: 종목마다 일봉 갱신 직후 같은 트랜잭션에서 hook(ctx, session, StockRefreshReport)로 불린다(M2에서 사용)."""
    rep = RefreshJobReport("success")
    with job_lock(JOB) as acquired:
        if not acquired:
            rep.status = "skipped_running"
            return rep
        if backup_fn is not None:
            try:
                rep.backup = backup_fn(ctx)
                ctx.logger(f"백업: {rep.backup.status} {rep.backup.message}")
            except Exception as exc:  # noqa: BLE001  (백업 실패는 갱신을 멈추지 않는다)
                rep.warnings.append(f"백업 중 오류: {exc!r}")
        run_id = claim_run(ctx, JOB)
        if run_id is None:
            rep.status = "skipped_running"
            ctx.logger("이미 갱신이 실행 중이라 건너뜁니다")
            return rep
        rep.run_id = run_id
        now = ctx.now()
        db = _db(ctx.db)
        try:
            # B1 지수
            for sym in INDEX_SYMBOLS:
                try:
                    with ctx.scope() as session:
                        reps = refresh_indexes(session, ctx.calendar, now, ctx.settings, symbols=(sym,), fdr=ctx.fdr, sleep=ctx.sleep, db=ctx.db)
                    rep.index_reports.extend(reps)
                    rep.rows_written += sum(r.rows_written for r in reps)
                    for r in reps:
                        rep.warnings.extend(r.warnings)
                except CollectorError as exc:
                    rep.index_failed.append((sym, str(exc)))
            # B2 종목 마스터(하루 한 번)
            _master_step(ctx, now, rep)
            # B3 편입 종목
            with ctx.scope() as session:
                codes = [s.code for s in db.list_stocks(session, is_tracked=True, is_active=True)]
            for code in codes:
                try:
                    with ctx.scope() as session:
                        r: StockRefreshReport = refresh_stock(session, code, ctx.calendar, now, ctx.settings, fdr=ctx.fdr, sleep=ctx.sleep, db=ctx.db)
                        for hook in hooks:
                            hook(ctx, session, r)
                    rep.stocks_ok += 1
                    rep.rows_written += r.rows_written
                    rep.warnings.extend(f"{code}: {w}" for w in r.warnings)
                except CollectorError as exc:
                    rep.stocks_failed.append((code, str(exc)))
                except Exception as exc:  # noqa: BLE001  (한 종목의 예상 밖 오류가 전체를 멈추지 않게 한다)
                    rep.stocks_failed.append((code, f"예상 밖 오류: {exc!r}"))
        except Exception as exc:  # noqa: BLE001
            finish_run(ctx, run_id, "failed", rep.rows_written, f"중단: {exc!r}")
            raise
        failures = len(rep.index_failed) + len(rep.stocks_failed)
        rep.status = "failed" if failures else "success"
        parts = [f"종목 {rep.stocks_ok}개 완료", f"행 {rep.rows_written}개"]
        if rep.index_failed:
            parts.append("지수 실패 " + ", ".join(s for s, _ in rep.index_failed))
        if rep.stocks_failed:
            shown = ", ".join(c for c, _ in rep.stocks_failed[:10])
            parts.append(f"종목 실패 {len(rep.stocks_failed)}개({shown})")
        if rep.warnings:
            parts.append(f"경고 {len(rep.warnings)}건")
        finish_run(ctx, run_id, rep.status, rep.rows_written, "; ".join(parts))
        ctx.logger(f"갱신 {rep.status}: " + "; ".join(parts))
        return rep


def main() -> None:
    setup_logging()
    rep = run_refresh(build_context())
    print(rep.status, f"종목 {rep.stocks_ok}개, 실패 {len(rep.stocks_failed) + len(rep.index_failed)}건, 행 {rep.rows_written}개")
    for w in rep.warnings[:20]:
        print("경고:", w)


if __name__ == "__main__":
    main()
