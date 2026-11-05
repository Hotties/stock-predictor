"""달력 테스트. 가짜 달력(평일 - 지정한 휴장일)으로 계산 로직을 확인한다.

2026-11-04는 수요일이다. 가짜 달력의 휴장일:
  한국: 2026-10-09(금, 한글날)
  미국: 2026-11-04(수) <- 미국 휴장일이 낀 경우를 시험하려고 일부러 넣음
추가 휴장일(extra): 2026-06-03(수)은 가짜 한국 달력에서는 거래일이지만 추가 휴장일로 빠진다.
"""
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone

from app.services.calendar import (
    CalendarRangeError, TradingCalendar, load_extra_holidays,
)
from app.settings import PROJECT_ROOT, load_settings

try:
    import exchange_calendars  # noqa: F401
    HAVE_XCALS = True
except ImportError:
    HAVE_XCALS = False


class FakeSource:
    def __init__(self, holidays=()):
        self.holidays = set(holidays)

    def all_sessions(self):
        d, out = date(2020, 1, 1), []
        while d <= date(2030, 12, 31):
            if d.weekday() < 5 and d not in self.holidays:
                out.append(d)
            d += timedelta(days=1)
        return out


def make():
    return TradingCalendar(
        kr=FakeSource([date(2026, 10, 9)]),
        us=FakeSource([date(2026, 11, 4)]),
        extra_holidays={date(2026, 6, 3)},
    )


def dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm)


class KoreanTradingDayTest(unittest.TestCase):
    def setUp(self):
        self.cal = make()

    def test_is_trading_day(self):
        self.assertTrue(self.cal.is_trading_day(date(2026, 11, 4)))
        self.assertFalse(self.cal.is_trading_day(date(2026, 10, 31)))   # 토요일
        self.assertFalse(self.cal.is_trading_day(date(2026, 10, 9)))    # 달력의 휴장일
        self.assertFalse(self.cal.is_trading_day(date(2026, 6, 3)))     # 추가 휴장일
        self.assertTrue(self.cal.is_trading_day(date(2026, 6, 2)))

    def test_next_and_previous(self):
        c = self.cal
        self.assertEqual(c.next_trading_day(date(2026, 10, 30)), date(2026, 11, 2))   # 금 -> 월
        self.assertEqual(c.next_trading_day(date(2026, 10, 31)), date(2026, 11, 2))   # 토 -> 월
        self.assertEqual(c.next_trading_day(date(2026, 10, 8)), date(2026, 10, 12))   # 목 -> (금 휴장) -> 월
        self.assertEqual(c.next_trading_day(date(2026, 6, 2)), date(2026, 6, 4))      # 추가 휴장일 건너뜀
        self.assertEqual(c.previous_trading_day(date(2026, 11, 2)), date(2026, 10, 30))
        self.assertEqual(c.previous_trading_day(date(2026, 10, 31)), date(2026, 10, 30))
        self.assertEqual(c.previous_trading_day(date(2026, 10, 12)), date(2026, 10, 8))
        self.assertEqual(c.previous_trading_day(date(2026, 11, 4)), date(2026, 11, 3))

    def test_latest_base_date(self):
        c = self.cal
        self.assertEqual(c.latest_base_date(dt(2026, 11, 4, 15, 0)), date(2026, 11, 3))
        self.assertEqual(c.latest_base_date(dt(2026, 11, 4, 16, 29)), date(2026, 11, 3))
        self.assertEqual(c.latest_base_date(dt(2026, 11, 4, 16, 30)), date(2026, 11, 4))   # 확정 시각 포함
        self.assertEqual(c.latest_base_date(dt(2026, 11, 4, 23, 59)), date(2026, 11, 4))
        self.assertEqual(c.latest_base_date(dt(2026, 10, 31, 12, 0)), date(2026, 10, 30))  # 토요일
        self.assertEqual(c.latest_base_date(dt(2026, 11, 2, 8, 0)), date(2026, 10, 30))    # 월요일 아침
        self.assertEqual(c.latest_base_date(dt(2026, 10, 12, 9, 0)), date(2026, 10, 8))    # 휴장일 다음 월요일
        self.assertEqual(c.latest_base_date(dt(2026, 10, 9, 20, 0)), date(2026, 10, 8))    # 휴장일 당일 저녁

    def test_latest_base_date_accepts_timezone(self):
        c = self.cal
        utc = timezone.utc
        # UTC 07:00 = KST 16:00 (확정 전), UTC 08:00 = KST 17:00 (확정 후)
        self.assertEqual(c.latest_base_date(datetime(2026, 11, 4, 7, 0, tzinfo=utc)), date(2026, 11, 3))
        self.assertEqual(c.latest_base_date(datetime(2026, 11, 4, 8, 0, tzinfo=utc)), date(2026, 11, 4))

    def test_start_date_for_history(self):
        c = self.cal
        base = date(2026, 11, 4)
        self.assertEqual(c.start_date_for_history(base, 1), base)
        self.assertEqual(c.start_date_for_history(base, 2), date(2026, 11, 3))
        for n in (5, 60, 870):
            start = c.start_date_for_history(base, n)
            days = [d for d in (start + timedelta(days=i) for i in range((base - start).days + 1)) if c.is_trading_day(d)]
            self.assertEqual(len(days), n)          # 시작일~기준일에 거래일이 정확히 n개
            self.assertTrue(c.is_trading_day(start))
        with self.assertRaises(ValueError):
            c.start_date_for_history(base, 0)
        with self.assertRaises(CalendarRangeError):
            c.start_date_for_history(date(2020, 1, 10), 100)   # 달력 앞부분이 모자람

    def test_range_errors(self):
        c = self.cal
        with self.assertRaises(CalendarRangeError):
            c.is_trading_day(date(2019, 6, 1))
        with self.assertRaises(CalendarRangeError):
            c.next_trading_day(date(2031, 1, 1))
        with self.assertRaises(CalendarRangeError):
            c.latest_us_session_before(date(2031, 1, 1))


class UsSessionTest(unittest.TestCase):
    def setUp(self):
        self.cal = make()

    def test_latest_us_session_before(self):
        c = self.cal
        self.assertEqual(c.latest_us_session_before(date(2026, 11, 2)), date(2026, 10, 30))   # 월 -> 직전 금
        self.assertEqual(c.latest_us_session_before(date(2026, 11, 3)), date(2026, 11, 2))
        self.assertEqual(c.latest_us_session_before(date(2026, 11, 4)), date(2026, 11, 3))
        self.assertEqual(c.latest_us_session_before(date(2026, 11, 5)), date(2026, 11, 3))   # 11/4 미국 휴장 -> 화
        self.assertEqual(c.latest_us_session_before(date(2026, 10, 31)), date(2026, 10, 30))  # 토요일 날짜도 가능


class PredictionWindowTest(unittest.TestCase):
    def setUp(self):
        self.cal = make()

    def test_normal_weekday(self):
        w = self.cal.prediction_window(date(2026, 11, 4))   # 수요일
        self.assertEqual((w.base_date, w.us_date), (date(2026, 11, 3), date(2026, 11, 3)))
        self.assertEqual((w.start, w.end), (dt(2026, 11, 4, 7, 0), dt(2026, 11, 4, 16, 30)))

    def test_monday_opens_on_saturday(self):
        w = self.cal.prediction_window(date(2026, 11, 2))
        self.assertEqual((w.base_date, w.us_date), (date(2026, 10, 30), date(2026, 10, 30)))
        self.assertEqual((w.start, w.end), (dt(2026, 10, 31, 7, 0), dt(2026, 11, 2, 16, 30)))

    def test_us_holiday_pushes_start_to_base_close(self):
        # 11/4(수) 미국 휴장 -> 11/5(목) 예측은 미국 정보가 화요일 것이고, 기준일(수) 종가 확정 16:30에 열린다
        w = self.cal.prediction_window(date(2026, 11, 5))
        self.assertEqual((w.base_date, w.us_date), (date(2026, 11, 4), date(2026, 11, 3)))
        self.assertEqual(w.start, dt(2026, 11, 4, 16, 30))
        self.assertEqual(w.end, dt(2026, 11, 5, 16, 30))

    def test_after_korean_holiday(self):
        w = self.cal.prediction_window(date(2026, 10, 12))   # 금(10/9) 휴장 뒤 월요일
        self.assertEqual((w.base_date, w.us_date), (date(2026, 10, 8), date(2026, 10, 9)))
        self.assertEqual(w.start, dt(2026, 10, 10, 7, 0))

    def test_target_must_be_trading_day(self):
        with self.assertRaises(ValueError):
            self.cal.prediction_window(date(2026, 10, 31))

    def test_window_is_half_open(self):
        w = self.cal.prediction_window(date(2026, 11, 4))
        self.assertFalse(w.contains(dt(2026, 11, 4, 6, 59)))
        self.assertTrue(w.contains(dt(2026, 11, 4, 7, 0)))        # 시작 포함
        self.assertTrue(w.contains(dt(2026, 11, 4, 16, 29)))
        self.assertFalse(w.contains(dt(2026, 11, 4, 16, 30)))     # 끝 제외


class PredictionStateTest(unittest.TestCase):
    def setUp(self):
        self.cal = make()

    def test_open_during_the_day(self):
        s = self.cal.prediction_state(dt(2026, 11, 4, 10, 0))
        self.assertTrue(s.is_open)
        self.assertEqual(s.open_window.target_date, date(2026, 11, 4))
        self.assertEqual(s.last_window.target_date, date(2026, 11, 3))   # 가장 최근에 끝난 구간 = 화요일 예측
        self.assertEqual(s.next_window.target_date, date(2026, 11, 5))

    def test_waiting_evening(self):
        s = self.cal.prediction_state(dt(2026, 11, 3, 17, 0))   # 화 저녁
        self.assertFalse(s.is_open)
        self.assertEqual(s.last_window.target_date, date(2026, 11, 3))
        self.assertEqual(s.next_open_at, dt(2026, 11, 4, 7, 0))

    def test_waiting_before_open(self):
        s = self.cal.prediction_state(dt(2026, 11, 4, 6, 0))    # 수 새벽(미국 마감 전)
        self.assertFalse(s.is_open)
        self.assertEqual(s.last_window.target_date, date(2026, 11, 3))
        self.assertEqual(s.next_open_at, dt(2026, 11, 4, 7, 0))

    def test_friday_evening_then_saturday_opens_monday(self):
        s = self.cal.prediction_state(dt(2026, 10, 30, 17, 0))
        self.assertFalse(s.is_open)
        self.assertEqual(s.next_open_at, dt(2026, 10, 31, 7, 0))
        s = self.cal.prediction_state(dt(2026, 10, 31, 8, 0))
        self.assertTrue(s.is_open)
        self.assertEqual(s.open_window.target_date, date(2026, 11, 2))
        self.assertEqual(s.last_window.target_date, date(2026, 10, 30))
        s = self.cal.prediction_state(dt(2026, 11, 1, 23, 0))   # 일요일 밤도 열려 있음
        self.assertEqual(s.open_window.target_date, date(2026, 11, 2))

    def test_us_holiday_windows_touch_but_do_not_overlap(self):
        s = self.cal.prediction_state(dt(2026, 11, 4, 16, 29))
        self.assertEqual(s.open_window.target_date, date(2026, 11, 4))
        s = self.cal.prediction_state(dt(2026, 11, 4, 16, 30))   # 같은 시각에 수요일 구간은 끝나고 목요일 구간이 열린다
        self.assertEqual(s.open_window.target_date, date(2026, 11, 5))

    def test_at_most_one_window_open_and_state_is_consistent(self):
        c = self.cal
        targets = []
        d = date(2026, 9, 1)
        while d < date(2027, 1, 31):
            if c.is_trading_day(d):
                targets.append(c.prediction_window(d))
            d += timedelta(days=1)
        for a, b in zip(targets, targets[1:]):
            self.assertLessEqual(a.end, b.start, f"구간이 겹침: {a.target_date} / {b.target_date}")
        now = dt(2026, 10, 1, 0, 0)
        while now < dt(2026, 12, 31, 0, 0):
            containing = [w for w in targets if w.contains(now)]
            self.assertLessEqual(len(containing), 1)
            s = c.prediction_state(now)
            self.assertEqual(s.open_window, containing[0] if containing else None, str(now))
            if s.last_window:
                self.assertLessEqual(s.last_window.end, now)
            if s.next_window:
                self.assertGreater(s.next_window.start, now)
            now += timedelta(minutes=30)


class CompareWithIndexTest(unittest.TestCase):
    def test_mismatch_detection(self):
        c = make()
        start, end = date(2026, 10, 26), date(2026, 11, 5)
        index = [d for d in (start + timedelta(days=i) for i in range((end - start).days + 1)) if c.is_trading_day(d)]
        self.assertTrue(c.compare_with_index(index, start, end).ok)
        index.remove(date(2026, 11, 3))        # 달력상 거래일인데 지수 행이 없음(휴장일 누락 의심)
        index.append(date(2026, 10, 31))       # 달력에 없는 날(토요일)에 지수 행이 있음
        m = c.compare_with_index(index, start, end)
        self.assertFalse(m.ok)
        self.assertEqual(m.only_in_calendar, [date(2026, 11, 3)])
        self.assertEqual(m.only_in_index, [date(2026, 10, 31)])


class ExtraHolidaysFileTest(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8-sig", newline="")
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_parse_with_comments_and_blank_lines(self):
        path = self.write("date,reason\n\n# 주석\n2026-06-03,지방선거\n2026-07-17,제헌절\n")
        self.assertEqual(load_extra_holidays(path), {date(2026, 6, 3), date(2026, 7, 17)})

    def test_missing_file_is_empty(self):
        self.assertEqual(load_extra_holidays("/no/such/file.csv"), set())

    def test_bad_date_and_bad_header(self):
        with self.assertRaises(ValueError):
            load_extra_holidays(self.write("date,reason\n2026/06/03,x\n"))
        with self.assertRaises(ValueError):
            load_extra_holidays(self.write("when,why\n2026-06-03,x\n"))

    def test_repo_config_file(self):
        got = load_extra_holidays(PROJECT_ROOT / "config" / "extra_holidays.csv")
        self.assertIn(date(2026, 6, 3), got)
        self.assertIn(date(2026, 7, 17), got)


@unittest.skipUnless(HAVE_XCALS, "exchange_calendars가 설치되어 있지 않아 건너뜀")
class RealLibraryTest(unittest.TestCase):
    """실제 라이브러리 연결 확인. M0 점검(2026-10-27)에서 알게 된 사실을 확인한다."""

    @classmethod
    def setUpClass(cls):
        from app.services.calendar import make_calendar
        cls.cal = make_calendar(load_settings(environ={}))

    def test_korean_holidays(self):
        c = self.cal
        self.assertFalse(c.is_trading_day(date(2026, 10, 9)))    # 한글날
        self.assertFalse(c.is_trading_day(date(2026, 6, 3)))     # 지방선거(라이브러리에 없어도 추가 휴장일로 빠짐)
        self.assertFalse(c.is_trading_day(date(2026, 7, 17)))    # 제헌절
        self.assertFalse(c.is_trading_day(date(2026, 12, 31)))   # 연말 휴장
        self.assertTrue(c.is_trading_day(date(2026, 11, 4)))

    def test_us_thanksgiving(self):
        self.assertEqual(self.cal.latest_us_session_before(date(2026, 11, 27)), date(2026, 11, 25))

    def test_window_for_next_wednesday(self):
        w = self.cal.prediction_window(date(2026, 11, 4))
        self.assertEqual(w.start, dt(2026, 11, 4, 7, 0))
        self.assertEqual(w.end, dt(2026, 11, 4, 16, 30))

    def test_history_start_is_870_days_back(self):
        start = self.cal.start_date_for_history(date(2026, 10, 28), 870)
        self.assertLess(start, date(2023, 4, 1))   # M0 점검: 1,300 달력일 전은 865거래일 -> 870거래일은 더 이전


if __name__ == "__main__":
    unittest.main()
