"""Match requests: proposing a time, answering, and suggested slots."""

import unittest
from datetime import date, datetime, timedelta

from ladder import divisions as div
from ladder.scheduling import SchedulingError
from ladder.storage import (
    REQUEST_ACCEPTED, REQUEST_CANCELLED, REQUEST_DECLINED, REQUEST_PLAYED,
)
from tests.helpers import play, with_roster

MS = div.MENS_SINGLES


def at(hour, minute=0):
    return hour * 60 + minute


def next_weekday(weekday: int) -> date:
    """The next occurrence of a weekday, always in the future."""
    today = date.today()
    ahead = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=ahead)


class SchedulingTestCase(unittest.TestCase):
    def setUp(self):
        self.svc, self.p = with_roster()
        self.sched = self.svc.scheduler
        # Al and Bo overlap on Tuesdays 4-6pm; Cy is free but never with them.
        self.svc.db.set_weekly_availability(self.p["Al"], {1: [(at(15), at(18))]})
        self.svc.db.set_weekly_availability(self.p["Bo"], {1: [(at(16), at(20))]})
        self.svc.db.set_weekly_availability(self.p["Cy"], {5: [(at(9), at(11))]})

    def soon(self, hours=48):
        return (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


class TestSuggestions(SchedulingTestCase):
    def test_it_finds_the_overlap(self):
        slots = self.sched.suggest(self.p["Al"], self.p["Bo"])
        self.assertTrue(slots)
        self.assertEqual((slots[0].start, slots[0].end), (at(16), at(18)))

    def test_a_longer_format_needs_a_longer_gap(self):
        """The overlap is two hours, so best-of-three (2h) still fits but a
        format needing more would not."""
        self.assertTrue(self.sched.suggest(self.p["Al"], self.p["Bo"],
                                           match_format="best_of_three"))
        self.svc.config.match_formats["marathon"] = {
            "label": "Marathon", "minutes": 180}
        self.assertEqual(self.sched.suggest(self.p["Al"], self.p["Bo"],
                                            match_format="marathon"), [])

    def test_no_overlap_gives_no_suggestions(self):
        self.assertEqual(self.sched.suggest(self.p["Al"], self.p["Cy"]), [])

    def test_a_player_with_no_availability_gives_no_suggestions(self):
        self.assertEqual(self.sched.suggest(self.p["Al"], self.p["Dan"]), [])

    def test_has_availability_reports_who_has_filled_it_in(self):
        self.assertTrue(self.sched.has_availability(self.p["Al"]))
        self.assertFalse(self.sched.has_availability(self.p["Dan"]))

    def test_blocking_a_day_removes_only_that_suggestion(self):
        first = self.sched.suggest(self.p["Al"], self.p["Bo"])[0]
        self.svc.db.add_exception(self.p["Al"], first.on.isoformat(), 0, 1440)
        after = self.sched.suggest(self.p["Al"], self.p["Bo"])
        self.assertTrue(after)
        self.assertNotEqual(after[0].on, first.on)

    def test_format_minutes_falls_back_for_an_unknown_format(self):
        self.assertEqual(self.sched.format_minutes("nonsense"),
                         self.sched.format_minutes(
                             self.svc.config.default_match_format))


class TestRequests(SchedulingTestCase):
    def test_a_request_starts_pending(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon(), message="courts free?")
        self.assertEqual(request.status, "pending")
        self.assertEqual(request.message, "courts free?")
        self.assertEqual(request.minutes,
                         self.sched.format_minutes("one_set"))

    def test_the_opponent_sees_it_in_their_inbox(self):
        self.sched.request_match(division=MS, from_player=self.p["Al"],
                                 to_player=self.p["Bo"], starts_at=self.soon())
        self.assertEqual(len(self.sched.inbox(self.p["Bo"])), 1)
        self.assertEqual(self.sched.inbox(self.p["Al"]), [])
        self.assertEqual(len(self.sched.outbox(self.p["Al"])), 1)

    def test_accepting_schedules_it(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon())
        answered = self.sched.respond(request.id, self.p["Bo"], True)
        self.assertEqual(answered.status, REQUEST_ACCEPTED)
        self.assertEqual(len(self.sched.scheduled(self.p["Al"])), 1)

    def test_declining_closes_it(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon())
        self.assertEqual(self.sched.respond(request.id, self.p["Bo"], False).status,
                         REQUEST_DECLINED)
        self.assertEqual(self.sched.scheduled(self.p["Al"]), [])

    def test_only_the_person_asked_can_answer(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon())
        for impostor in ("Al", "Cy"):
            with self.assertRaises(SchedulingError, msg=impostor):
                self.sched.respond(request.id, self.p[impostor], True)

    def test_answering_twice_is_refused(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon())
        self.sched.respond(request.id, self.p["Bo"], True)
        with self.assertRaises(SchedulingError):
            self.sched.respond(request.id, self.p["Bo"], False)

    def test_accepting_one_time_cancels_the_others_with_that_player(self):
        """Otherwise you agree a time and still have two stale asks pending."""
        first = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon(48))
        second = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon(72))
        self.sched.respond(first.id, self.p["Bo"], True)
        self.assertEqual(self.svc.db.get_match_request(second.id).status,
                         REQUEST_CANCELLED)

    def test_either_player_can_cancel(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon())
        self.sched.cancel(request.id, self.p["Bo"])
        self.assertEqual(self.svc.db.get_match_request(request.id).status,
                         REQUEST_CANCELLED)

    def test_an_outsider_cannot_cancel(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon())
        with self.assertRaises(SchedulingError):
            self.sched.cancel(request.id, self.p["Cy"])

    def test_a_time_in_the_past_is_refused(self):
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
        with self.assertRaises(SchedulingError) as ctx:
            self.sched.request_match(division=MS, from_player=self.p["Al"],
                                     to_player=self.p["Bo"], starts_at=past)
        self.assertIn("passed", str(ctx.exception))

    def test_you_cannot_ask_yourself(self):
        with self.assertRaises(SchedulingError):
            self.sched.request_match(division=MS, from_player=self.p["Al"],
                                     to_player=self.p["Al"],
                                     starts_at=self.soon())

    def test_a_malformed_time_is_refused(self):
        with self.assertRaises(SchedulingError):
            self.sched.request_match(division=MS, from_player=self.p["Al"],
                                     to_player=self.p["Bo"],
                                     starts_at="next tuesday-ish")

    def test_requests_are_capped_so_nobody_gets_spammed(self):
        for hours in (24, 48, 72):
            self.sched.request_match(division=MS, from_player=self.p["Al"],
                                     to_player=self.p["Bo"],
                                     starts_at=self.soon(hours))
        with self.assertRaises(SchedulingError) as ctx:
            self.sched.request_match(division=MS, from_player=self.p["Al"],
                                     to_player=self.p["Bo"],
                                     starts_at=self.soon(96))
        self.assertIn("three pending", str(ctx.exception))

    def test_playing_the_match_closes_the_agreed_request(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon())
        self.sched.respond(request.id, self.p["Bo"], True)
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 0, score="6-4")
        self.assertEqual(self.svc.db.get_match_request(request.id).status,
                         REQUEST_PLAYED)

    def test_an_unrelated_result_leaves_the_request_alone(self):
        request = self.sched.request_match(
            division=MS, from_player=self.p["Al"], to_player=self.p["Bo"],
            starts_at=self.soon())
        self.sched.respond(request.id, self.p["Bo"], True)
        play(self.svc, MS, self.p["Cy"], self.p["Dan"], 0, score="6-4")
        self.assertEqual(self.svc.db.get_match_request(request.id).status,
                         REQUEST_ACCEPTED)


if __name__ == "__main__":
    unittest.main()
