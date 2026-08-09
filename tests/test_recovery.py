"""Does the system actually recover known skill from simulated results?

This is the test that matters most, because everything else could pass while
the ratings were quietly meaningless. Players are given a hidden strength --
separately for singles and doubles -- a season is simulated from it, and each
ladder has to rediscover the order for its own discipline.

One season is far too noisy to judge on: a player can go 13-4 on a 42% expected
win rate and nothing is wrong with the ratings when they believe it. So these
average over several simulated seasons and compare against the displacement you
would get from ordering at random.
"""

import contextlib
import io
import unittest

from ladder import divisions as div
from ladder.service import LadderService
from tests.helpers import make_config

SEEDS = range(1, 6)


def season(seed_value: int, seasons: int = 1):
    from tools.seed_demo import seed

    with contextlib.redirect_stdout(io.StringIO()):     # keep test output clean
        db = seed(":memory:", weeks=22, seed_value=seed_value, seasons=seasons)
    return db, LadderService(db, make_config())


def truth_for(division: str) -> dict:
    from tools.seed_demo import DOUBLES, ROSTER, SINGLES

    column = SINGLES if div.get(division).team_size == 1 else DOUBLES
    return {row[0]: row[column] for row in ROSTER}


def mean_displacement(found, truth) -> float:
    """Average places each player sits from where their hidden strength says."""
    ideal = [name for name, _ in sorted(truth.items(), key=lambda kv: -kv[1])
             if name in found]
    rank = {name: i for i, name in enumerate(ideal)}
    return sum(abs(rank[name] - i) for i, name in enumerate(found)) / len(found)


def random_baseline(n: int) -> float:
    """Mean displacement of a uniformly random ordering of n players.

    Exactly (n^2 - 1) / 3n. The threshold has to scale with division size:
    mixed doubles has twice as many players as the others, so the same absolute
    displacement there is a much better result.
    """
    return (n * n - 1) / (3 * n)


class TestSkillRecovery(unittest.TestCase):
    def test_every_division_beats_random_ordering(self):
        for division in div.DIVISION_ORDER:
            truth = truth_for(division)
            scores, sizes = [], []
            for seed_value in SEEDS:
                _, svc = season(seed_value)
                found = [e.player.name for e in svc.engine.ladder(division).entries]
                if len(found) < 6:
                    continue
                scores.append(mean_displacement(found, truth))
                sizes.append(len(found))
            self.assertTrue(scores, f"{division} produced no ladders")

            average = sum(scores) / len(scores)
            baseline = random_baseline(round(sum(sizes) / len(sizes)))
            # A floor, not a target. Some pairs on the roster sit 20 points
            # apart, which is a coin flip no rating system can resolve, so a
            # perfect ordering is not achievable even in principle.
            self.assertLess(
                average, baseline * 0.8,
                f"{division}: {average:.2f} places out vs {baseline:.2f} "
                f"for random ordering -- the ratings carry little information")

    def test_the_strongest_and_weakest_singles_players_are_found(self):
        truth = truth_for(div.MENS_SINGLES)
        top_hits = bottom_hits = 0
        for seed_value in SEEDS:
            _, svc = season(seed_value)
            entries = svc.engine.ladder(div.MENS_SINGLES).entries
            names = [e.player.name for e in entries]
            ordered = [n for n, _ in sorted(truth.items(), key=lambda kv: -kv[1])
                       if n in names]
            top_hits += names[0] in ordered[:3]
            bottom_hits += names[-1] in ordered[-3:]
        self.assertGreaterEqual(top_hits, len(SEEDS) - 1)
        self.assertGreaterEqual(bottom_hits, len(SEEDS) - 1)

    def test_doubles_ratings_follow_doubles_strength_not_singles(self):
        """The roster deliberately gives some players different singles and
        doubles strength. If doubles were just copying singles, this fails."""
        singles_truth = truth_for(div.MENS_SINGLES)
        doubles_truth = truth_for(div.MENS_DOUBLES)
        closer_to_doubles = 0
        for seed_value in SEEDS:
            _, svc = season(seed_value)
            found = [e.player.name for e in svc.engine.ladder(div.MENS_DOUBLES).entries]
            if len(found) < 6:
                continue
            closer_to_doubles += (mean_displacement(found, doubles_truth)
                                  <= mean_displacement(found, singles_truth))
        self.assertGreaterEqual(closer_to_doubles, 3,
                                "doubles ladder tracks singles strength too closely")

    def test_uncertainty_shrinks_for_players_who_turn_up(self):
        _, svc = season(1)
        entries = svc.engine.ladder(div.MENS_DOUBLES).entries
        busiest = max(entries, key=lambda e: e.played)
        quietest = min(entries, key=lambda e: e.played)
        self.assertLessEqual(busiest.rating.rd, quietest.rating.rd)
        self.assertFalse(busiest.provisional)

    def test_a_two_season_history_still_recovers_the_order(self):
        """Carry-over must not smear the signal across seasons."""
        truth = truth_for(div.WOMENS_SINGLES)
        scores = []
        for seed_value in SEEDS:
            _, svc = season(seed_value, seasons=2)
            found = [e.player.name for e in
                     svc.engine.ladder(div.WOMENS_SINGLES).entries]
            if len(found) >= 6:
                scores.append(mean_displacement(found, truth))
        self.assertLess(sum(scores) / len(scores), random_baseline(7) * 0.8)


if __name__ == "__main__":
    unittest.main()
