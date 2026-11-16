"""중복 실행 방지.

- job_lock(name)  : 같은 프로그램 안에서 같은 작업이 겹쳐 돌지 않게 한다(스레드 잠금).
- stock_lock(code): 같은 종목에 요청이 몰려도 편입 작업은 한 번에 하나만 돈다.
- claim_run / finish_run: pipeline_runs 기록으로 "실행 중"을 표시하고, 비정상 종료로 남은 오래된 기록은 정리한다.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import timedelta

from ..services.refresh import _db
from .context import JobContext

_guard = threading.Lock()
_job_locks: dict[str, threading.Lock] = {}
_stock_locks: dict[str, threading.Lock] = {}


@contextmanager
def job_lock(name: str):
    """with job_lock('refresh') as acquired: 잠금을 얻지 못하면 acquired가 False(기다리지 않는다)."""
    with _guard:
        lock = _job_locks.setdefault(name, threading.Lock())
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def stock_lock(code: str) -> threading.Lock:
    """with stock_lock('005930'): 같은 종목의 작업은 차례로 하나씩 한다(기다린다)."""
    with _guard:
        return _stock_locks.setdefault(code, threading.Lock())


def claim_run(ctx: JobContext, job: str) -> int | None:
    """실행 기록을 시작한다. 이미 실행 중이면 None. 오래된 running 기록은 failed로 닫는다."""
    db = _db(ctx.db)
    now = ctx.now()
    stale = timedelta(hours=ctx.settings.runtime.stale_run_hours)
    with ctx.scope() as session:
        closed = db.close_stale_runs(session, job, now, stale)
        if closed:
            ctx.logger(f"경고: {job}의 오래된 실행 기록 {closed}건을 비정상 종료로 닫았습니다")
        if db.is_running(session, job, now, stale):
            return None
        return db.start_run(session, job, now)


def finish_run(ctx: JobContext, run_id: int | None, status: str, rows: int | None = None, message: str | None = None) -> None:
    if run_id is None:
        return
    db = _db(ctx.db)
    with ctx.scope() as session:
        db.finish_run(session, run_id, status, processed_rows=rows, message=(message or "")[:2000] or None, now=ctx.now())
