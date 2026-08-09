"""Glicko-2, implemented from Mark Glickman's paper.

Why Glicko-2 rather than Elo: Elo tracks a single number and treats every
player's rating as equally trustworthy. In a club ladder people play a handful
of matches a year, so that assumption is wrong in a way you can feel -- someone
who has played twice sits next to someone who has played forty times and the
ladder pretends it knows both equally well.

Glicko-2 carries three numbers per player:

  rating      the skill estimate
  rd          rating deviation -- how unsure we are (shrinks with play, grows
              with inactivity)
  volatility  how erratic the player's results are, which controls how fast
              their rating is allowed to move

The practical effects: beating a strong, well-established opponent moves you a
lot; beating an unknown moves you little; a newcomer converges in a few matches
instead of twenty; and someone returning after a season away is treated as an
open question rather than as their year-old rating.

This module is pure maths -- no database, no I/O -- so it can be tested on its
own (see tests/test_glicko2.py, which checks it against the worked example in
Glickman's paper).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

# Glicko-2 works on an internal scale where 173.7178 rating points = 1 unit.
SCALE = 173.7178
CONVERGENCE = 1e-6
MAX_ITERATIONS = 100


@dataclass(frozen=True)
class Rating:
    """A player's rating state. Immutable: updates return a new Rating."""

    rating: float = 1500.0
    rd: float = 350.0
    volatility: float = 0.06

    # -- conversion to/from the internal Glicko-2 scale ------------------
    @property
    def mu(self) -> float:
        return (self.rating - 1500.0) / SCALE

    @property
    def phi(self) -> float:
        return self.rd / SCALE

    @classmethod
    def from_internal(cls, mu: float, phi: float, volatility: float) -> "Rating":
        return cls(rating=mu * SCALE + 1500.0, rd=phi * SCALE, volatility=volatility)


@dataclass(frozen=True)
class Result:
    """One match from the perspective of the player being updated.

    `score` is 1.0 for a win and 0.0 for a loss, but any value in between is
    valid -- that is how margin of victory is folded in (see scoring.py).
    """

    opponent: Rating
    score: float


def _g(phi: float) -> float:
    """Weight an opponent's contribution by how well we know their rating."""
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _expected(mu: float, opp_mu: float, opp_phi: float) -> float:
    """Probability the player beats this opponent."""
    return 1.0 / (1.0 + math.exp(-_g(opp_phi) * (mu - opp_mu)))


def _new_volatility(phi: float, v: float, delta: float, sigma: float, tau: float) -> float:
    """Solve for the new volatility using the Illinois variant of regula falsi.

    This is step 5 of Glickman's paper: find sigma' maximising the posterior,
    i.e. the root of f(x) below, where x = ln(sigma'^2).
    """
    a = math.log(sigma * sigma)
    phi2, delta2 = phi * phi, delta * delta

    def f(x: float) -> float:
        ex = math.exp(x)
        numerator = ex * (delta2 - phi2 - v - ex)
        denominator = 2.0 * (phi2 + v + ex) ** 2
        return numerator / denominator - (x - a) / (tau * tau)

    # Bracket the root.
    lo = a
    if delta2 > phi2 + v:
        hi = math.log(delta2 - phi2 - v)
    else:
        k = 1
        while f(a - k * tau) < 0 and k < MAX_ITERATIONS:
            k += 1
        hi = a - k * tau

    f_lo, f_hi = f(lo), f(hi)
    for _ in range(MAX_ITERATIONS):
        if abs(hi - lo) <= CONVERGENCE:
            break
        mid = lo + (lo - hi) * f_lo / (f_hi - f_lo)
        f_mid = f(mid)
        if f_mid * f_hi <= 0:
            lo, f_lo = hi, f_hi
        else:
            f_lo /= 2.0          # the Illinois tweak: stops one endpoint sticking
        hi, f_hi = mid, f_mid
    return math.exp(lo / 2.0)


def rate(
    player: Rating,
    results: Sequence[Result],
    *,
    tau: float = 0.5,
    min_rd: float = 30.0,
    max_rd: float = 350.0,
) -> Rating:
    """Update one player for one rating period.

    `results` is every match that player played in the period. An empty
    sequence means they sat the period out, which widens their RD.
    """
    if not results:
        return decay(player, max_rd=max_rd)

    mu, phi = player.mu, player.phi

    # Step 3: estimated variance of the rating based on the games played.
    v_inv = 0.0
    # Step 4: estimated improvement in rating.
    delta_sum = 0.0
    for res in results:
        opp_mu, opp_phi = res.opponent.mu, res.opponent.phi
        g = _g(opp_phi)
        e = _expected(mu, opp_mu, opp_phi)
        v_inv += g * g * e * (1.0 - e)
        delta_sum += g * (res.score - e)

    if v_inv == 0.0:
        # Degenerate: the outcome carried no information (astronomical rating
        # gap). Leave the rating alone rather than dividing by zero.
        return player

    v = 1.0 / v_inv
    delta = v * delta_sum

    # Step 5: new volatility.
    sigma_prime = _new_volatility(phi, v, delta, player.volatility, tau)

    # Step 6/7: pre-period RD bump, then shrink it by what we learned.
    phi_star = math.sqrt(phi * phi + sigma_prime * sigma_prime)
    phi_prime = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + v_inv)
    mu_prime = mu + phi_prime * phi_prime * delta_sum

    updated = Rating.from_internal(mu_prime, phi_prime, sigma_prime)
    return Rating(
        rating=updated.rating,
        rd=_clamp(updated.rd, min_rd, max_rd),
        volatility=sigma_prime,
    )


def decay(player: Rating, *, max_rd: float = 350.0) -> Rating:
    """Age a rating over a period the player did not play in.

    The rating itself is unchanged -- we have no new evidence -- but we become
    less sure it is still accurate.
    """
    phi_star = math.sqrt(player.phi * player.phi + player.volatility * player.volatility)
    return Rating(
        rating=player.rating,
        rd=min(phi_star * SCALE, max_rd),
        volatility=player.volatility,
    )


def win_probability(player: Rating, opponent: Rating) -> float:
    """Chance `player` beats `opponent`, accounting for both uncertainties.

    Used for the "expected result" hint on the challenge view.
    """
    combined_phi = math.sqrt(player.phi ** 2 + opponent.phi ** 2)
    return 1.0 / (1.0 + math.exp(-_g(combined_phi) * (player.mu - opponent.mu)))


def conservative(player: Rating, k: float = 2.0) -> float:
    """The number the ladder is actually sorted on.

    rating - k*RD is roughly "the rating we're confident they're at least
    worth". It keeps an unproven player from topping the ladder on one upset
    and rewards actually turning up to play.
    """
    return player.rating - k * player.rd


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def average(ratings: Iterable[Rating]) -> Rating:
    """Mean rating of a group -- used for club-wide stats only."""
    items = list(ratings)
    if not items:
        return Rating()
    n = len(items)
    return Rating(
        rating=sum(r.rating for r in items) / n,
        rd=sum(r.rd for r in items) / n,
        volatility=sum(r.volatility for r in items) / n,
    )
