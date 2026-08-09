"""Submission rules, the replay, divisions, and partner chemistry."""

import unittest
from datetime import date, timedelta

from ladder import divisions as div
from ladder.service import ServiceError
from ladder.storage import CONFIRMED, PENDING, REJECTED
from tests.helpers import days_ago, fresh, play, with_roster

MS, WS = div.MENS_SINGLES, div.WOMENS_SINGLES
MD, XD = div.MENS_DOUBLES, div.MIXED_DOUBLES


class TestSubmission(unittest.TestCase):
    def setUp(self):
        self.svc, self.p = with_roster()

    def submit(self, **kwargs):
        base = dict(division=MS, side_a=[self.p["Al"]], side_b=[self.p["Bo"]],
                    score_text="6-4 6-4", played_on=days_ago(1),
                    submitted_by=self.p["Al"])
        base.update(kwargs)
        return self.svc.submit_result(**base)

    def test_a_result_is_pending_until_the_opponent_confirms(self):
        sub = self.submit()
        self.assertEqual(sub.status, PENDING)
        self.assertEqual(self.svc.engine.ladder(MS).entries, [])

        self.svc.confirm(sub.match_id, self.p["Bo"])
        self.assertEqual(self.svc.db.get_match(sub.match_id).status, CONFIRMED)
        self.assertEqual(self.svc.engine.ladder(MS).entry(self.p["Al"]).played, 1)

    def test_you_cannot_confirm_your_own_submission(self):
        sub = self.submit()
        with self.assertRaises(ServiceError):
            self.svc.confirm(sub.match_id, self.p["Al"])

    def test_an_outsider_cannot_confirm(self):
        sub = self.submit()
        with self.assertRaises(ServiceError):
            self.svc.confirm(sub.match_id, self.p["Cy"])

    def test_admin_can_confirm_anything(self):
        sub = self.submit()
        self.svc.confirm(sub.match_id, None, is_admin=True)
        self.assertEqual(self.svc.db.get_match(sub.match_id).status, CONFIRMED)

    def test_a_disputed_result_never_counts(self):
        sub = self.submit(score_text="6-0 6-0")
        self.svc.reject(sub.match_id, self.p["Bo"])
        self.assertEqual(self.svc.db.get_match(sub.match_id).status, REJECTED)
        self.assertEqual(self.svc.engine.ladder(MS).entries, [])

    def test_a_score_contradicting_the_stated_winner_is_refused(self):
        with self.assertRaises(ServiceError) as ctx:
            self.submit(winner_side="b")
        self.assertIn("Al", str(ctx.exception))

    def test_future_dates_are_refused(self):
        with self.assertRaises(ServiceError):
            self.submit(played_on=(date.today() + timedelta(days=1)).isoformat())

    def test_a_player_cannot_play_themselves(self):
        with self.assertRaises(ServiceError):
            self.submit(side_b=[self.p["Al"]])

    def test_a_bad_score_is_refused_with_a_helpful_message(self):
        with self.assertRaises(ServiceError) as ctx:
            self.submit(score_text="I won easily")
        self.assertIn("6-4", str(ctx.exception))

    def test_the_rematch_warning_is_off_by_default(self):
        """The club replays opponents within days; the cooldown default is 0."""
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 2)
        sub = self.submit(auto_confirm=True)
        self.assertIsNone(sub.warning)

    def test_the_rematch_warning_still_works_when_configured(self):
        svc, p = with_roster(rematch_cooldown_days=14)
        play(svc, MS, p["Al"], p["Bo"], 2)
        sub = svc.submit_result(division=MS, side_a=[p["Al"]], side_b=[p["Bo"]],
                                score_text="6-4 6-4", played_on=days_ago(1),
                                auto_confirm=True)
        self.assertIsNotNone(sub.warning)
        self.assertEqual(svc.db.get_match(sub.match_id).status, CONFIRMED)

    def test_duplicate_player_names_are_refused(self):
        with self.assertRaises(ServiceError):
            self.svc.add_player("al")


class TestDoublesSubmission(unittest.TestCase):
    def setUp(self):
        self.svc, self.p = with_roster()

    def test_one_opponent_can_confirm_a_doubles_result(self):
        sub = self.svc.submit_result(
            division=MD, side_a=[self.p["Al"], self.p["Bo"]],
            side_b=[self.p["Cy"], self.p["Dan"]], score_text="6-4 6-4",
            played_on=days_ago(1), submitted_by=self.p["Al"])
        self.svc.confirm(sub.match_id, self.p["Cy"])
        self.assertEqual(self.svc.db.get_match(sub.match_id).status, CONFIRMED)

    def test_your_own_partner_cannot_confirm_your_result(self):
        """Otherwise the pair that entered a result could also sign it off."""
        sub = self.svc.submit_result(
            division=MD, side_a=[self.p["Al"], self.p["Bo"]],
            side_b=[self.p["Cy"], self.p["Dan"]], score_text="6-4 6-4",
            played_on=days_ago(1), submitted_by=self.p["Al"])
        with self.assertRaises(ServiceError) as ctx:
            self.svc.confirm(sub.match_id, self.p["Bo"])
        self.assertIn("other team", str(ctx.exception))

    def test_a_doubles_match_records_four_players(self):
        play(self.svc, MD, [self.p["Al"], self.p["Bo"]],
             [self.p["Cy"], self.p["Dan"]], 1)
        ladder = self.svc.engine.ladder(MD)
        self.assertEqual(len(ladder.entries), 4)
        for name in ("Al", "Bo", "Cy", "Dan"):
            self.assertEqual(ladder.entry(self.p[name]).played, 1)

    def test_both_winners_gain_and_both_losers_lose(self):
        for day in (5, 4, 3):
            play(self.svc, MD, [self.p["Al"], self.p["Bo"]],
                 [self.p["Cy"], self.p["Dan"]], day)
        ladder = self.svc.engine.ladder(MD)
        for winner in ("Al", "Bo"):
            self.assertGreater(ladder.entry(self.p[winner]).rating.rating, 1500)
        for loser in ("Cy", "Dan"):
            self.assertLess(ladder.entry(self.p[loser]).rating.rating, 1500)

    def test_a_repeated_player_within_a_match_is_refused(self):
        with self.assertRaises(ServiceError):
            self.svc.submit_result(
                division=MD, side_a=[self.p["Al"], self.p["Bo"]],
                side_b=[self.p["Al"], self.p["Cy"]], score_text="6-4 6-4",
                played_on=days_ago(1))

    def test_head_to_head_ignores_matches_played_as_partners(self):
        play(self.svc, MD, [self.p["Al"], self.p["Bo"]],
             [self.p["Cy"], self.p["Dan"]], 5)
        together, _, meetings = self.svc.engine.head_to_head(
            self.p["Al"], self.p["Bo"])
        self.assertEqual(meetings, [])
        wins, losses, against = self.svc.engine.head_to_head(
            self.p["Al"], self.p["Cy"])
        self.assertEqual((wins, losses, len(against)), (1, 0, 1))


class TestDivisions(unittest.TestCase):
    def setUp(self):
        self.svc, self.p = with_roster()

    def test_wrong_category_is_blocked(self):
        with self.assertRaises(ServiceError) as ctx:
            play(self.svc, MS, self.p["Eve"], self.p["Al"], 1)
        self.assertIn("Eve", str(ctx.exception))

    def test_mixed_doubles_needs_one_of_each_per_side(self):
        with self.assertRaises(ServiceError) as ctx:
            play(self.svc, XD, [self.p["Al"], self.p["Bo"]],
                 [self.p["Cy"], self.p["Eve"]], 1)
        self.assertIn("one player from each", str(ctx.exception))

    def test_a_valid_mixed_pairing_is_accepted(self):
        play(self.svc, XD, [self.p["Al"], self.p["Eve"]],
             [self.p["Bo"], self.p["Fay"]], 1)
        self.assertEqual(len(self.svc.engine.ladder(XD).entries), 4)

    def test_admin_override_bypasses_the_category_check(self):
        play(self.svc, MS, self.p["Eve"], self.p["Al"], 1,
             allow_category_override=True)
        self.assertEqual(len(self.svc.engine.ladder(MS).entries), 2)

    def test_a_singles_lineup_is_refused_in_a_doubles_division(self):
        with self.assertRaises(ServiceError) as ctx:
            play(self.svc, MD, self.p["Al"], self.p["Bo"], 1)
        self.assertIn("two players", str(ctx.exception))

    def test_divisions_keep_separate_ratings(self):
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 5, score="6-0 6-0")
        singles = self.svc.engine.ladder(MS).entry(self.p["Al"]).rating.rating
        doubles = self.svc.engine.ladder(MD).entry(self.p["Al"])
        self.assertGreater(singles, 1500)
        self.assertIsNone(doubles)      # no doubles matches, so no doubles entry

    def test_an_unknown_division_is_refused(self):
        with self.assertRaises(ServiceError):
            play(self.svc, "beach_volleyball", self.p["Al"], self.p["Bo"], 1)

    def test_a_disabled_division_is_refused(self):
        svc, p = with_roster(enabled_divisions=[MS])
        with self.assertRaises(ServiceError) as ctx:
            play(svc, MD, [p["Al"], p["Bo"]], [p["Cy"], p["Dan"]], 1)
        self.assertIn("isn't one of this club's ladders", str(ctx.exception))


class TestCrossDivisionSeeding(unittest.TestCase):
    def test_doubles_starts_from_the_players_singles_rating(self):
        svc, p = with_roster()
        for day in range(30, 5, -3):
            play(svc, MS, p["Al"], p["Bo"], day, score="6-1 6-1")
        singles = svc.engine.ladder(MS).entry(p["Al"]).rating.rating
        self.assertGreater(singles, 1600)

        play(svc, MD, [p["Al"], p["Cy"]], [p["Bo"], p["Dan"]], 2)
        entry = svc.engine.ladder(MD).entry(p["Al"])
        self.assertEqual(entry.seeded_from, "Men's Singles")
        # Started near the singles rating rather than at 1500.
        self.assertGreater(entry.rating.rating, 1550)

    def test_a_player_with_no_singles_history_starts_at_the_default(self):
        svc, p = with_roster()
        play(svc, MD, [p["Al"], p["Cy"]], [p["Bo"], p["Dan"]], 2)
        self.assertEqual(svc.engine.ladder(MD).entry(p["Al"]).seeded_from, "new")

    def test_seeding_can_be_switched_off(self):
        svc, p = with_roster(cross_division_seed=False)
        for day in range(30, 5, -3):
            play(svc, MS, p["Al"], p["Bo"], day, score="6-1 6-1")
        play(svc, MD, [p["Al"], p["Cy"]], [p["Bo"], p["Dan"]], 2)
        self.assertEqual(svc.engine.ladder(MD).entry(p["Al"]).seeded_from, "new")

    def test_mixed_doubles_seeds_from_each_players_own_singles_ladder(self):
        svc, p = with_roster()
        for day in range(30, 5, -3):
            play(svc, MS, p["Al"], p["Bo"], day, score="6-1 6-1")
            play(svc, WS, p["Eve"], p["Fay"], day, score="6-1 6-1")
        play(svc, XD, [p["Al"], p["Eve"]], [p["Bo"], p["Fay"]], 2)
        ladder = svc.engine.ladder(XD)
        self.assertEqual(ladder.entry(p["Al"]).seeded_from, "Men's Singles")
        self.assertEqual(ladder.entry(p["Eve"]).seeded_from, "Women's Singles")


class TestLadder(unittest.TestCase):
    def setUp(self):
        self.svc, self.p = with_roster()

    def test_winning_climbs_the_ladder(self):
        for day in range(20, 0, -3):
            play(self.svc, MS, self.p["Al"], self.p["Dan"], day)
        ladder = self.svc.engine.ladder(MS)
        self.assertLess(ladder.rank_of(self.p["Al"]), ladder.rank_of(self.p["Dan"]))

    def test_who_you_beat_matters_not_just_how_often(self):
        for day in range(30, 10, -4):
            play(self.svc, MS, self.p["Bo"], self.p["Dan"], day)
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 8)
        for day in (7, 5, 3):
            play(self.svc, MS, self.p["Cy"], self.p["Dan"], day)
        ladder = self.svc.engine.ladder(MS)
        self.assertLess(ladder.rank_of(self.p["Al"]), ladder.rank_of(self.p["Cy"]))

    def test_an_unproven_player_does_not_top_the_ladder_on_one_upset(self):
        for day in range(40, 5, -3):
            play(self.svc, MS, self.p["Bo"], self.p["Cy"], day)
        newcomer = self.svc.add_player("Zed", category=div.MENS)
        play(self.svc, MS, newcomer.id, self.p["Bo"], 2, score="6-4 6-4")
        ladder = self.svc.engine.ladder(MS)
        self.assertTrue(ladder.entry(newcomer.id).provisional)
        self.assertGreater(ladder.rank_of(newcomer.id), 1)

    def test_a_rating_settles_after_three_matches(self):
        """The club's setting: rated after 3 results, not 5."""
        for day in (9, 6, 3):
            play(self.svc, MS, self.p["Al"], self.p["Bo"], day)
        self.assertFalse(self.svc.engine.ladder(MS).entry(self.p["Al"]).provisional)

    def test_deleting_a_match_rewinds_every_rating_after_it(self):
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 5)
        match_id = self.svc.db.list_matches()[0].id
        self.assertGreater(
            self.svc.engine.ladder(MS).entry(self.p["Al"]).rating.rating, 1500)
        self.svc.delete_match(match_id)
        self.assertEqual(self.svc.engine.ladder(MS).entries, [])

    def test_the_replay_is_deterministic(self):
        for day in range(30, 0, -3):
            play(self.svc, MS, self.p["Al"], self.p["Bo"], day)
        first = [e.rating.rating for e in self.svc.engine.ladder(MS).entries]
        self.svc.engine.invalidate()
        second = [e.rating.rating for e in self.svc.engine.ladder(MS).entries]
        self.assertEqual(first, second)

    def test_inactive_players_leave_the_ladder_but_keep_their_history(self):
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 5)
        self.svc.db.set_player_active(self.p["Bo"], False)
        self.svc.engine.invalidate()
        self.assertIsNone(self.svc.engine.ladder(MS).entry(self.p["Bo"]))
        self.assertIsNotNone(
            self.svc.engine.ladder(MS, include_inactive=True).entry(self.p["Bo"]))

    def test_ranks_are_contiguous(self):
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 5)
        ranks = [e.rank for e in self.svc.engine.ladder(MS).entries]
        self.assertEqual(ranks, list(range(1, len(ranks) + 1)))

    def test_inactivity_widens_uncertainty(self):
        play(self.svc, MS, self.p["Al"], self.p["Bo"], 200)
        for day in (10, 7, 4):
            play(self.svc, MS, self.p["Cy"], self.p["Dan"], day)
        ladder = self.svc.engine.ladder(MS)
        self.assertGreater(ladder.entry(self.p["Al"]).rating.rd,
                           ladder.entry(self.p["Cy"]).rating.rd)

    def test_rank_movement_reports_an_overtake(self):
        order = ["Al", "Bo", "Cy", "Dan"]
        day = 90
        for _ in range(6):
            play(self.svc, MS, self.p["Al"], self.p["Bo"], day)
            play(self.svc, MS, self.p["Bo"], self.p["Cy"], day - 1)
            play(self.svc, MS, self.p["Cy"], self.p["Dan"], day - 2)
            play(self.svc, MS, self.p["Al"], self.p["Cy"], day - 3)
            play(self.svc, MS, self.p["Bo"], self.p["Dan"], day - 4)
            day -= 7
        self.assertEqual([e.player.name for e in self.svc.engine.ladder(MS).entries],
                         order)
        for _ in range(3):
            play(self.svc, MS, self.p["Dan"], self.p["Cy"], 2, score="6-0 6-0")
            play(self.svc, MS, self.p["Dan"], self.p["Cy"], 1, score="6-1 6-0")
        ladder = self.svc.engine.ladder(MS)
        self.assertLess(ladder.rank_of(self.p["Dan"]), ladder.rank_of(self.p["Cy"]))
        self.assertEqual(ladder.entry(self.p["Dan"]).rank_change, 1)
        self.assertEqual(ladder.entry(self.p["Cy"]).rank_change, -1)
        self.assertEqual(sum(e.rank_change for e in ladder.entries), 0)

    def test_challenge_window_respects_the_configured_range(self):
        svc = fresh(challenge_up_positions=2)
        names = ["P1", "P2", "P3", "P4", "P5"]
        ids = {n: svc.add_player(n, category=div.MENS).id for n in names}
        day = 60
        for i, higher in enumerate(names):
            for lower in names[i + 1:]:
                play(svc, MS, ids[higher], ids[lower], day, score="6-2 6-2")
                day -= 1
        bottom = svc.engine.ladder(MS).entries[-1]
        targets = svc.engine.challengeable(bottom.player.id, MS)
        self.assertEqual([t.rank for t in targets], [bottom.rank - 2, bottom.rank - 1])


class TestPartnerChemistry(unittest.TestCase):
    def test_beating_expectation_together_scores_positive(self):
        svc, p = with_roster()
        # Al and Dan repeatedly beat Bo and Cy. Since everyone starts level,
        # winning is beating expectation.
        for day in range(12, 0, -2):
            play(svc, MD, [p["Al"], p["Dan"]], [p["Bo"], p["Cy"]], day,
                 score="6-1 6-1")
        stats = {s.partner_id: s for s in svc.engine.partners_of(p["Al"])}
        self.assertIn(p["Dan"], stats)
        self.assertGreater(stats[p["Dan"]].over_expectation, 0)
        self.assertGreater(stats[p["Dan"]].per_match, 0)

    def test_losing_pairs_score_negative(self):
        svc, p = with_roster()
        for day in range(12, 0, -2):
            play(svc, MD, [p["Al"], p["Dan"]], [p["Bo"], p["Cy"]], day,
                 score="6-1 6-1")
        stats = {s.partner_id: s for s in svc.engine.partners_of(p["Bo"])}
        self.assertLess(stats[p["Cy"]].per_match, 0)

    def test_identical_records_score_differently_by_quality_of_opposition(self):
        """The whole point of the metric. Two pairs both go 1-0; the one that
        beat a strong pair has better chemistry than the one that beat a weak
        pair. Raw win/loss cannot see this difference at all."""
        svc, p = with_roster()
        more = {n: svc.add_player(n, category=div.MENS).id
                for n in ("Ed", "Fin", "Gus", "Hal")}

        # Ed & Fin build a strong doubles rating at Gus & Hal's expense.
        for day in range(40, 10, -2):
            play(svc, MD, [more["Ed"], more["Fin"]], [more["Gus"], more["Hal"]],
                 day, score="6-1 6-1")
        strong = svc.engine.ladder(MD).entry(more["Ed"]).rating.rating
        weak = svc.engine.ladder(MD).entry(more["Gus"]).rating.rating
        self.assertGreater(strong, weak + 200)

        play(svc, MD, [p["Al"], p["Bo"]], [more["Ed"], more["Fin"]], 4)
        play(svc, MD, [p["Cy"], p["Dan"]], [more["Gus"], more["Hal"]], 4)

        beat_strong = {s.partner_id: s for s in svc.engine.partners_of(p["Al"])}[p["Bo"]]
        beat_weak = {s.partner_id: s for s in svc.engine.partners_of(p["Cy"])}[p["Dan"]]
        self.assertEqual(beat_strong.record, beat_weak.record)      # both 1-0
        self.assertGreater(beat_strong.per_match, beat_weak.per_match)

    def test_an_unbeaten_pair_can_still_be_under_expectation(self):
        svc, p = with_roster()
        for day in range(40, 12, -2):
            play(svc, MD, [p["Bo"], p["Cy"]], [p["Al"], p["Dan"]], day,
                 score="6-0 6-0")
        before = {s.partner_id: s
                  for s in svc.engine.partners_of(p["Bo"])}[p["Cy"]].per_match
        # Now they only scrape past the same opponents, repeatedly.
        for day in range(11, 0, -1):
            play(svc, MD, [p["Bo"], p["Cy"]], [p["Al"], p["Dan"]], day,
                 score="7-6 6-7 10-8")
        stat = {s.partner_id: s for s in svc.engine.partners_of(p["Bo"])}[p["Cy"]]
        self.assertEqual(stat.lost, 0)               # still unbeaten...
        self.assertLess(stat.per_match, before)      # ...but trending down

    def test_thin_samples_are_flagged(self):
        svc, p = with_roster()
        play(svc, MD, [p["Al"], p["Dan"]], [p["Bo"], p["Cy"]], 1)
        stat = svc.engine.partners_of(p["Al"])[0]
        self.assertTrue(stat.thin)

    def test_singles_matches_produce_no_partner_stats(self):
        svc, p = with_roster()
        play(svc, MS, p["Al"], p["Bo"], 1)
        self.assertEqual(svc.engine.partners_of(p["Al"]), [])

    def test_partners_are_ordered_best_chemistry_first(self):
        svc, p = with_roster()
        for day in range(12, 6, -2):
            play(svc, MD, [p["Al"], p["Bo"]], [p["Cy"], p["Dan"]], day,
                 score="6-0 6-0")
        for day in range(6, 0, -2):
            play(svc, MD, [p["Cy"], p["Al"]], [p["Bo"], p["Dan"]], day,
                 score="7-6 7-6")
        stats = svc.engine.partners_of(p["Al"])
        self.assertGreaterEqual(stats[0].per_match, stats[-1].per_match)


class TestCsvImport(unittest.TestCase):
    def test_import_creates_players_and_singles_results(self):
        svc = fresh()
        csv_text = (
            "date,division,player_a,player_b,score,note\n"
            f"{days_ago(20)},mens_singles,Ana Silva,Ben Okafor,6-4 6-2,league\n"
            f"{days_ago(10)},mens_singles,Ben Okafor,Ana Silva,7-6 4-6 10-8,rematch\n"
        )
        imported, errors = svc.import_csv(csv_text)
        self.assertEqual((imported, errors), (2, []))
        self.assertEqual(len(svc.db.list_players()), 2)

    def test_import_handles_doubles_rows(self):
        svc = fresh()
        csv_text = (
            "date,division,player_a,player_a2,player_b,player_b2,score\n"
            f"{days_ago(5)},mixed_doubles,Ana,Ben,Cara,Dan,6-4 6-2\n"
        )
        imported, errors = svc.import_csv(csv_text)
        self.assertEqual((imported, errors), (1, []))
        match = svc.db.confirmed_matches_chronological()[0]
        self.assertTrue(match.is_doubles)
        self.assertEqual(len(match.players), 4)

    def test_division_aliases_are_accepted(self):
        svc = fresh()
        imported, errors = svc.import_csv(
            "date,division,player_a,player_b,score\n"
            f"{days_ago(5)},MS,Ana,Ben,6-4 6-2\n"
            f"{days_ago(4)},Mixed,Ana,Ben,6-4 6-2\n")
        self.assertEqual(imported, 1)                # mixed needs four players
        self.assertEqual(len(errors), 1)

    def test_bad_rows_are_reported_and_good_rows_still_land(self):
        svc = fresh()
        csv_text = (
            "date,division,player_a,player_b,score\n"
            f"{days_ago(5)},mens_singles,Ana,Ben,6-4 6-2\n"
            f"{days_ago(4)},mens_singles,Ana,Ben,nonsense\n"
            f"not-a-date,mens_singles,Ana,Ben,6-4 6-2\n"
            f"{days_ago(3)},quidditch,Ana,Ben,6-4 6-2\n"
        )
        imported, errors = svc.import_csv(csv_text)
        self.assertEqual(imported, 1)
        self.assertEqual(len(errors), 3)

    def test_missing_columns_are_rejected_clearly(self):
        svc = fresh()
        with self.assertRaises(ServiceError) as ctx:
            svc.import_csv("date,winner,loser\n2026-01-01,Ana,Ben\n")
        self.assertIn("player_a", str(ctx.exception))

    def test_export_round_trips_through_import(self):
        svc = fresh()
        svc.import_csv("date,division,player_a,player_b,score\n"
                       f"{days_ago(3)},mens_singles,Ana,Ben,6-4 6-2\n")
        exported = svc.export_matches_csv()
        self.assertIn("Ana", exported)
        self.assertIn("6-4 6-2", exported)
        self.assertIn("confirmed", exported)


if __name__ == "__main__":
    unittest.main()
