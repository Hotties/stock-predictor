"""작업이 쓰는 도구 묶음. 운영에서는 build_context()로 만들고, 테스트에서는 가짜를 끼운다."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, ContextManager

from .. import clock
from ..services.calendar import TradingCalendar, make_calendar
from ..settings import Settings, load_settings

log = logging.getLogger("stock")


@dataclass
class JobContext:
    settings: Settings
    calendar: TradingCalendar
    scope: Callable[[], ContextManager]          # with ctx.scope() as session: ... (끝나면 커밋)
    db: Any = None                               # None이면 app.db.repo
    fdr: Any = None                              # None이면 실제 FinanceDataReader
    sleep: Callable[[float], None] = time.sleep
    now_fn: Callable[[], datetime] = clock.now_kst_naive
    logger: Callable[[str], None] = log.info

    def now(self) -> datetime:
        return self.now_fn()


def build_context(settings: Settings | None = None) -> JobContext:
    """설정으로 운영용 도구 묶음을 만든다(MariaDB 연결, 거래소 달력)."""
    from ..db.connection import make_engine, make_session_factory, session_scope

    settings = settings or load_settings()
    factory = make_session_factory(make_engine(settings.database.url()))
    return JobContext(settings=settings, calendar=make_calendar(settings), scope=lambda: session_scope(factory))


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
