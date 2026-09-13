"""compute_next_run: pure schedule arithmetic, no clock patching needed -
every "now" is a parameter. See app/missions/scheduler.py.

Run with:
    python -m unittest tests.test_scheduler -v
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.missions.scheduler import ScheduleKind, compute_next_run  # noqa: E402


class OnceTests(unittest.TestCase):
    def test_a_future_time_is_returned_as_is(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        at = datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.ONCE, now=now, schedule_at=at.isoformat())
        self.assertEqual(result, at)

    def test_a_past_time_returns_none(self) -> None:
        now = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        at = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.ONCE, now=now, schedule_at=at.isoformat())
        self.assertIsNone(result)

    def test_no_schedule_at_returns_none(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertIsNone(compute_next_run(ScheduleKind.ONCE, now=now, schedule_at=None))


class DailyTests(unittest.TestCase):
    def test_later_today_if_the_time_has_not_passed(self) -> None:
        now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.DAILY, now=now, time_of_day="09:00")
        self.assertEqual(result, datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))

    def test_tomorrow_if_the_time_already_passed_today(self) -> None:
        now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.DAILY, now=now, time_of_day="09:00")
        self.assertEqual(result, datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc))

    def test_exactly_now_counts_as_passed(self) -> None:
        now = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.DAILY, now=now, time_of_day="09:00")
        self.assertEqual(result, datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc))


class WeeklyTests(unittest.TestCase):
    def test_the_same_day_later_if_still_ahead(self) -> None:
        # 2026-01-01 is a Thursday (weekday() == 3).
        now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.WEEKLY, now=now, time_of_day="09:00", weekday=3)
        self.assertEqual(result, datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))

    def test_next_week_if_todays_time_already_passed(self) -> None:
        now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.WEEKLY, now=now, time_of_day="09:00", weekday=3)
        self.assertEqual(result, datetime(2026, 1, 8, 9, 0, tzinfo=timezone.utc))

    def test_a_different_weekday_lands_on_the_right_date(self) -> None:
        # Next Monday (weekday 0) after Thursday 2026-01-01 is 2026-01-05.
        now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.WEEKLY, now=now, time_of_day="09:00", weekday=0)
        self.assertEqual(result, datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))


class IntervalTests(unittest.TestCase):
    def test_from_the_last_run_when_recent(self) -> None:
        now = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)
        last_run = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.INTERVAL, now=now, interval_seconds=3600,
                                  last_run_at=last_run)
        self.assertEqual(result, datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))

    def test_from_now_when_there_is_no_last_run(self) -> None:
        now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.INTERVAL, now=now, interval_seconds=3600,
                                  last_run_at=None)
        self.assertEqual(result, now + timedelta(seconds=3600))

    def test_a_stale_last_run_does_not_produce_an_overdue_burst(self) -> None:
        """The app was closed for three days; the next run must still be
        one interval out from *now*, not an instantly-due backlog."""
        now = datetime(2026, 1, 4, 10, 0, tzinfo=timezone.utc)
        last_run = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
        result = compute_next_run(ScheduleKind.INTERVAL, now=now, interval_seconds=3600,
                                  last_run_at=last_run)
        self.assertEqual(result, now + timedelta(seconds=3600))

    def test_zero_or_missing_interval_returns_none(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertIsNone(compute_next_run(ScheduleKind.INTERVAL, now=now, interval_seconds=0))
        self.assertIsNone(compute_next_run(ScheduleKind.INTERVAL, now=now, interval_seconds=None))


if __name__ == "__main__":
    unittest.main()
