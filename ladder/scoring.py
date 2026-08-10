"""Turn a tennis scoreline into something the rating engine can use.

Two jobs:

1. Parse what people actually type -- "6-4 3-6 10-8", "7-6(5) 6-2",
   "6-3 2-1 ret.", "w/o" -- into sets, and work out who won.

   Club formats vary a lot and the parser is deliberately permissive about
   set lengths, so all of these are accepted as complete matches: a single
   set ("6-4"), two sets and a deciding match tie-break ("6-4 4-6 10-8"),
   a best-of-three ("6-4 3-6 6-2"), an eight-game pro set ("8-6") and Fast4
   ("4-2"). Anything with the sets level and no decider is rejected instead,
   since that is an unfinished match rather than a format.
2. Convert the result into a score in [0, 1] for Glicko-2, blending the
   win/loss outcome with the margin so that grinding out 7-6 7-6 is not
   recorded as the same performance as 6-0 6-0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

SET_RE = re.compile(
    r"^(\d{1,2})\s*[-:/]\s*(\d{1,2})(?:\s*\(\s*(\d{1,2})\s*\))?$")
RETIRE_RE = re.compile(r"\b(ret|retired|rtd)\b\.?", re.IGNORECASE)
WALKOVER_RE = re.compile(r"\b(w/?o|walkover|default|def)\b\.?", re.IGNORECASE)


class ScoreError(ValueError):
    """The scoreline could not be understood, with a message for the user."""


@dataclass
class ParsedScore:
    sets: List[Tuple[int, int]] = field(default_factory=list)
    # Points the tie-break loser scored, per set; None where a set had no
    # tie-break. Kept so 7-6(5) survives a round trip and still reads as a
    # tie-break afterwards -- it says nothing to the rating, but it is the
    # difference between "we had a close one" and a bare 7-6.
    tiebreaks: List[Optional[int]] = field(default_factory=list)
    sets_a: int = 0
    sets_b: int = 0
    games_a: int = 0
    games_b: int = 0
    retired: bool = False
    walkover: bool = False
    # a_won is None only if the result is genuinely undecided (rejected later)
    a_won: Optional[bool] = None

    @property
    def normalised(self) -> str:
        """Canonical text form, e.g. '6-4 7-6(5) 10-8 ret.'"""
        pieces = []
        for index, (a, b) in enumerate(self.sets):
            tiebreak = self.tiebreaks[index] if index < len(self.tiebreaks) else None
            pieces.append(f"{a}-{b}({tiebreak})" if tiebreak is not None
                          else f"{a}-{b}")
        text = " ".join(pieces)
        if self.walkover:
            return (text + " w/o").strip()
        if self.retired:
            return (text + " ret.").strip()
        return text


def parse_score(text: str, *, match_tiebreak_in_decider: bool = True) -> ParsedScore:
    """Parse a scoreline written from player A's perspective.

    A's games always come first: '6-4 6-2' means A won, '4-6 2-6' means B won.
    """
    if text is None:
        raise ScoreError("Enter a score.")

    raw = text.strip()
    if not raw:
        raise ScoreError(
            "Enter a score. One set is fine ('6-4'), as is a full match "
            "('6-4 3-6 10-8')."
        )

    walkover = bool(WALKOVER_RE.search(raw))
    retired = bool(RETIRE_RE.search(raw))
    cleaned = WALKOVER_RE.sub(" ", RETIRE_RE.sub(" ", raw)).replace(",", " ")

    sets: List[Tuple[int, int]] = []
    tiebreaks: List[Optional[int]] = []
    for token in cleaned.split():
        match = SET_RE.match(token)
        if not match:
            raise ScoreError(
                f"Couldn't read {token!r}. Write each set as '6-4', separated by "
                "spaces, e.g. '6-4 3-6 10-8'."
            )
        sets.append((int(match.group(1)), int(match.group(2))))
        tiebreaks.append(int(match.group(3)) if match.group(3) else None)

    if not sets and not walkover:
        raise ScoreError("Enter at least one set, for example '6-4'.")

    parsed = ParsedScore(sets=sets, tiebreaks=tiebreaks, retired=retired,
                         walkover=walkover)

    if walkover:
        # No tennis was played. Direction comes from the sets if any were given
        # (e.g. '6-0 w/o'), otherwise the caller must say who advanced.
        parsed.a_won = _walkover_direction(sets)
        return parsed

    for index, (a, b) in enumerate(sets):
        if a == b:
            raise ScoreError(f"Set {index + 1} is {a}-{b}: a set can't be tied.")
        if a > b:
            parsed.sets_a += 1
        else:
            parsed.sets_b += 1

        a_games, b_games = _set_games(sets, index, match_tiebreak_in_decider)
        parsed.games_a += a_games
        parsed.games_b += b_games

    if retired:
        # Whoever was ahead when the other pulled out is credited with the win.
        # If sets were level, the current set decides it.
        parsed.a_won = _retirement_direction(parsed)
    elif parsed.sets_a == parsed.sets_b:
        raise ScoreError(
            f"{parsed.sets_a}-{parsed.sets_b} in sets isn't a completed match. "
            "Add the deciding set, or mark it 'ret.' if someone retired."
        )
    else:
        parsed.a_won = parsed.sets_a > parsed.sets_b

    return parsed


def _set_games(
    sets: List[Tuple[int, int]], index: int, match_tiebreak_in_decider: bool
) -> Tuple[int, int]:
    """Games credited for one set.

    A deciding-set match tie-break ('10-8') is worth one game, not ten -- it
    replaces a set, so counting every point as a game would let a third-set
    breaker outweigh the two real sets that came before it.
    """
    a, b = sets[index]
    is_decider = index == len(sets) - 1 and index >= 2
    if match_tiebreak_in_decider and is_decider and max(a, b) >= 10:
        return (1, 0) if a > b else (0, 1)
    return a, b


def _walkover_direction(sets: List[Tuple[int, int]]) -> Optional[bool]:
    if not sets:
        return None
    a_sets = sum(1 for a, b in sets if a > b)
    b_sets = sum(1 for a, b in sets if b > a)
    if a_sets == b_sets:
        return None
    return a_sets > b_sets


def _retirement_direction(parsed: ParsedScore) -> bool:
    if parsed.sets_a != parsed.sets_b:
        return parsed.sets_a > parsed.sets_b
    if not parsed.sets:
        raise ScoreError("A retirement needs at least a partial score, e.g. '6-3 2-1 ret.'")
    last_a, last_b = parsed.sets[-1]
    if last_a == last_b:
        raise ScoreError("Sets are level and the last set is tied -- who was ahead?")
    return last_a > last_b


def flip_score(text: str) -> str:
    """Rewrite a scoreline from the other player's point of view.

    Scores are stored from player A's perspective, but results read as
    "winner def. loser", so whenever the winner is player B the score has to be
    turned around or it looks like the winner lost.

        '6-7 1-6'  ->  '7-6 6-1'
    """
    parts = []
    for token in (text or "").split():
        match = SET_RE.match(token)
        if match:
            # The bracket is the tie-break loser's points, which is the same
            # number whichever way round the set is written.
            tiebreak = f"({match.group(3)})" if match.group(3) else ""
            parts.append(f"{match.group(2)}-{match.group(1)}{tiebreak}")
        else:
            parts.append(token)          # 'ret.', 'w/o' and anything unparsed
    return " ".join(parts)


def glicko_score(
    parsed: ParsedScore,
    *,
    for_player_a: bool,
    margin_weight: float = 0.20,
) -> float:
    """Convert a result into a Glicko-2 score in [0, 1].

    Pure win/loss is 1 or 0. We blend in the share of games won so that the
    scoreline carries some information:

        score = (1 - w) * outcome + w * games_share

    With the default w = 0.2:

        6-0 6-0 win  -> 1.000    (total domination)
        6-4 6-4 win  -> 0.933
        7-6 7-6 win  -> 0.908    (still clearly a win, worth a little less)

    The two players' scores always sum to 1, so no rating is created or
    destroyed by the margin term.

    Retirements and walkovers ignore margin entirely -- an abandoned match says
    nothing reliable about the gap between the players, so it counts as a plain
    win.
    """
    if parsed.a_won is None:
        raise ScoreError("This result has no winner.")

    won = parsed.a_won if for_player_a else not parsed.a_won
    outcome = 1.0 if won else 0.0

    if parsed.retired or parsed.walkover:
        return outcome

    total_games = parsed.games_a + parsed.games_b
    if total_games == 0:
        return outcome

    own_games = parsed.games_a if for_player_a else parsed.games_b
    games_share = own_games / total_games
    weight = max(0.0, min(1.0, margin_weight))
    return (1.0 - weight) * outcome + weight * games_share
