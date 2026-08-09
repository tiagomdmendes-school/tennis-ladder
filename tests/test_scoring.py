"""Score parsing and the margin-of-victory conversion."""

import unittest

from ladder.scoring import ScoreError, flip_score, glicko_score, parse_score


class TestParsing(unittest.TestCase):
    def test_straight_sets(self):
        p = parse_score("6-4 6-2")
        self.assertTrue(p.a_won)
        self.assertEqual((p.sets_a, p.sets_b), (2, 0))
        self.assertEqual((p.games_a, p.games_b), (12, 6))

    def test_loss_is_read_from_player_a_perspective(self):
        p = parse_score("4-6 2-6")
        self.assertFalse(p.a_won)
        self.assertEqual((p.games_a, p.games_b), (6, 12))

    def test_tiebreak_detail_is_accepted_and_dropped(self):
        p = parse_score("7-6(5) 7-6(11)")
        self.assertEqual((p.games_a, p.games_b), (14, 12))
        self.assertEqual(p.normalised, "7-6 7-6")

    def test_commas_and_slashes_and_colons(self):
        for text in ("6-4, 6-4", "6:4 6:4", "6/4 6/4"):
            self.assertTrue(parse_score(text).a_won, text)

    def test_deciding_super_tiebreak_counts_as_one_game(self):
        p = parse_score("6-4 3-6 10-8")
        self.assertTrue(p.a_won)
        # 6+3+1 vs 4+6+0 -- the breaker is one game, not ten.
        self.assertEqual((p.games_a, p.games_b), (10, 10))

    def test_super_tiebreak_can_be_counted_literally_if_configured(self):
        p = parse_score("6-4 3-6 10-8", match_tiebreak_in_decider=False)
        self.assertEqual((p.games_a, p.games_b), (19, 18))

    def test_retirement_credits_the_player_ahead(self):
        p = parse_score("6-3 2-1 ret.")
        self.assertTrue(p.a_won)
        self.assertTrue(p.retired)

    def test_retirement_when_sets_are_level_uses_the_current_set(self):
        p = parse_score("6-3 2-6 3-1 ret.")
        self.assertTrue(p.a_won)

    def test_walkover_without_a_score_has_no_direction(self):
        p = parse_score("w/o")
        self.assertTrue(p.walkover)
        self.assertIsNone(p.a_won)

    def test_walkover_with_a_score_keeps_direction(self):
        self.assertTrue(parse_score("6-0 w/o").a_won)

    def test_normalisation_round_trips(self):
        self.assertEqual(parse_score("  6-4   3-6  10-8 ").normalised, "6-4 3-6 10-8")


class TestParsingRejections(unittest.TestCase):
    def test_empty_score(self):
        with self.assertRaises(ScoreError):
            parse_score("")

    def test_gibberish(self):
        with self.assertRaises(ScoreError):
            parse_score("we played and I won")

    def test_tied_set(self):
        with self.assertRaises(ScoreError):
            parse_score("6-6 6-3")

    def test_unfinished_match_is_rejected(self):
        with self.assertRaises(ScoreError) as ctx:
            parse_score("6-4 3-6")
        self.assertIn("ret.", str(ctx.exception))


class TestMatchFormats(unittest.TestCase):
    """Every format the club actually plays must go in as a complete match.

    Challenge matches here are usually a single set, for timing. That is the
    format most likely to be broken by a future parser change, because it looks
    superficially like an unfinished best-of-three -- so each of these is pinned
    explicitly.
    """

    def assert_complete(self, text, *, sets, games, expect_winner=True):
        parsed = parse_score(text)
        self.assertEqual(parsed.a_won, expect_winner, text)
        self.assertEqual((parsed.sets_a, parsed.sets_b), sets, text)
        self.assertEqual((parsed.games_a, parsed.games_b), games, text)
        return parsed

    def test_a_single_set_is_a_complete_match(self):
        self.assert_complete("6-4", sets=(1, 0), games=(6, 4))

    def test_a_single_set_can_be_lost(self):
        self.assert_complete("4-6", sets=(0, 1), games=(4, 6),
                             expect_winner=False)

    def test_a_single_set_decided_on_a_tiebreak(self):
        self.assert_complete("7-6", sets=(1, 0), games=(7, 6))
        self.assert_complete("7-6(5)", sets=(1, 0), games=(7, 6))

    def test_two_sets_and_a_match_tiebreak(self):
        # The deciding tie-break is one game, not ten: 6+4+1 vs 4+6+0.
        self.assert_complete("6-4 4-6 10-8", sets=(2, 1), games=(11, 10))

    def test_full_best_of_three(self):
        self.assert_complete("6-4 3-6 6-2", sets=(2, 1), games=(15, 12))

    def test_straight_sets(self):
        self.assert_complete("6-4 6-2", sets=(2, 0), games=(12, 6))

    def test_an_eight_game_pro_set(self):
        self.assert_complete("8-6", sets=(1, 0), games=(8, 6))

    def test_fast4(self):
        self.assert_complete("4-2", sets=(1, 0), games=(4, 2))

    def test_a_retirement_in_the_first_set(self):
        parsed = self.assert_complete("6-3 2-1 ret.", sets=(2, 0), games=(8, 4))
        self.assertTrue(parsed.retired)

    def test_every_format_produces_a_usable_rating_score(self):
        for text in ("6-4", "7-6", "6-4 4-6 10-8", "6-4 3-6 6-2", "8-6", "4-2"):
            parsed = parse_score(text)
            a = glicko_score(parsed, for_player_a=True)
            b = glicko_score(parsed, for_player_a=False)
            self.assertGreater(a, 0.5, text)         # A won all of these
            self.assertAlmostEqual(a + b, 1.0, places=9, msg=text)

    def test_a_single_set_counts_as_much_as_a_full_match(self):
        """A deliberate choice, not an oversight: one set is this club's normal
        format, so down-weighting it would slow every rating from settling."""
        one_set = glicko_score(parse_score("6-4"), for_player_a=True)
        three_sets = glicko_score(parse_score("6-4 3-6 6-2"), for_player_a=True)
        self.assertAlmostEqual(one_set, 0.920, places=3)
        self.assertAlmostEqual(three_sets, 0.911, places=3)

    def test_level_sets_with_no_decider_are_still_refused(self):
        """The one thing that must not be mistaken for a short format."""
        for text in ("6-4 3-6", "6-4 3-6 6-7 7-6"):
            with self.assertRaises(ScoreError, msg=text):
                parse_score(text)


class TestFlipScore(unittest.TestCase):
    """Scores are stored from player A's view; results read 'winner def. loser'."""

    def test_sets_are_reversed(self):
        self.assertEqual(flip_score("6-7 1-6"), "7-6 6-1")

    def test_annotations_are_kept(self):
        self.assertEqual(flip_score("1-2 3-6 ret."), "2-1 6-3 ret.")
        self.assertEqual(flip_score("6-0 w/o"), "0-6 w/o")

    def test_flipping_twice_is_the_original(self):
        for text in ("6-4 6-2", "6-4 3-6 10-8", "6-3 2-1 ret."):
            self.assertEqual(flip_score(flip_score(text)), text)

    def test_the_flipped_score_names_the_other_winner(self):
        original = parse_score("6-7 1-6")
        flipped = parse_score(flip_score("6-7 1-6"))
        self.assertFalse(original.a_won)
        self.assertTrue(flipped.a_won)


class TestGlickoScore(unittest.TestCase):
    def test_scores_always_sum_to_one(self):
        for text in ("6-0 6-0", "6-4 6-4", "7-6 7-6", "6-4 3-6 10-8", "4-6 6-7"):
            p = parse_score(text)
            total = (glicko_score(p, for_player_a=True)
                     + glicko_score(p, for_player_a=False))
            self.assertAlmostEqual(total, 1.0, places=9, msg=text)

    def test_a_thrashing_scores_higher_than_a_squeaker(self):
        double_bagel = glicko_score(parse_score("6-0 6-0"), for_player_a=True)
        squeaker = glicko_score(parse_score("7-6 7-6"), for_player_a=True)
        self.assertGreater(double_bagel, squeaker)
        self.assertEqual(double_bagel, 1.0)

    def test_every_win_still_beats_every_loss(self):
        worst_win = glicko_score(parse_score("7-6 6-7 10-8"), for_player_a=True)
        best_loss = glicko_score(parse_score("7-6 6-7 8-10"), for_player_a=True)
        self.assertGreater(worst_win, 0.5)
        self.assertLess(best_loss, 0.5)

    def test_margin_can_be_switched_off(self):
        p = parse_score("6-4 6-4")
        self.assertEqual(glicko_score(p, for_player_a=True, margin_weight=0.0), 1.0)

    def test_retirement_ignores_the_margin(self):
        p = parse_score("6-3 2-1 ret.")
        self.assertEqual(glicko_score(p, for_player_a=True), 1.0)


if __name__ == "__main__":
    unittest.main()
