"""The doubles rating model.

The club's requirement, in the user's words: "bad teammate and still win does a
lot for your score." These tests encode that literally, plus the invariants that
make the model trustworthy rather than merely generous.
"""

import unittest

from ladder.doubles import (
    match_results, team_rating, team_win_probability, virtual_opponent,
)
from ladder.glicko2 import Rating, rate, win_probability


class TestVirtualOpponent(unittest.TestCase):
    def test_singles_reduces_to_the_actual_opponent(self):
        """The n=1 case must degenerate exactly, or singles and doubles would
        need two code paths that could drift apart."""
        me, them = Rating(1600, 80), Rating(1500, 120)
        virtual = virtual_opponent(0, [me], [them])
        self.assertAlmostEqual(virtual.rating, them.rating, places=9)
        self.assertAlmostEqual(virtual.rd, them.rd, places=9)

    def test_the_gap_faced_equals_the_gap_between_teams(self):
        side = [Rating(1600, 80), Rating(1200, 80)]
        opponents = [Rating(1500, 80), Rating(1500, 80)]
        team_gap = (1600 + 1200) / 2 - (1500 + 1500) / 2       # -100
        virtual = virtual_opponent(0, side, opponents)
        self.assertAlmostEqual(side[0].rating - virtual.rating, team_gap, places=9)

    def test_a_weak_partner_raises_the_virtual_opponent(self):
        opponents = [Rating(1500, 80), Rating(1500, 80)]
        me = Rating(1600, 80)
        weak = virtual_opponent(0, [me, Rating(1200, 80)], opponents)
        strong = virtual_opponent(0, [me, Rating(1900, 80)], opponents)
        self.assertGreater(weak.rating, strong.rating)
        self.assertAlmostEqual(weak.rating, 1700, places=6)
        self.assertAlmostEqual(strong.rating, 1350, places=6)

    def test_uncertainty_comes_from_the_other_three_players(self):
        side = [Rating(1500, 50), Rating(1500, 100)]
        opponents = [Rating(1500, 200), Rating(1500, 300)]
        virtual = virtual_opponent(0, side, opponents)
        expected = (100 ** 2 + 200 ** 2 + 300 ** 2) ** 0.5 / 2
        self.assertAlmostEqual(virtual.rd, expected, places=6)

    def test_mismatched_side_sizes_are_refused(self):
        with self.assertRaises(ValueError):
            virtual_opponent(0, [Rating()], [Rating(), Rating()])


class TestTheClubsRequirement(unittest.TestCase):
    """Carrying a weak partner has to pay more than coasting behind a strong one."""

    def setUp(self):
        self.me = Rating(1600, 80, 0.06)
        self.opponents = [Rating(1500, 80), Rating(1500, 80)]

    def gain_with(self, partner_rating: float) -> float:
        partner = Rating(partner_rating, 80)
        results_a, _ = match_results([self.me, partner], self.opponents, 1.0)
        after = rate(self.me, [results_a[0]])
        return after.rating - self.me.rating

    def test_winning_with_a_weak_partner_gains_much_more(self):
        weak = self.gain_with(1200)
        equal = self.gain_with(1600)
        strong = self.gain_with(1900)
        self.assertGreater(weak, equal)
        self.assertGreater(equal, strong)
        # Not a token difference -- it should be several times the payout.
        self.assertGreater(weak, strong * 2.5)

    def test_losing_with_a_strong_partner_costs_more(self):
        def loss_with(partner_rating):
            partner = Rating(partner_rating, 80)
            results_a, _ = match_results([self.me, partner], self.opponents, 0.0)
            return rate(self.me, [results_a[0]]).rating - self.me.rating

        self.assertLess(loss_with(1900), loss_with(1200))

    def test_both_partners_share_one_team_expectation(self):
        """The property that makes the model fair rather than ad hoc: partners
        differ in how far they move, never in what was expected of them."""
        me, partner = Rating(1600, 80), Rating(1200, 80)
        results_a, _ = match_results([me, partner], self.opponents, 1.0)
        mine = win_probability(me, results_a[0].opponent)
        theirs = win_probability(partner, results_a[1].opponent)
        self.assertAlmostEqual(mine, theirs, places=9)

    def test_the_more_uncertain_partner_moves_further(self):
        settled, unproven = Rating(1500, 50, 0.06), Rating(1500, 300, 0.06)
        results_a, _ = match_results([settled, unproven], self.opponents, 1.0)
        settled_gain = rate(settled, [results_a[0]]).rating - settled.rating
        unproven_gain = rate(unproven, [results_a[1]]).rating - unproven.rating
        self.assertGreater(unproven_gain, settled_gain)


class TestConservation(unittest.TestCase):
    def test_the_two_sides_scores_always_sum_to_one(self):
        for score_a in (0.0, 0.13, 0.5, 0.87, 1.0):
            results_a, results_b = match_results(
                [Rating(1600, 80), Rating(1200, 90)],
                [Rating(1500, 70), Rating(1450, 110)], score_a)
            for a, b in zip(results_a, results_b):
                self.assertAlmostEqual(a.score + b.score, 1.0, places=9)

    def test_every_player_gets_exactly_one_result(self):
        results_a, results_b = match_results(
            [Rating(), Rating()], [Rating(), Rating()], 1.0)
        self.assertEqual((len(results_a), len(results_b)), (2, 2))


class TestTeamNumbers(unittest.TestCase):
    def test_team_rating_is_the_mean(self):
        team = team_rating([Rating(1600, 100), Rating(1400, 100)])
        self.assertAlmostEqual(team.rating, 1500, places=9)

    def test_a_pair_is_more_certain_than_either_player(self):
        team = team_rating([Rating(1500, 100), Rating(1500, 100)])
        self.assertLess(team.rd, 100)

    def test_team_win_probability_is_symmetric(self):
        strong = [Rating(1700, 60), Rating(1650, 60)]
        weak = [Rating(1400, 60), Rating(1350, 60)]
        self.assertAlmostEqual(
            team_win_probability(strong, weak) + team_win_probability(weak, strong),
            1.0, places=9)

    def test_even_teams_are_a_coin_flip(self):
        side = [Rating(1500, 80), Rating(1500, 80)]
        self.assertAlmostEqual(team_win_probability(side, list(side)), 0.5, places=9)

    def test_the_stronger_pair_is_favoured(self):
        self.assertGreater(
            team_win_probability([Rating(1800, 50), Rating(1750, 50)],
                                 [Rating(1300, 50), Rating(1250, 50)]),
            0.85)

    def test_two_mediocre_players_beat_one_star_and_one_beginner_on_paper(self):
        """Sanity check that the mean is doing something reasonable: a balanced
        pair and a lopsided pair with the same average are rated level."""
        balanced = [Rating(1500, 80), Rating(1500, 80)]
        lopsided = [Rating(1900, 80), Rating(1100, 80)]
        self.assertAlmostEqual(
            team_win_probability(balanced, lopsided), 0.5, places=6)


if __name__ == "__main__":
    unittest.main()
