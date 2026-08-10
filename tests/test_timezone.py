"""The club's timezone.

Every datetime in this app is naive and read from the machine's clock. On the
Oracle server that clock is UTC while the club is on US Eastern, so a suggested
slot rendered as "2:29pm" actually meant 10:29am to the people reading it --
already past, which is exactly how this surfaced: a suggested time that could
not be clicked.

`apply_timezone` moves the whole process instead of converting at each call
site, so there is one place to get right rather than dozens.
"""

import os
import time
import unittest
from datetime import datetime

from ladder.config import Config, apply_timezone


class TimezoneTestCase(unittest.TestCase):
    """Restores the process timezone, since these tests change it globally."""

    def setUp(self):
        self._saved = os.environ.get("TZ")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._saved
        if hasattr(time, "tzset"):
            time.tzset()


class TestApplyTimezone(TimezoneTestCase):
    def test_it_moves_the_process_clock(self):
        apply_timezone(Config(timezone="UTC"))
        utc_hour = datetime.now().hour
        apply_timezone(Config(timezone="America/New_York"))
        eastern_hour = datetime.now().hour
        # Eastern is 4 or 5 hours behind UTC depending on the season.
        self.assertIn((utc_hour - eastern_hour) % 24, (4, 5))

    def test_it_reports_the_zone_in_effect(self):
        self.assertIn(apply_timezone(Config(timezone="America/New_York")),
                      ("EST", "EDT"))
        self.assertEqual(apply_timezone(Config(timezone="UTC")), "UTC")

    def test_an_empty_setting_leaves_the_machine_alone(self):
        apply_timezone(Config(timezone="Asia/Tokyo"))
        before = datetime.now().hour
        apply_timezone(Config(timezone=""))
        self.assertEqual(datetime.now().hour, before)

    def test_daylight_saving_is_handled_not_hardcoded(self):
        """A fixed offset would be an hour wrong for half the year, so the
        system tz database has to be doing the work."""
        apply_timezone(Config(timezone="America/New_York"))
        self.assertEqual(set(time.tzname), {"EST", "EDT"})


class TestTheBugThatWasReported(TimezoneTestCase):
    def test_a_time_shown_as_afternoon_is_actually_afternoon(self):
        """The symptom: at 10:29 Eastern the app offered "2:29pm", meaning
        14:29 UTC -- one minute in the past. With the club's zone applied, the
        clock the app reads is the same one the player is looking at."""
        apply_timezone(Config(timezone="America/New_York"))
        eastern_now = datetime.now()
        os.environ["TZ"] = "UTC"
        if hasattr(time, "tzset"):
            time.tzset()
        utc_now = datetime.now()
        self.assertNotEqual(eastern_now.hour, utc_now.hour)

    def test_suggested_starts_are_rounded_to_a_clean_time(self):
        """"2:29pm" was also a symptom of trimming a window to the current
        minute: not a time anyone arranges, and stale the moment it renders."""
        from datetime import date

        from ladder.availability import SUGGESTION_GRANULARITY, Availability, mutual_slots

        both = Availability(weekly={d: [(8 * 60, 22 * 60)] for d in range(7)},
                            blocked={}, extra={})
        today = date(2026, 8, 10)
        slots = mutual_slots(both, both, minutes=60, start_day=today, days=1,
                             earliest=datetime(2026, 8, 10, 14, 29))
        self.assertTrue(slots)
        self.assertEqual(slots[0].start % SUGGESTION_GRANULARITY, 0)
        self.assertGreaterEqual(slots[0].start, 14 * 60 + 29)

    def test_a_freshly_suggested_slot_can_still_be_requested(self):
        """The race that made every trimmed slot unclickable: the suggestion
        was exactly now, so it was past by the time anyone clicked it."""
        from ladder.scheduling import SchedulingError
        from tests.helpers import with_roster

        apply_timezone(Config(timezone="America/New_York"))
        svc, players = with_roster()
        for pid in (players["Al"], players["Bo"]):
            svc.db.set_weekly_availability(pid, {d: [(0, 24 * 60)] for d in range(7)})

        slots = svc.scheduler.suggest(players["Al"], players["Bo"])
        self.assertTrue(slots, "expected a suggestion for wide-open availability")
        when = slots[0].starts_at.strftime("%Y-%m-%dT%H:%M")
        try:
            request = svc.scheduler.request_match(
                division="mens_singles", from_player=players["Al"],
                to_player=players["Bo"], starts_at=when)
        except SchedulingError as exc:
            self.fail(f"could not request the very slot just suggested: {exc}")
        self.assertEqual(request.status, "pending")


if __name__ == "__main__":
    unittest.main()
