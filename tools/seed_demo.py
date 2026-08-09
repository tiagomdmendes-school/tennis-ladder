#!/usr/bin/env python3
"""Fill a database with a plausible college season so the app has something to show.

Players get a hidden "true" strength -- separately for singles and doubles,
since they are not the same skill -- and matches are simulated from it. That
makes this useful for more than a demo: if the rating system works, each final
ladder should closely track the hidden order for that discipline
(tests/test_engine.py checks exactly that).

    python3 -m tools.seed_demo [path/to/ladder.db]
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from typing import Dict, List, Sequence, Tuple

from ladder import divisions as div
from ladder.config import CONFIG, DB_PATH
from ladder.service import LadderService
from ladder.storage import Database

# name, category, singles strength, doubles strength
ROSTER: List[Tuple[str, str, int, int]] = [
    ("Ana Silva",      div.WOMENS, 1780, 1700),
    ("Ben Okafor",     div.MENS,   1720, 1760),
    ("Chiara Rossi",   div.WOMENS, 1690, 1620),
    ("Devon Park",     div.MENS,   1640, 1700),
    ("Elif Demir",     div.WOMENS, 1600, 1660),
    ("Farid Haddad",   div.MENS,   1560, 1500),
    ("Grace Lin",      div.WOMENS, 1520, 1580),
    ("Hugo Mendes",    div.MENS,   1480, 1520),
    ("Ines Duarte",    div.WOMENS, 1440, 1400),
    ("Jonas Weber",    div.MENS,   1400, 1450),
    ("Kira Novak",     div.WOMENS, 1350, 1420),
    ("Liam Doyle",     div.MENS,   1300, 1340),
    ("Maya Torres",    div.WOMENS, 1620, 1560),
    ("Noah Fischer",   div.MENS,   1580, 1620),
]

SINGLES = 2      # index into a roster row
DOUBLES = 3


def _simulate_score(rng: random.Random, p_win: float) -> str:
    """A scoreline whose closeness reflects how one-sided the matchup was."""
    edge = abs(p_win - 0.5) * 2                     # 0 = even, 1 = mismatch
    if rng.random() < 0.04:
        return "6-3 2-1 ret." if p_win >= 0.5 else "1-2 3-6 ret."

    def set_score(dominant: bool) -> Tuple[int, int]:
        weights = [0.10 + edge * 0.25, 0.15 + edge * 0.15, 0.20,
                   0.22 - edge * 0.05, 0.18 - edge * 0.08, 0.15 - edge * 0.1]
        weights = [max(w, 0.01) for w in weights]
        loser_games = rng.choices([0, 1, 2, 3, 4, 5], weights=weights)[0]
        if rng.random() < 0.12:
            return (7, 6) if dominant else (6, 7)
        # At 5 games the set has to be won 7-5 -- 6-5 is not a tennis score.
        winner_games = 7 if loser_games == 5 else 6
        return (winner_games, loser_games) if dominant else (loser_games, winner_games)

    winner_is_a = rng.random() < p_win
    sets: List[str] = []
    a_sets = b_sets = 0
    while a_sets < 2 and b_sets < 2:
        a_takes = winner_is_a if rng.random() < 0.78 else not winner_is_a
        if a_sets + b_sets == 2:                    # deciding set
            a_takes = winner_is_a
            if rng.random() < 0.5:                  # many clubs play a super-TB
                sets.append("10-8" if a_takes else "8-10")
                break
        a, b = set_score(a_takes)
        sets.append(f"{a}-{b}")
        if a > b:
            a_sets += 1
        else:
            b_sets += 1
    return " ".join(sets)


def _p_win(gap: float) -> float:
    return 1 / (1 + 10 ** (-gap / 400))


def _pick_opponents(
    rng: random.Random, pool: Sequence[int], strengths: Dict[int, int], size: int
) -> Tuple[List[int], List[int]]:
    """Two sides of `size`, mostly close in strength but sometimes not.

    Cross-band matches matter: without them the ladder is a chain of local
    comparisons with nothing tying the ends together, and no rating system can
    order it confidently.
    """
    if len(pool) < size * 2:
        return [], []
    anchor = rng.choice(list(pool))
    if rng.random() < 0.25:
        candidates = [p for p in pool if p != anchor]
    else:
        near = sorted(pool, key=lambda p: abs(strengths[p] - strengths[anchor]))
        candidates = [p for p in near[1:size * 2 + 3] if p != anchor]
    if len(candidates) < size * 2 - 1:
        candidates = [p for p in pool if p != anchor]
    chosen = rng.sample(candidates, size * 2 - 1)
    everyone = [anchor] + chosen
    rng.shuffle(everyone)
    return everyone[:size], everyone[size:]


def seed(db_path: str = DB_PATH, *, weeks: int = 22, seed_value: int = 7,
         seasons: int = 2) -> Database:
    db = Database(db_path)
    if db.list_players():
        print("  Database already has players -- leaving it alone.")
        return db

    rng = random.Random(seed_value)
    service = LadderService(db, CONFIG)

    singles: Dict[int, int] = {}
    doubles: Dict[int, int] = {}
    category: Dict[int, str] = {}
    for name, cat, s_strength, d_strength in ROSTER:
        player = service.add_player(name, category=cat)
        singles[player.id] = s_strength
        doubles[player.id] = d_strength
        category[player.id] = cat

    men = [pid for pid, c in category.items() if c == div.MENS]
    women = [pid for pid, c in category.items() if c == div.WOMENS]

    plans = [
        (div.MENS_SINGLES, men, singles, 1),
        (div.WOMENS_SINGLES, women, singles, 1),
        (div.MENS_DOUBLES, men, doubles, 2),
        (div.WOMENS_DOUBLES, women, doubles, 2),
    ]

    total = 0
    start = date.today() - timedelta(weeks=weeks * seasons)
    # The bootstrap season starts "today" by default; these matches are
    # backdated, so correct it or the dates on screen make no sense.
    db.set_season_start(db.current_season().id, start.isoformat())

    for season_index in range(seasons):
        if season_index > 0:
            offset = start + timedelta(weeks=weeks * season_index)
            service.start_season(f"Season {season_index + 1}",
                                 offset.isoformat())

        base = start + timedelta(weeks=weeks * season_index)
        for week in range(weeks):
            when = base + timedelta(days=week * 7 + rng.randint(0, 6))
            if when > date.today():
                continue
            for division, pool, strengths, size in plans:
                for _ in range(rng.randint(1, 3)):
                    side_a, side_b = _pick_opponents(rng, pool, strengths, size)
                    if not side_a:
                        continue
                    gap = (sum(strengths[p] for p in side_a) / size
                           - sum(strengths[p] for p in side_b) / size)
                    total += _record(service, division, side_a, side_b,
                                     _simulate_score(rng, _p_win(gap)), when)

            # Mixed doubles: one player from each category per side. It gets
            # more matches per week than the others because everyone is
            # eligible for it -- twice the players need roughly twice the
            # matches to order them as confidently.
            for _ in range(rng.randint(3, 5)):
                if len(men) < 2 or len(women) < 2:
                    break
                m1, m2 = rng.sample(men, 2)
                w1, w2 = rng.sample(women, 2)
                side_a, side_b = [m1, w1], [m2, w2]
                gap = ((doubles[m1] + doubles[w1]) / 2
                       - (doubles[m2] + doubles[w2]) / 2)
                total += _record(service, div.MIXED_DOUBLES, side_a, side_b,
                                 _simulate_score(rng, _p_win(gap)), when)

    # Leave a couple awaiting confirmation so the confirm screen isn't empty.
    if len(men) >= 2:
        service.submit_result(
            division=div.MENS_SINGLES, side_a=[men[0]], side_b=[men[1]],
            score_text="6-4 6-3", played_on=date.today().isoformat(),
            submitted_by=men[0])
    if len(men) >= 2 and len(women) >= 2:
        service.submit_result(
            division=div.MIXED_DOUBLES, side_a=[men[0], women[0]],
            side_b=[men[1], women[1]], score_text="7-5 6-4",
            played_on=date.today().isoformat(), submitted_by=men[0])

    print(f"  Seeded {len(ROSTER)} players and {total} confirmed matches "
          f"across {seasons} season(s) and 5 divisions.")
    return db


def _record(service: LadderService, division: str, side_a, side_b,
            score: str, when: date) -> int:
    try:
        service.submit_result(
            division=division, side_a=side_a, side_b=side_b,
            score_text=score, played_on=when.isoformat(),
            auto_confirm=True, note="challenge match",
        )
        return 1
    except Exception as exc:                          # noqa: BLE001
        print(f"  skipped a simulated match: {exc}")
        return 0


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
