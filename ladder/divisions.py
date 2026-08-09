"""The five ladders, and who is allowed to play in each.

A club that plays men's, women's and mixed needs separate ladders, and a player
carries a separate rating in each: being #2 in men's singles says very little
about how you play mixed doubles. Divisions never interact except through
seeding (see engine.py), so each one is its own competition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# Player categories. `unspecified` is the default so that adding a player never
# forces a decision at the wrong moment -- it just limits which divisions they
# can be entered into until an admin sets it.
MENS = "mens"
WOMENS = "womens"
UNSPECIFIED = "unspecified"
CATEGORIES = (MENS, WOMENS, UNSPECIFIED)

CATEGORY_LABELS = {
    MENS: "Men's",
    WOMENS: "Women's",
    UNSPECIFIED: "Unspecified",
}


@dataclass(frozen=True)
class Division:
    key: str
    label: str
    short: str
    team_size: int            # 1 = singles, 2 = doubles
    category: str             # MENS, WOMENS, or "mixed"
    # The singles division a player's rating is seeded from when they first
    # appear here. None for the singles divisions themselves.
    seed_from: Optional[str]

    @property
    def is_doubles(self) -> bool:
        return self.team_size == 2

    @property
    def is_mixed(self) -> bool:
        return self.category == "mixed"

    @property
    def players_per_match(self) -> int:
        return self.team_size * 2


MENS_SINGLES = "mens_singles"
WOMENS_SINGLES = "womens_singles"
MENS_DOUBLES = "mens_doubles"
WOMENS_DOUBLES = "womens_doubles"
MIXED_DOUBLES = "mixed_doubles"

DIVISIONS: Dict[str, Division] = {
    MENS_SINGLES: Division(
        MENS_SINGLES, "Men's Singles", "MS", 1, MENS, None),
    WOMENS_SINGLES: Division(
        WOMENS_SINGLES, "Women's Singles", "WS", 1, WOMENS, None),
    MENS_DOUBLES: Division(
        MENS_DOUBLES, "Men's Doubles", "MD", 2, MENS, MENS_SINGLES),
    WOMENS_DOUBLES: Division(
        WOMENS_DOUBLES, "Women's Doubles", "WD", 2, WOMENS, WOMENS_SINGLES),
    MIXED_DOUBLES: Division(
        MIXED_DOUBLES, "Mixed Doubles", "XD", 2, "mixed", None),
}

DIVISION_ORDER: Sequence[str] = (
    MENS_SINGLES, WOMENS_SINGLES, MENS_DOUBLES, WOMENS_DOUBLES, MIXED_DOUBLES,
)


class DivisionError(ValueError):
    """A lineup that doesn't belong in this division, phrased for the user."""


def get(key: str) -> Division:
    try:
        return DIVISIONS[key]
    except KeyError:
        raise DivisionError(f"{key!r} is not one of the club's divisions.") from None

def is_division(key: str) -> bool:
    return key in DIVISIONS


def all_divisions() -> List[Division]:
    return [DIVISIONS[key] for key in DIVISION_ORDER]


def seed_division_for(division_key: str, category: str) -> Optional[str]:
    """Which singles ladder a player's rating in `division_key` is seeded from.

    Mixed doubles has no singles equivalent, so it seeds from whichever singles
    ladder the player's own category puts them in.
    """
    division = get(division_key)
    if division.seed_from:
        return division.seed_from
    if division.is_mixed:
        if category == MENS:
            return MENS_SINGLES
        if category == WOMENS:
            return WOMENS_SINGLES
    return None


def validate_lineup(
    division_key: str,
    side_a: Sequence,
    side_b: Sequence,
    *,
    override: bool = False,
) -> None:
    """Check a match's lineup against the division's rules.

    `side_a` / `side_b` are sequences of objects with `.id`, `.name` and
    `.category`. Raises DivisionError with a message aimed at whoever is
    entering the result.

    Category rules are enforced rather than merely warned about, because a
    men's singles match with the wrong player in it is nearly always a
    mis-click, and one bad row quietly distorts a ladder for weeks. `override`
    exists for the genuine exceptions and is admin-only.
    """
    division = get(division_key)

    for side, label in ((side_a, "first"), (side_b, "second")):
        if len(side) != division.team_size:
            expected = "one player" if division.team_size == 1 else "two players"
            raise DivisionError(
                f"{division.label} needs {expected} per side -- the {label} side "
                f"has {len(side)}."
            )

    everyone = list(side_a) + list(side_b)
    ids = [p.id for p in everyone]
    if len(set(ids)) != len(ids):
        raise DivisionError("The same player appears twice in this match.")

    if override:
        return

    if division.is_mixed:
        for side, label in ((side_a, "first"), (side_b, "second")):
            categories = sorted(p.category for p in side)
            if categories != [MENS, WOMENS]:
                names = " and ".join(p.name for p in side)
                raise DivisionError(
                    f"Mixed doubles needs one player from each category on every "
                    f"side. The {label} side is {names}. "
                    "Set their categories in Admin, or tick the admin override."
                )
        return

    wrong = [p for p in everyone if p.category != division.category]
    if wrong:
        names = ", ".join(p.name for p in wrong)
        expected = CATEGORY_LABELS[division.category]
        raise DivisionError(
            f"{division.label} is a {expected.lower()} division, but {names} "
            f"{'is' if len(wrong) == 1 else 'are'} not set to {expected}. "
            "Fix their category in Admin, or tick the admin override."
        )


def divisions_for_category(category: str) -> List[Division]:
    """The divisions a player of this category can normally enter."""
    if category == UNSPECIFIED:
        return []
    return [
        d for d in all_divisions()
        if d.category == category or d.is_mixed
    ]
