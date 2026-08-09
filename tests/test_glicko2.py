"""Glicko-2 checked against the worked example in Glickman's own paper.

http://www.glicko.net/glicko/glicko2.pdf -- section "Example calculation".
A player rated 1500 (RD 200) plays three opponents and finishes at 1464.06
with RD 151.52. If this test fails, the rating maths is wrong, and everything
built on it is wrong too.
"""

import unittest

from ladder.glicko2 import Rating, Result, conservative, decay, rate, win_probability


class TestGlickmanExample(unittest.TestCase):
    def setUp(self):
        self.player = Rating(1500, 200, 0.06)
        self.results = [
            Result(Rating(1400, 30), 1.0),
            Result(Rating(1550, 100), 0.0),
            Result(Rating(1700, 300), 0.0),
        ]

    def test_matches_published_values(self):
        out = rate(self.player, self.results, tau=0.5, min_rd=0, max_rd=350)
        self.assertAlmostEqual(out.rating, 1464.06, places=1)
        self.assertAlmostEqual(out.rd, 151.52, places=1)
        self.assertAlmostEqual(out.volatility, 0.05999, places=4)


class TestRatingBehaviour(unittest.TestCase):
    def test_beating_a_stronger_player_gains_more(self):
        player = Rating(1500, 100, 0.06)
        vs_strong = rate(player, [Result(Rating(1800, 50), 1.0)])
        vs_weak = rate(player, [Result(Rating(1200, 50), 1.0)])
        self.assertGreater(vs_strong.rating, vs_weak.rating)

    def test_losing_to_a_stronger_player_costs_less(self):
        player = Rating(1500, 100, 0.06)
        to_strong = rate(player, [Result(Rating(1800, 50), 0.0)])
        to_weak = rate(player, [Result(Rating(1200, 50), 0.0)])
        self.assertLess(to_strong.rating, 1500)
        self.assertLess(to_weak.rating, to_strong.rating)

    def test_playing_reduces_uncertainty(self):
        player = Rating(1500, 350, 0.06)
        after = rate(player, [Result(Rating(1500, 50), 1.0)])
        self.assertLess(after.rd, player.rd)

    def test_beating_an_unknown_moves_you_less_than_beating_a_known_equal(self):
        player = Rating(1500, 80, 0.06)
        vs_known = rate(player, [Result(Rating(1500, 30), 1.0)])
        vs_unknown = rate(player, [Result(Rating(1500, 350), 1.0)])
        self.assertGreater(vs_known.rating, vs_unknown.rating)

    def test_sitting_out_widens_uncertainty_but_keeps_rating(self):
        player = Rating(1600, 60, 0.06)
        aged = decay(player)
        self.assertEqual(aged.rating, 1600)
        self.assertGreater(aged.rd, player.rd)

    def test_uncertainty_is_capped_on_long_absence(self):
        player = Rating(1600, 60, 0.06)
        for _ in range(500):
            player = decay(player, max_rd=350)
        self.assertLessEqual(player.rd, 350)

    def test_rd_floor_is_respected(self):
        player = Rating(1500, 40, 0.06)
        for _ in range(50):
            player = rate(player, [Result(Rating(1500, 30), 1.0)], min_rd=30)
        self.assertGreaterEqual(player.rd, 30)

    def test_margin_scores_land_between_win_and_loss(self):
        player = Rating(1500, 100, 0.06)
        opponent = Rating(1500, 100)
        clean = rate(player, [Result(opponent, 1.0)])
        narrow = rate(player, [Result(opponent, 0.9)])
        loss = rate(player, [Result(opponent, 0.0)])
        self.assertGreater(clean.rating, narrow.rating)
        self.assertGreater(narrow.rating, loss.rating)

    def test_empty_period_is_treated_as_inactivity(self):
        player = Rating(1500, 60, 0.06)
        self.assertEqual(rate(player, []), decay(player))


class TestDerivedNumbers(unittest.TestCase):
    def test_win_probability_is_symmetric(self):
        a, b = Rating(1700, 50), Rating(1500, 50)
        self.assertAlmostEqual(win_probability(a, b) + win_probability(b, a), 1.0, places=9)

    def test_even_players_are_a_coin_flip(self):
        even = Rating(1500, 80)
        self.assertAlmostEqual(win_probability(even, even), 0.5, places=9)

    def test_stronger_player_is_favoured(self):
        self.assertGreater(win_probability(Rating(1800, 50), Rating(1400, 50)), 0.8)

    def test_conservative_rating_penalises_uncertainty(self):
        proven = Rating(1600, 40)
        unproven = Rating(1600, 300)
        self.assertGreater(conservative(proven), conservative(unproven))


if __name__ == "__main__":
    unittest.main()
