"""Rating doubles as individual players.

The problem: four people play, but only two ratings should move per side, and
each player's gain has to reflect *the team they were up against and the partner
they had*. Carrying a weak partner past strong opponents is a real achievement
and has to pay accordingly; coasting behind a strong partner should not.

The approach: reduce each player's doubles match to an ordinary Glicko-2 singles
update against a **virtual opponent**, chosen so that the rating gap the player
faces equals the gap between the two *teams*.

    d      = mean(your side) - mean(their side)      the team-level gap
    v_you  = your rating - d                         so (you - v_you) == d
    rd_v   = sqrt(sum of everyone else's RD^2) / n   their combined uncertainty

Worked example -- you 1600, weak partner 1200, both opponents 1500:

    d      = 1400 - 1500 = -100
    v_you  = 1600 + 100  = 1700     you are treated as facing a 1700 player

so winning pays as if you had beaten someone 100 points above you, which is
exactly what your team did. Meanwhile your partner's virtual opponent is 1300,
one hundred above *their* 1200 -- so both of you carry the **same team win
expectation**, and differ only in how far each of you moves, which Glicko-2
already scales by each player's own RD.

That shared-expectation property is why this formulation was chosen over an
ad-hoc "weak partner bonus": the credit falls out of the arithmetic, and the two
players' scores still sum to one, so no rating is created or destroyed.

Singles is the n = 1 case of the same formula -- the virtual opponent works out
to be the actual opponent -- so one code path serves both.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from .glicko2 import Rating, Result, _g


def team_rating(side: Sequence[Rating]) -> Rating:
    """A side's combined rating, on the same scale as an individual's.

    The mean is the right first-order estimate of a pair's strength, and it
    keeps team numbers comparable with player numbers. Uncertainty is combined
    as the uncertainty of that mean.
    """
    if not side:
        raise ValueError("A side needs at least one player.")
    n = len(side)
    rating = sum(r.rating for r in side) / n
    rd = math.sqrt(sum(r.rd ** 2 for r in side)) / n
    volatility = sum(r.volatility for r in side) / n
    return Rating(rating=rating, rd=rd, volatility=volatility)


def virtual_opponent(
    index: int, side: Sequence[Rating], opponents: Sequence[Rating]
) -> Rating:
    """The single opponent that makes `side[index]`'s update come out right.

    Its rating is placed so the gap this player sees equals the gap between the
    teams; its RD carries the uncertainty of the three other people involved
    (your partner blurs the comparison just as the opponents do).
    """
    if len(side) != len(opponents):
        raise ValueError("Both sides must field the same number of players.")
    n = len(side)

    gap = (sum(r.rating for r in side) / n) - (sum(r.rating for r in opponents) / n)

    others = [r for i, r in enumerate(side) if i != index] + list(opponents)
    rd = math.sqrt(sum(r.rd ** 2 for r in others)) / n

    player = side[index]
    return Rating(rating=player.rating - gap, rd=rd, volatility=player.volatility)


def match_results(
    side_a: Sequence[Rating],
    side_b: Sequence[Rating],
    score_a: float,
) -> Tuple[List[Result], List[Result]]:
    """One Glicko-2 Result per player, ready to drop into a rating period.

    `score_a` is side A's score in [0, 1] -- 1 for a win, or the margin-blended
    value from scoring.py. Side B's is its complement, so the two sides' credit
    always sums to one.

    Works for singles (one player per side) and doubles (two) alike.
    """
    score_b = 1.0 - score_a
    results_a = [
        Result(virtual_opponent(i, side_a, side_b), score_a)
        for i in range(len(side_a))
    ]
    results_b = [
        Result(virtual_opponent(i, side_b, side_a), score_b)
        for i in range(len(side_b))
    ]
    return results_a, results_b


def team_win_probability(side_a: Sequence[Rating], side_b: Sequence[Rating]) -> float:
    """Chance side A beats side B, accounting for everyone's uncertainty.

    Used for the expected-result hint and for partner chemistry, where a pair's
    performance is judged against what these four ratings predicted.
    """
    a, b = team_rating(side_a), team_rating(side_b)
    combined_phi = math.sqrt(a.phi ** 2 + b.phi ** 2)
    return 1.0 / (1.0 + math.exp(-_g(combined_phi) * (a.mu - b.mu)))
