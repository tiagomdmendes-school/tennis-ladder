"""Tournament draws: seeded knockout brackets and round robins.

Pure structure only -- who plays whom, in which round, and how a result moves
someone along. No database, no dates, so the fiddly parts (seeding, byes,
rotation) can be tested directly.

Two styles, because they suit different sized fields:

* **Single elimination** -- a seeded bracket. Fast and dramatic, but half the
  entrants play once and are done.
* **Round robin** -- everyone plays everyone. Better for a small club: nobody
  is knocked out on one bad afternoon, and it feeds the ladder far more results.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

ELIMINATION = "elimination"
ROUND_ROBIN = "round_robin"
STYLES = (ELIMINATION, ROUND_ROBIN)

LADDER_SEEDING = "ladder"
RANDOM_SEEDING = "random"
SEEDINGS = (LADDER_SEEDING, RANDOM_SEEDING)

STYLE_LABELS = {
    ELIMINATION: "Single elimination",
    ROUND_ROBIN: "Round robin",
}
SEEDING_LABELS = {
    LADDER_SEEDING: "By ladder ranking",
    RANDOM_SEEDING: "Random draw",
}


@dataclass
class Pairing:
    """One match in a draw, before anyone has played."""

    round_no: int
    slot: int
    player_a: Optional[int]      # None means "waiting on an earlier round"
    player_b: Optional[int]

    @property
    def is_bye(self) -> bool:
        """One side is empty in the first round -- the other walks through."""
        return (self.player_a is None) != (self.player_b is None)

    @property
    def occupant(self) -> Optional[int]:
        return self.player_a if self.player_a is not None else self.player_b


@dataclass
class Draw:
    style: str
    rounds: List[List[Pairing]] = field(default_factory=list)
    round_names: List[str] = field(default_factory=list)

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    def all_pairings(self) -> List[Pairing]:
        return [p for r in self.rounds for p in r]


# --------------------------------------------------------------------- seeding
def seed_positions(size: int) -> List[int]:
    """Standard bracket order for a power-of-two draw.

    Builds the classic pattern by reflection, so the top two seeds can only
    meet in the final, the top four only in the semis, and so on:

        2 -> [1, 2]
        4 -> [1, 4, 2, 3]
        8 -> [1, 8, 4, 5, 2, 7, 3, 6]
    """
    order = [1]
    while len(order) < size:
        width = len(order) * 2
        reflected: List[int] = []
        for seed in order:
            reflected.append(seed)
            reflected.append(width + 1 - seed)
        order = reflected
    return order


def bracket_size(entrants: int) -> int:
    """Next power of two at or above the field size."""
    if entrants < 2:
        return 2
    return 2 ** math.ceil(math.log2(entrants))


def order_entrants(
    player_ids: Sequence[int], seeding: str, rng: Optional[random.Random] = None
) -> List[int]:
    """Put entrants in seed order: 1st is the top seed.

    `player_ids` arrives already in ladder order, so seeding by ladder is just
    keeping it. A random draw shuffles, which is what you want when the ladder
    is young and its order isn't worth much yet.
    """
    entrants = list(player_ids)
    if seeding == RANDOM_SEEDING:
        (rng or random.Random()).shuffle(entrants)
    return entrants


# ----------------------------------------------------------------- elimination
def elimination_round_name(remaining: int) -> str:
    return {2: "Final", 4: "Semi-finals", 8: "Quarter-finals"}.get(
        remaining, f"Round of {remaining}")


def build_elimination(player_ids: Sequence[int]) -> Draw:
    """A seeded knockout draw, byes included.

    Entrants must already be in seed order. Anyone whose notional opponent
    doesn't exist (a field that isn't a power of two) gets a bye, and byes go
    to the top seeds, which is the point of seeding them.
    """
    entrants = list(player_ids)
    if len(entrants) < 2:
        raise ValueError("A tournament needs at least two players.")

    size = bracket_size(len(entrants))
    # seed number -> player id, with seeds past the field left empty
    by_seed: Dict[int, Optional[int]] = {
        seed: (entrants[seed - 1] if seed <= len(entrants) else None)
        for seed in range(1, size + 1)
    }
    ordered = [by_seed[seed] for seed in seed_positions(size)]

    draw = Draw(style=ELIMINATION)
    first = [
        Pairing(0, slot, ordered[slot * 2], ordered[slot * 2 + 1])
        for slot in range(size // 2)
    ]
    draw.rounds.append(first)
    draw.round_names.append(elimination_round_name(size))

    remaining = size // 2
    round_no = 1
    while remaining >= 2:
        draw.rounds.append(
            [Pairing(round_no, slot, None, None) for slot in range(remaining // 2)])
        draw.round_names.append(elimination_round_name(remaining))
        remaining //= 2
        round_no += 1
    return draw


def parent_slot(round_no: int, slot: int) -> Tuple[int, int, str]:
    """Where a winner goes next: (round, slot, which side)."""
    return round_no + 1, slot // 2, "a" if slot % 2 == 0 else "b"


# ---------------------------------------------------------------- round robin
def build_round_robin(player_ids: Sequence[int]) -> Draw:
    """Everyone plays everyone, spread over rounds by the circle method.

    Each round pairs the whole field at once where possible, so a round is a
    sensible unit to put a deadline on. An odd field means one player sits out
    each round.
    """
    entrants = list(player_ids)
    if len(entrants) < 2:
        raise ValueError("A tournament needs at least two players.")

    # A placeholder makes an odd field even; whoever draws it sits that round.
    field_ids: List[Optional[int]] = list(entrants)
    if len(field_ids) % 2:
        field_ids.append(None)

    count = len(field_ids)
    draw = Draw(style=ROUND_ROBIN)
    rotating = field_ids[1:]

    for round_no in range(count - 1):
        line_up = [field_ids[0]] + rotating
        pairings = []
        slot = 0
        for i in range(count // 2):
            home, away = line_up[i], line_up[count - 1 - i]
            if home is None or away is None:
                continue                      # the sit-out; not a match
            pairings.append(Pairing(round_no, slot, home, away))
            slot += 1
        draw.rounds.append(pairings)
        draw.round_names.append(f"Round {round_no + 1}")
        rotating = [rotating[-1]] + rotating[:-1]
    return draw


# ------------------------------------------------------------------ standings
@dataclass
class Standing:
    player_id: int
    played: int = 0
    won: int = 0
    lost: int = 0
    games_won: int = 0
    games_lost: int = 0

    @property
    def games_diff(self) -> int:
        return self.games_won - self.games_lost

    @property
    def record(self) -> str:
        return f"{self.won}-{self.lost}"


def standings(
    player_ids: Sequence[int],
    results: Sequence[dict],
) -> List[Standing]:
    """Round-robin table.

    `results` are dicts with winner, loser, winner_games, loser_games. Ranked
    on wins, then head-to-head between the tied players, then games difference
    -- head-to-head first because "but I beat you" is the argument that
    actually gets had.
    """
    table = {pid: Standing(pid) for pid in player_ids}
    beaten: Dict[int, set] = {pid: set() for pid in player_ids}

    for result in results:
        winner, loser = result["winner"], result["loser"]
        if winner not in table or loser not in table:
            continue
        table[winner].played += 1
        table[winner].won += 1
        table[winner].games_won += result.get("winner_games", 0)
        table[winner].games_lost += result.get("loser_games", 0)
        table[loser].played += 1
        table[loser].lost += 1
        table[loser].games_won += result.get("loser_games", 0)
        table[loser].games_lost += result.get("winner_games", 0)
        beaten[winner].add(loser)

    def sort_key(standing: Standing):
        # Head-to-head only settles it between players on equal wins, so it
        # enters the key as "how many of my equals did I beat".
        peers = [s for s in table.values()
                 if s.won == standing.won and s.player_id != standing.player_id]
        head_to_head = sum(1 for peer in peers
                           if peer.player_id in beaten[standing.player_id])
        return (-standing.won, -head_to_head, -standing.games_diff)

    return sorted(table.values(), key=sort_key)
