"""현재 시각은 이 파일에서만 얻는다(KST 고정).

PC의 시간대 설정과 상관없이 한국 시간으로 동작하고, 테스트에서는 frozen()으로 시각을 바꿔 쓸 수 있다.
DB에는 시간대 없는 KST 시각(naive)을 저장하므로 now_kst_naive()를 쓴다.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
_override: datetime | None = None


def _to_kst(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def now_kst() -> datetime:
    return _override if _override is not None else datetime.now(KST)


def now_kst_naive() -> datetime:
    return now_kst().replace(tzinfo=None)


def today_kst() -> date:
    return now_kst().date()


def set_now(dt: datetime) -> None:
    global _override
    _override = _to_kst(dt)


def reset_now() -> None:
    global _override
    _override = None


@contextmanager
def frozen(dt: datetime):
    """with frozen(datetime(2026, 11, 4, 7, 5)): ... 안에서는 now_kst()가 이 시각을 돌려준다."""
    global _override
    previous = _override
    _override = _to_kst(dt)
    try:
        yield
    finally:
        _override = previous
