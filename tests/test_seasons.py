"""Seasons and carry-over.

A college roster turns over every year, so seasons have to reset the
competition without throwing away what the club has learned about the players
who are still there.
"""

import unittest

from ladder import divisions as div
from tests.helpers import days_ago, play, with_roster

MS = div.MENS_SINGLES
MD = div.MENS_DOUBLES


class TestSeasonBasics(unittest.TestCase):
    def test_a_database_starts_with_one_current_season(self):
        svc, _ = with_roster()
        seasons = svc.db.seasons()
        self.assertEqual(len(seasons), 1)
        self.assertTrue(seasons[0].is_current)

    def test_starting_a_season_closes_the_previous_one(self):
        svc, _ = with_roster()
        first = svc.db.current_season()
        second = svc.start_season("Spring 2027")
        self.assertNotEqual(first.id, second.id)
        self.assertTrue(svc.db.get_season(second.id).is_current)
        self.assertFalse(svc.db.get_season(first.id).is_current)
        self.assertIsNotNone(svc.db.get_season(first.id).ends_on)

    def test_seasons_are_ordered_by_when_they_ran(self):
        """Ordering must not depend on start dates: the bootstrap season's
        date is just the day the database was made, and matches are often
        backdated into it. Getting this wrong silently inverts carry-over."""
        svc, _ = with_roster()
        first = svc.db.current_season()
        svc.db.set_season_start(first.id, days_ago(1))    # later than season 2's
        second = svc.start_season("Later season", days_ago(200))
        self.assertEqual([s.id for s in svc.db.seasons()], [first.id, second.id])

    def test_new_matches_land_in_the_current_season(self):
        svc, p = with_roster()
        play(svc, MS, p["Al"], p["Bo"], 30)
        second = svc.start_season("Season 2")
        play(svc, MS, p["Al"], p["Bo"], 1)
        recent = svc.db.list_matches(limit=1)[0]
        self.assertEqual(recent.season_id, second.id)

    def test_a_season_needs_a_name(self):
        svc, _ = with_roster()
        from ladder.service import ServiceError
        with self.assertRaises(ServiceError):
            svc.start_season("   ")


class TestCarryOver(unittest.TestCase):
    def setUp(self):
        self.svc, self.p = with_roster()
        # Establish a clear order in season one.
        day = 90
        for _ in range(5):
            play(self.svc, MS, self.p["Al"], self.p["Bo"], day)
            play(self.svc, MS, self.p["Bo"], self.p["Cy"], day - 1)
            play(self.svc, MS, self.p["Al"], self.p["Cy"], day - 2)
            day -= 7
        self.first_id = self.svc.db.current_season().id
        self.before = {
            name: self.svc.engine.ladder(MS, self.first_id).entry(self.p[name]).rating
            for name in ("Al", "Bo", "Cy")
        }

    def test_ratings_carry_into_the_new_season(self):
        self.svc.start_season("Season 2")
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 1)
        entry = self.svc.engine.ladder(MS).entry(self.p["Al"])
        self.assertEqual(entry.seeded_from, "last season")
        # Started from last season's number, not from 1500.
        self.assertGreater(entry.history[0].rating, 1550)

    def test_carry_over_widens_uncertainty(self):
        """Where you finished seeds where you start, without freezing it."""
        cfg = self.svc.config
        self.svc.start_season("Season 2")
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 1)
        rd_now = self.svc.engine.ladder(MS).entry(self.p["Al"]).rating.rd
        self.assertGreaterEqual(rd_now, min(self.before["Al"].rd,
                                            cfg.season_carryover_rd) - 1)

    def test_the_previous_order_is_preserved_at_the_start(self):
        self.svc.start_season("Season 2")
        for name in ("Al", "Bo", "Cy"):
            play(self.svc, MS, self.p[name], self.p["Dan"], 2)
        new = self.svc.engine.ladder(MS)
        self.assertLess(new.rank_of(self.p["Al"]), new.rank_of(self.p["Cy"]))

    def test_past_seasons_stay_readable(self):
        self.svc.start_season("Season 2")
        play(self.svc, MS, self.p["Cy"], self.p["Al"], 1)
        old = self.svc.engine.ladder(MS, self.first_id)
        self.assertEqual(old.entry(self.p["Al"]).rating.rating,
                         self.before["Al"].rating)

    def test_carry_over_can_be_switched_off(self):
        svc, p = with_roster(season_carryover=False)
        for day in range(40, 5, -4):
            play(svc, MS, p["Al"], p["Bo"], day, score="6-0 6-0")
        svc.start_season("Clean slate")
        play(svc, MS, p["Al"], p["Bo"], 1)
        self.assertEqual(svc.engine.ladder(MS).entry(p["Al"]).seeded_from, "new")

    def test_a_recruit_joining_mid_way_starts_fresh(self):
        self.svc.start_season("Season 2")
        recruit = self.svc.add_player("Newbie", category=div.MENS)
        play(self.svc, MS, recruit.id, self.p["Dan"], 1)
        entry = self.svc.engine.ladder(MS).entry(recruit.id)
        self.assertEqual(entry.seeded_from, "new")
        self.assertTrue(entry.provisional)

    def test_a_carried_over_player_is_not_provisional(self):
        """They already have a season of evidence behind them."""
        self.svc.start_season("Season 2")
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 1)
        self.assertFalse(self.svc.engine.ladder(MS).entry(self.p["Al"]).provisional)

    def test_graduating_players_keep_their_history(self):
        self.svc.db.set_player_active(self.p["Cy"], False)
        self.svc.engine.invalidate()
        self.assertIsNone(self.svc.engine.ladder(MS, self.first_id).entry(self.p["Cy"]))
        archived = self.svc.engine.ladder(MS, self.first_id, include_inactive=True)
        self.assertIsNotNone(archived.entry(self.p["Cy"]))

    def test_doubles_ratings_carry_over_independently(self):
        for day in range(40, 10, -4):
            play(self.svc, MD, [self.p["Al"], self.p["Bo"]],
                 [self.p["Cy"], self.p["Dan"]], day, score="6-1 6-1")
        self.svc.start_season("Season 2")
        play(self.svc, MD, [self.p["Al"], self.p["Bo"]],
             [self.p["Cy"], self.p["Dan"]], 1)
        entry = self.svc.engine.ladder(MD).entry(self.p["Al"])
        self.assertEqual(entry.seeded_from, "last season")


if __name__ == "__main__":
    unittest.main()
