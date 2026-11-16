"""사용자가 처음 검색한 종목을 편입하는 작업(검색 요청 흐름의 loading 단계).

같은 종목에 요청이 몰려도 종목별 잠금으로 한 번에 하나씩만 돌고, 뒤의 요청은 앞의 요청이 끝난 뒤
이미 편입된 종목으로 보고 증분 갱신만 한다. 실행 기록(job = onboard:종목코드)을 남겨서 화면이 진행 상태를 확인할 수 있게 한다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from ..collectors.common import CollectorError
from ..services.onboarding import OnboardResult, onboard_stock
from .context import JobContext, build_context, setup_logging
from .locks import claim_run, finish_run, stock_lock


@dataclass
class OnboardJobResult:
    status: str                         # success | failed
    run_id: int | None
    result: OnboardResult | None = None
    error: str | None = None


def run_onboard(ctx: JobContext, code: str, master_row=None) -> OnboardJobResult:
    with stock_lock(code):
        run_id = claim_run(ctx, f"onboard:{code}")   # 비정상 종료로 남은 기록이 막고 있어도 작업은 계속한다(run_id만 없음)
        try:
            with ctx.scope() as session:
                result = onboard_stock(session, code, ctx.calendar, ctx.now(), ctx.settings,
                                       master_row=master_row, fdr=ctx.fdr, sleep=ctx.sleep, db=ctx.db)
        except CollectorError as exc:
            finish_run(ctx, run_id, "failed", message=str(exc))
            return OnboardJobResult("failed", run_id, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            finish_run(ctx, run_id, "failed", message=repr(exc))
            raise
        rows = result.refresh.rows_written if result.refresh else 0
        finish_run(ctx, run_id, "success", rows, f"{result.status}")
        return OnboardJobResult("success", run_id, result)


def main() -> None:
    setup_logging()
    if len(sys.argv) != 2:
        print("사용법: python -m app.jobs.run_onboard 종목코드")
        return
    out = run_onboard(build_context(), sys.argv[1])
    if out.result:
        d = out.result.decision
        print(out.status, out.result.status, "" if d is None else (d.message or "지원 대상"))
    else:
        print(out.status, out.error)


if __name__ == "__main__":
    main()
