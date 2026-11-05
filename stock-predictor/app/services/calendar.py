"""거래일 달력과 예측 가능 구간 계산. 달력 판정은 이 파일 한 곳에서만 한다.

- 한국 거래일 = 거래소 달력 라이브러리(XKRX)의 거래일 - 설정의 추가 휴장일(config/extra_holidays.csv)
- 미국 거래일 = 거래소 달력 라이브러리(XNYS)의 거래일
- 달력의 지원 범위를 벗어난 날짜를 물으면 CalendarRangeError를 낸다(조용히 틀린 답을 내지 않는다).

예측 가능 구간(대상일 T):
  시작 = max( T보다 앞선 가장 최근 미국 거래일의 다음 날 07:00, 기준일(T 직전 한국 거래일) 16:30 )
  끝   = T 16:30
  구간은 [시작, 끝) 반열린 구간이라 서로 겹치지 않는다. 구간 밖(대기 구간)에는 새 예측을 만들지 않는다.
"""
from __future__ import annotations

import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Protocol

from ..clock import KST
from ..settings import Settings


class CalendarRangeError(RuntimeError):
    """달력이 지원하는 범위를 벗어난 날짜를 물었을 때."""


# ------------------------------------------------------------------ 달력 원천
class SessionSource(Protocol):
    """거래일 목록을 주는 원천. 운영에서는 거래소 달력 라이브러리, 테스트에서는 가짜를 쓴다."""

    def all_sessions(self) -> list[date]: ...


class ExchangeCalendarsSource:
    """exchange_calendars 라이브러리의 거래일을 가져온다(예: 'XKRX', 'XNYS')."""

    def __init__(self, name: str):
        import exchange_calendars as xcals  # 설치되어 있지 않으면 여기서 ImportError

        self._cal = xcals.get_calendar(name)
        self.name = name

    def all_sessions(self) -> list[date]:
        first, last = self._cal.first_session, self._cal.last_session
        sessions = self._cal.sessions_in_range(first.date().isoformat(), last.date().isoformat())
        return [s.date() for s in sessions]


# ------------------------------------------------------------------ 추가 휴장일
def load_extra_holidays(path: str | Path) -> set[date]:
    """config/extra_holidays.csv를 읽는다. 헤더는 date,reason. 빈 줄과 #로 시작하는 줄은 무시한다."""
    p = Path(path)
    if not p.exists():
        return set()
    result: set[date] = set()
    with open(p, encoding="utf-8-sig", newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if reader.fieldnames is None or "date" not in reader.fieldnames:
        raise ValueError(f"{p}: 첫 줄은 date,reason 헤더여야 합니다")
    for n, row in enumerate(reader, start=2):
        raw = (row.get("date") or "").strip()
        try:
            result.add(date.fromisoformat(raw))
        except ValueError as exc:
            raise ValueError(f"{p}: {n}번째 줄의 날짜가 YYYY-MM-DD 형식이 아닙니다: {raw!r}") from exc
    return result


# ------------------------------------------------------------------ 결과 자료형
@dataclass(frozen=True)
class PredictionWindow:
    target_date: date   # 예측 대상일 T
    base_date: date     # 기준일 D (T 직전 한국 거래일)
    us_date: date       # T보다 앞선 가장 최근 미국 거래일
    start: datetime     # 구간 시작(KST, 시간대 없음)
    end: datetime       # 구간 끝(KST, 시간대 없음)

    def contains(self, now: datetime) -> bool:
        return self.start <= now < self.end


@dataclass(frozen=True)
class PredictionState:
    now: datetime
    open_window: PredictionWindow | None   # 지금 열려 있는 구간(없으면 대기 구간)
    last_window: PredictionWindow | None   # 가장 최근에 끝난 구간(읽기 전용으로 보여 줄 예측의 대상)
    next_window: PredictionWindow | None   # 다음에 열릴 구간

    @property
    def is_open(self) -> bool:
        return self.open_window is not None

    @property
    def next_open_at(self) -> datetime | None:
        return self.next_window.start if self.next_window else None


@dataclass(frozen=True)
class CalendarMismatch:
    only_in_index: list[date]      # 지수에는 있는데 달력에는 거래일이 아닌 날
    only_in_calendar: list[date]   # 달력상 거래일인데 지수에는 행이 없는 날(휴장일 누락 의심)

    @property
    def ok(self) -> bool:
        return not self.only_in_index and not self.only_in_calendar


def _naive_kst(now: datetime) -> datetime:
    """시간대가 있으면 KST로 바꾸고, 없으면 KST로 본다. 비교용으로 시간대는 떼어 낸다."""
    if now.tzinfo is not None:
        return now.astimezone(KST).replace(tzinfo=None)
    return now


# ------------------------------------------------------------------ 달력
class TradingCalendar:
    def __init__(
        self,
        kr: SessionSource,
        us: SessionSource,
        extra_holidays: Iterable[date] = (),
        close_confirm_time: time = time(16, 30),
        prediction_open_time: time = time(7, 0),
    ):
        self._kr_all = sorted(set(kr.all_sessions()))
        self._us_all = sorted(set(us.all_sessions()))
        if not self._kr_all or not self._us_all:
            raise ValueError("달력에 거래일이 하나도 없습니다")
        self.extra_holidays = frozenset(extra_holidays)
        extra = self.extra_holidays
        self._kr = [d for d in self._kr_all if d not in extra]
        self._kr_set = set(self._kr)
        self._us = self._us_all
        self._us_set = set(self._us)
        self.close_confirm_time = close_confirm_time
        self.prediction_open_time = prediction_open_time

    # ---- 범위 확인
    @property
    def kr_range(self) -> tuple[date, date]:
        return self._kr_all[0], self._kr_all[-1]

    @property
    def us_range(self) -> tuple[date, date]:
        return self._us[0], self._us[-1]

    def _check_kr(self, d: date) -> None:
        lo, hi = self.kr_range
        if not lo <= d <= hi:
            raise CalendarRangeError(
                f"한국 달력 지원 범위({lo} ~ {hi})를 벗어난 날짜입니다: {d}. exchange_calendars를 업데이트하세요"
            )

    def _check_us(self, d: date) -> None:
        lo, hi = self.us_range
        if not lo <= d <= hi:
            raise CalendarRangeError(f"미국 달력 지원 범위({lo} ~ {hi})를 벗어난 날짜입니다: {d}")

    # ---- 한국 거래일
    def is_trading_day(self, d: date) -> bool:
        self._check_kr(d)
        return d in self._kr_set

    def next_trading_day(self, d: date) -> date:
        """d 다음(d 제외) 한국 거래일."""
        self._check_kr(d)
        i = bisect_right(self._kr, d)
        if i >= len(self._kr):
            raise CalendarRangeError(f"{d} 이후의 거래일이 달력 범위 안에 없습니다")
        return self._kr[i]

    def previous_trading_day(self, d: date) -> date:
        """d 이전(d 제외) 한국 거래일."""
        self._check_kr(d)
        i = bisect_left(self._kr, d) - 1
        if i < 0:
            raise CalendarRangeError(f"{d} 이전의 거래일이 달력 범위 안에 없습니다")
        return self._kr[i]

    def latest_base_date(self, now: datetime) -> date:
        """기준일 D = 마지막 확정 종가일.

        오늘이 거래일이고 확정 시각(16:30) 이후면 오늘, 아니면 오늘 이전의 가장 최근 거래일이다.
        """
        now = _naive_kst(now)
        today = now.date()
        if self.is_trading_day(today) and now.time() >= self.close_confirm_time:
            return today
        return self.previous_trading_day(today)

    def start_date_for_history(self, base_date: date, n: int) -> date:
        """base_date까지 n개의 거래일을 담는 시작일(base_date 이하의 거래일 중 n번째로 최근)."""
        if n < 1:
            raise ValueError("n은 1 이상이어야 합니다")
        self._check_kr(base_date)
        i = bisect_right(self._kr, base_date)
        if i < n:
            raise CalendarRangeError(f"{base_date}까지 거래일이 {i}개뿐이라 {n}거래일 전을 계산할 수 없습니다")
        return self._kr[i - n]

    # ---- 미국 거래일
    def latest_us_session_before(self, target: date) -> date:
        """target(한국 날짜)보다 앞선(target 제외) 가장 최근 미국 거래일."""
        self._check_us(target)
        i = bisect_left(self._us, target) - 1
        if i < 0:
            raise CalendarRangeError(f"{target} 이전의 미국 거래일이 달력 범위 안에 없습니다")
        return self._us[i]

    # ---- 예측 가능 구간
    def prediction_window(self, target: date) -> PredictionWindow:
        if not self.is_trading_day(target):
            raise ValueError(f"예측 대상일은 한국 거래일이어야 합니다: {target}")
        base = self.previous_trading_day(target)
        us_date = self.latest_us_session_before(target)
        us_ready = datetime.combine(us_date + timedelta(days=1), self.prediction_open_time)
        base_ready = datetime.combine(base, self.close_confirm_time)
        start = max(us_ready, base_ready)
        end = datetime.combine(target, self.close_confirm_time)
        if start >= end:
            raise RuntimeError(f"예측 구간이 비어 있습니다: {target} ({start} ~ {end})")
        return PredictionWindow(target, base, us_date, start, end)

    def prediction_state(self, now: datetime) -> PredictionState:
        """지금 시각의 예측 상태: 열린 구간, 가장 최근에 끝난 구간, 다음에 열릴 구간."""
        now = _naive_kst(now)
        today = now.date()
        self._check_kr(today)
        lo = bisect_left(self._kr, today - timedelta(days=8))
        hi = bisect_right(self._kr, today + timedelta(days=8))
        windows = [self.prediction_window(t) for t in self._kr[lo:hi]]
        open_w = next((w for w in windows if w.contains(now)), None)
        ended = [w for w in windows if w.end <= now]
        upcoming = [w for w in windows if w.start > now]
        last_w = max(ended, key=lambda w: w.end) if ended else None
        next_w = min(upcoming, key=lambda w: w.start) if upcoming else None
        return PredictionState(now, open_w, last_w, next_w)

    # ---- 지수 데이터와의 비교
    def compare_with_index(self, index_dates: Iterable[date], start: date, end: date) -> CalendarMismatch:
        """[start, end] 구간에서 지수 날짜와 달력의 거래일을 비교한다. 어긋나면 경고 대상이다."""
        idx = {d for d in index_dates if start <= d <= end}
        cal = {d for d in self._kr if start <= d <= end}
        return CalendarMismatch(sorted(idx - cal), sorted(cal - idx))


# ------------------------------------------------------------------ 운영용 생성
def make_calendar(settings: Settings) -> TradingCalendar:
    """설정으로 운영용 달력을 만든다(exchange_calendars 필요)."""
    extra = load_extra_holidays(settings.resolve(settings.paths.extra_holidays))
    return TradingCalendar(
        kr=ExchangeCalendarsSource("XKRX"),
        us=ExchangeCalendarsSource("XNYS"),
        extra_holidays=extra,
        close_confirm_time=settings.schedule.close_confirm_time,
        prediction_open_time=settings.schedule.prediction_open_time,
    )
