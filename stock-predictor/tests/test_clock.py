import unittest
from datetime import date, datetime, timezone

from app import clock


class ClockTest(unittest.TestCase):
    def tearDown(self):
        clock.reset_now()

    def test_real_clock_is_kst(self):
        self.assertEqual(clock.now_kst().utcoffset().total_seconds(), 9 * 3600)

    def test_frozen_naive_is_treated_as_kst(self):
        with clock.frozen(datetime(2026, 11, 4, 7, 5)):
            self.assertEqual(clock.now_kst().hour, 7)
            self.assertEqual(clock.today_kst(), date(2026, 11, 4))
            self.assertIsNone(clock.now_kst_naive().tzinfo)
            self.assertEqual(clock.now_kst_naive(), datetime(2026, 11, 4, 7, 5))

    def test_frozen_aware_is_converted_to_kst(self):
        # UTC 2026-11-03 22:30 = KST 2026-11-04 07:30
        with clock.frozen(datetime(2026, 11, 3, 22, 30, tzinfo=timezone.utc)):
            self.assertEqual(clock.today_kst(), date(2026, 11, 4))
            self.assertEqual((clock.now_kst().hour, clock.now_kst().minute), (7, 30))

    def test_frozen_restores_previous_value(self):
        clock.set_now(datetime(2026, 1, 1, 9, 0))
        with clock.frozen(datetime(2027, 1, 1, 9, 0)):
            self.assertEqual(clock.today_kst().year, 2027)
        self.assertEqual(clock.today_kst().year, 2026)
        clock.reset_now()
        self.assertGreater(clock.today_kst().year, 2025)


if __name__ == "__main__":
    unittest.main()
