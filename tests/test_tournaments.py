"""Draws, seeding, byes and standings."""

import unittest

from ladder import divisions as div
from ladder import tournaments as T
from ladder.service import ServiceError
from tests.helpers import days_ago, play, with_roster

MS = div.MENS_SINGLES
MD = div.MENS_DOUBLES


class TestSeeding(unittest.TestCase):
    def test_the_pattern_matches_the_standard_bracket(self):
        self.assertEqual(T.seed_positions(2), [1, 2])
        self.assertEqual(T.seed_positions(4), [1, 4, 2, 3])
        self.assertEqual(T.seed_positions(8), [1, 8, 4, 5, 2, 7, 3, 6])

    def test_every_seed_appears_exactly_once(self):
        for size in (2, 4, 8, 16, 32):
            order = T.seed_positions(size)
            self.assertEqual(sorted(order), list(range(1, size + 1)))

    def test_bracket_size_rounds_up_to_a_power_of_two(self):
        self.assertEqual([T.bracket_size(n) for n in (2, 3, 5, 8, 9)],
                         [2, 4, 8, 8, 16])

    def test_random_seeding_keeps_the_same_field(self):
        import random
        entrants = [10, 20, 30, 40, 50]
        shuffled = T.order_entrants(entrants, T.RANDOM_SEEDING, random.Random(1))
        self.assertEqual(sorted(shuffled), sorted(entrants))

    def test_ladder_seeding_preserves_the_given_order(self):
        entrants = [10, 20, 30]
        self.assertEqual(T.order_entrants(entrants, T.LADDER_SEEDING), entrants)


class TestEliminationDraw(unittest.TestCase):
    def test_the_top_two_seeds_can_only_meet_in_the_final(self):
        draw = T.build_elimination(list(range(1, 9)))
        first_round = [(p.player_a, p.player_b) for p in draw.rounds[0]]
        self.assertFalse(any(1 in pair and 2 in pair for pair in first_round))
        self.assertEqual(draw.round_names[-1], "Final")

    def test_top_seed_plays_bottom_seed_first(self):
        draw = T.build_elimination(list(range(1, 9)))
        self.assertIn((1, 8), [(p.player_a, p.player_b) for p in draw.rounds[0]])

    def test_byes_go_to_the_top_seeds(self):
        # Six entrants in an eight-draw means two byes, for seeds 1 and 2.
        draw = T.build_elimination([101, 102, 103, 104, 105, 106])
        byes = [p.occupant for p in draw.rounds[0] if p.is_bye]
        self.assertEqual(sorted(byes), [101, 102])

    def test_round_count_and_names(self):
        draw = T.build_elimination(list(range(1, 9)))
        self.assertEqual(draw.round_count, 3)
        self.assertEqual(draw.round_names,
                         ["Quarter-finals", "Semi-finals", "Final"])

    def test_a_two_player_draw_is_just_a_final(self):
        draw = T.build_elimination([1, 2])
        self.assertEqual(draw.round_count, 1)
        self.assertEqual(draw.round_names, ["Final"])

    def test_fewer_than_two_entrants_is_refused(self):
        with self.assertRaises(ValueError):
            T.build_elimination([1])

    def test_winners_feed_the_right_slot(self):
        self.assertEqual(T.parent_slot(0, 0), (1, 0, "a"))
        self.assertEqual(T.parent_slot(0, 1), (1, 0, "b"))
        self.assertEqual(T.parent_slot(0, 2), (1, 1, "a"))


class TestRoundRobinDraw(unittest.TestCase):
    def test_everyone_plays_everyone_exactly_once(self):
        draw = T.build_round_robin([1, 2, 3, 4, 5])
        pairs = {frozenset((p.player_a, p.player_b))
                 for p in draw.all_pairings()}
        self.assertEqual(len(pairs), 10)            # 5 choose 2
        self.assertEqual(len(draw.all_pairings()), 10)

    def test_an_even_field_pairs_everyone_each_round(self):
        draw = T.build_round_robin([1, 2, 3, 4])
        self.assertEqual(draw.round_count, 3)
        for pairings in draw.rounds:
            self.assertEqual(len(pairings), 2)

    def test_an_odd_field_sits_one_player_out_each_round(self):
        draw = T.build_round_robin([1, 2, 3, 4, 5])
        self.assertEqual(draw.round_count, 5)
        for pairings in draw.rounds:
            self.assertEqual(len(pairings), 2)      # 5 players -> 2 matches
            everyone = [p.player_a for p in pairings] + [p.player_b for p in pairings]
            self.assertEqual(len(set(everyone)), 4)

    def test_nobody_is_drawn_against_themselves(self):
        draw = T.build_round_robin([1, 2, 3, 4, 5, 6, 7])
        for p in draw.all_pairings():
            self.assertNotEqual(p.player_a, p.player_b)


class TestStandings(unittest.TestCase):
    def results(self, *pairs):
        return [{"winner": w, "loser": l, "winner_games": 6, "loser_games": 3}
                for w, l in pairs]

    def test_ordered_by_wins(self):
        table = T.standings([1, 2, 3], self.results((1, 2), (1, 3), (2, 3)))
        self.assertEqual([s.player_id for s in table], [1, 2, 3])
        self.assertEqual(table[0].record, "2-0")

    def test_head_to_head_breaks_a_tie_on_wins(self):
        """1 and 2 both finish 2-1; 2 beat 1, so 2 goes above."""
        table = T.standings([1, 2, 3, 4], self.results(
            (2, 1), (1, 3), (1, 4), (2, 3), (4, 2), (3, 4)))
        tied = [s.player_id for s in table if s.won == 2]
        self.assertEqual(tied, [2, 1])

    def test_a_three_way_cycle_cannot_be_resolved_by_head_to_head(self):
        """Everyone beat someone and lost to someone, with identical scores.

        Nothing separates them and nothing should pretend to -- this is a
        real property of round robins, not a gap in the tie-breaks. It's
        recorded here so a future change doesn't silently invent an order.
        """
        table = T.standings([1, 2, 3], self.results((1, 3), (2, 1), (3, 2)))
        self.assertTrue(all(s.won == 1 and s.lost == 1 for s in table))
        self.assertEqual(len({s.games_diff for s in table}), 1)

    def test_games_difference_is_the_final_tiebreak(self):
        results = [
            {"winner": 1, "loser": 3, "winner_games": 6, "loser_games": 0},
            {"winner": 2, "loser": 3, "winner_games": 6, "loser_games": 5},
        ]
        table = T.standings([1, 2, 3], results)
        self.assertEqual(table[0].player_id, 1)

    def test_players_with_no_matches_still_appear(self):
        table = T.standings([1, 2, 3], self.results((1, 2)))
        self.assertEqual(len(table), 3)
        idle = next(s for s in table if s.player_id == 3)
        self.assertEqual((idle.played, idle.won, idle.lost), (0, 0, 0))
        # A player yet to play outranks one who has lost: 0 games difference
        # beats a negative one, which is the honest ordering.
        self.assertLess([s.player_id for s in table].index(3),
                        [s.player_id for s in table].index(2))


class TestRunningATournament(unittest.TestCase):
    def setUp(self):
        self.svc, self.p = with_roster()
        # Establish a ladder order: Al > Bo > Cy > Dan.
        day = 60
        for _ in range(4):
            for higher, lower in (("Al", "Bo"), ("Bo", "Cy"),
                                  ("Cy", "Dan"), ("Al", "Cy")):
                play(self.svc, MS, self.p[higher], self.p[lower], day)
                day -= 1

    def create(self, style=T.ELIMINATION, seeding=T.LADDER_SEEDING, players=None):
        return self.svc.create_tournament(
            name="Fall Open", division=MS, style=style, seeding=seeding,
            match_format="one_set",
            player_ids=players or [self.p[n] for n in ("Dan", "Al", "Cy", "Bo")])

    def test_ladder_seeding_reorders_the_entry_list(self):
        tournament = self.create()
        seeds = dict(self.svc.db.entries(tournament.id))
        self.assertEqual(seeds[self.p["Al"]], 1)
        self.assertEqual(seeds[self.p["Dan"]], 4)

    def test_the_draw_pairs_top_seed_against_bottom(self):
        tournament = self.create()
        first = self.svc.db.tournament_matches(tournament.id, round_no=0)
        self.assertEqual({first[0].player_a, first[0].player_b},
                         {self.p["Al"], self.p["Dan"]})

    def test_rounds_get_deadlines(self):
        tournament = self.create()
        rounds = self.svc.db.rounds(tournament.id)
        self.assertEqual(len(rounds), 2)
        self.assertTrue(all(r.deadline for r in rounds))

    def test_a_confirmed_result_advances_the_bracket(self):
        tournament = self.create()
        play(self.svc, MS, self.p["Al"], self.p["Dan"], 3, score="6-2")
        play(self.svc, MS, self.p["Cy"], self.p["Bo"], 3, score="6-4")
        final = self.svc.db.tournament_matches(tournament.id, round_no=1)[0]
        self.assertEqual({final.player_a, final.player_b},
                         {self.p["Al"], self.p["Cy"]})
        self.assertEqual(final.status, "ready")

    def test_the_tournament_completes_when_the_final_is_played(self):
        tournament = self.create()
        play(self.svc, MS, self.p["Al"], self.p["Dan"], 3, score="6-2")
        play(self.svc, MS, self.p["Cy"], self.p["Bo"], 3, score="6-4")
        play(self.svc, MS, self.p["Cy"], self.p["Al"], 2, score="7-5")
        self.assertEqual(self.svc.db.get_tournament(tournament.id).status,
                         "complete")

    def test_tournament_matches_still_count_for_the_ladder(self):
        self.create()
        before = self.svc.engine.ladder(MS).entry(self.p["Dan"]).played
        play(self.svc, MS, self.p["Al"], self.p["Dan"], 3, score="6-2")
        after = self.svc.engine.ladder(MS).entry(self.p["Dan"]).played
        self.assertEqual(after, before + 1)

    def test_byes_advance_without_being_played(self):
        extra = self.svc.add_player("Ed", category=div.MENS)
        tournament = self.create(players=[self.p[n] for n in
                                          ("Al", "Bo", "Cy", "Dan")] + [extra.id])
        first = self.svc.db.tournament_matches(tournament.id, round_no=0)
        byes = [m for m in first if m.status == "bye"]
        self.assertTrue(byes)
        for bye in byes:
            self.assertIsNotNone(bye.winner_id)

    def test_an_unplayed_match_past_its_deadline_is_flagged(self):
        tournament = self.create()
        self.svc.db.set_round_deadline(tournament.id, 0, days_ago(3))
        overdue = self.svc.overdue_matches(tournament.id)
        self.assertEqual(len(overdue), 2)

    def test_nothing_is_flagged_before_the_deadline(self):
        tournament = self.create()
        self.assertEqual(self.svc.overdue_matches(tournament.id), [])

    def test_an_admin_can_send_someone_through(self):
        tournament = self.create()
        first = self.svc.db.tournament_matches(tournament.id, round_no=0)[0]
        self.svc.force_tournament_winner(first.id, self.p["Al"])
        final = self.svc.db.tournament_matches(tournament.id, round_no=1)[0]
        self.assertIn(self.p["Al"], final.players)

    def test_forcing_a_player_who_is_not_in_the_match_is_refused(self):
        tournament = self.create()
        first = self.svc.db.tournament_matches(tournament.id, round_no=0)[0]
        outsider = [pid for pid in self.p.values() if pid not in first.players][0]
        with self.assertRaises(ServiceError):
            self.svc.force_tournament_winner(first.id, outsider)

    def test_round_robin_standings_come_from_real_results(self):
        tournament = self.create(style=T.ROUND_ROBIN, seeding=T.RANDOM_SEEDING)
        day = 20
        for winner, losers in (("Al", ["Bo", "Cy", "Dan"]),
                               ("Bo", ["Cy", "Dan"]), ("Cy", ["Dan"])):
            for loser in losers:
                play(self.svc, MS, self.p[winner], self.p[loser], day,
                     score="6-3")
                day -= 1
        table = self.svc.tournament_standings(tournament.id)
        self.assertEqual([s.record for s in table], ["3-0", "2-1", "1-2", "0-3"])
        self.assertEqual(table[0].player_id, self.p["Al"])

    def test_doubles_divisions_are_refused_for_now(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.create_tournament(
                name="Doubles Cup", division=MD, style=T.ELIMINATION,
                seeding=T.LADDER_SEEDING, match_format="one_set",
                player_ids=[self.p["Al"], self.p["Bo"]])
        self.assertIn("singles", str(ctx.exception))

    def test_a_duplicate_entrant_is_refused(self):
        with self.assertRaises(ServiceError):
            self.create(players=[self.p["Al"], self.p["Al"], self.p["Bo"]])

    def test_a_field_of_one_is_refused(self):
        with self.assertRaises(ServiceError):
            self.create(players=[self.p["Al"]])

    def test_an_unknown_format_is_refused(self):
        with self.assertRaises(ServiceError):
            self.svc.create_tournament(
                name="X", division=MS, style="ladder_war",
                seeding=T.LADDER_SEEDING, match_format="one_set",
                player_ids=[self.p["Al"], self.p["Bo"]])


if __name__ == "__main__":
    unittest.main()
