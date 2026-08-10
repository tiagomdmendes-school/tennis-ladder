"""When people can play, and when two of them can play each other.

The whole feature lives or dies on whether anyone fills it in, so the model is
deliberately coarse and forgiving:

* You set your **usual week** once -- "Tuesdays 3-6, Thursdays after 4". That's
  the thing a class schedule actually gives you, and it doesn't go stale.
* Anything that differs is an **exception on one date**: block a slot you
  normally have, or add one you normally don't. One tap, and only for the
  weeks coming up.

Everything here is pure interval arithmetic over minutes-from-midnight, with no
database access, so the awkward parts (overlaps, subtraction, merging) can be
tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Interval = Tuple[int, int]          # (start, end) in minutes from midnight

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday")
WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Slot:
    """A concrete window on a real date."""

    on: date
    start: int
    end: int

    @property
    def minutes(self) -> int:
        return self.end - self.start

    @property
    def starts_at(self) -> datetime:
        return datetime.combine(self.on, datetime.min.time()) + timedelta(minutes=self.start)

    def label(self) -> str:
        return f"{self.on.strftime('%a %d %b')} {clock(self.start)}-{clock(self.end)}"


def clock(minutes: int) -> str:
    """1020 -> '5:00pm'. Twelve-hour, because that's how people talk."""
    minutes %= 24 * 60
    hour, minute = divmod(minutes, 60)
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    return f"{display}:{minute:02d}{suffix}"


def parse_clock(text: str) -> int:
    """'17:00', '5pm', '5:30pm' -> minutes from midnight."""
    raw = (text or "").strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("Enter a time, like 5pm or 17:00.")
    suffix = ""
    if raw.endswith("am") or raw.endswith("pm"):
        suffix, raw = raw[-2:], raw[:-2]
    hour, _, minute = raw.partition(":")
    try:
        hours, minutes = int(hour), int(minute or 0)
    except ValueError:
        raise ValueError(f"{text!r} isn't a time. Try 5pm or 17:00.") from None
    if suffix == "pm" and hours < 12:
        hours += 12
    if suffix == "am" and hours == 12:
        hours = 0
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"{text!r} isn't a time. Try 5pm or 17:00.")
    return hours * 60 + minutes


# --------------------------------------------------------------- interval maths
def merge(intervals: Iterable[Interval]) -> List[Interval]:
    """Sort and coalesce overlapping or touching intervals."""
    ordered = sorted((s, e) for s, e in intervals if e > s)
    if not ordered:
        return []
    out = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = out[-1]
        if start <= last_end:                      # overlapping or adjacent
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


def subtract(base: Sequence[Interval], holes: Sequence[Interval]) -> List[Interval]:
    """Everything in `base` that isn't covered by `holes`."""
    out = list(merge(base))
    for hole_start, hole_end in merge(holes):
        next_round: List[Interval] = []
        for start, end in out:
            if hole_end <= start or hole_start >= end:
                next_round.append((start, end))    # no overlap
                continue
            if start < hole_start:
                next_round.append((start, hole_start))
            if hole_end < end:
                next_round.append((hole_end, end))
        out = next_round
    return out


def intersect(a: Sequence[Interval], b: Sequence[Interval]) -> List[Interval]:
    """The windows both sides have free."""
    out: List[Interval] = []
    for a_start, a_end in merge(a):
        for b_start, b_end in merge(b):
            start, end = max(a_start, b_start), min(a_end, b_end)
            if end > start:
                out.append((start, end))
    return merge(out)


def long_enough(intervals: Sequence[Interval], minutes: int) -> List[Interval]:
    return [(s, e) for s, e in intervals if e - s >= minutes]


# ------------------------------------------------------------------ the model
@dataclass
class Availability:
    """One player's schedule: a weekly pattern plus dated exceptions."""

    # weekday index (Mon=0) -> intervals they are normally free
    weekly: Dict[int, List[Interval]]
    # ISO date -> intervals blocked out that day ("can't make it")
    blocked: Dict[str, List[Interval]]
    # ISO date -> extra intervals free that day ("actually I can this Saturday")
    extra: Dict[str, List[Interval]]

    @classmethod
    def empty(cls) -> "Availability":
        return cls(weekly={}, blocked={}, extra={})

    def on(self, day: date) -> List[Interval]:
        """Resolved free intervals for a real date.

        The usual week, minus anything blocked that day, plus any one-off
        additions. Blocks are applied before additions so that adding a slot
        back is always possible.
        """
        key = day.isoformat()
        base = merge(list(self.weekly.get(day.weekday(), []))
                     + list(self.extra.get(key, [])))
        return subtract(base, self.blocked.get(key, []))

    def has_any(self) -> bool:
        return any(self.weekly.values()) or bool(self.extra)


def free_slots(
    availability: Availability, day: date, *, minutes: int = 0
) -> List[Interval]:
    windows = availability.on(day)
    return long_enough(windows, minutes) if minutes else windows


def mutual_slots(
    a: Availability,
    b: Availability,
    *,
    minutes: int,
    start_day: Optional[date] = None,
    days: int = 14,
    limit: int = 6,
    not_after: Optional[date] = None,
    earliest: Optional[datetime] = None,
) -> List[Slot]:
    """Times both players are free for long enough, soonest first.

    `minutes` is how long the chosen match format takes -- a gap that doesn't
    fit the format isn't a suggestion, it's a wasted trip to the courts.
    """
    start_day = start_day or date.today()
    earliest = earliest or datetime.now()
    found: List[Slot] = []

    for offset in range(days):
        day = start_day + timedelta(days=offset)
        if not_after and day > not_after:
            break
        overlap = long_enough(intersect(a.on(day), b.on(day)), minutes)
        for start, end in overlap:
            slot = Slot(day, start, end)
            # Don't suggest a time that has already passed today.
            if slot.starts_at < earliest:
                shifted = _shift_into_future(slot, earliest, minutes)
                if shifted is None:
                    continue
                slot = shifted
            found.append(slot)
            if len(found) >= limit:
                return found
    return found


# Suggested start times are rounded up to a multiple of this, so a window that
# is already underway becomes a clean, proposable time.
SUGGESTION_GRANULARITY = 15


def _shift_into_future(slot: Slot, earliest: datetime, minutes: int) -> Optional[Slot]:
    """Trim a window that has already started, if enough of it is left.

    The start is rounded *up* to the next quarter hour rather than set to the
    current minute. Two reasons: "2:29pm" is not a time anyone arranges to meet
    at, and more importantly a suggestion of exactly-now is stale the instant
    it's rendered -- by the time someone clicks it, it is in the past and gets
    rejected.
    """
    if earliest.date() != slot.on:
        return None
    minutes_now = earliest.hour * 60 + earliest.minute
    start = max(slot.start, minutes_now)
    start = -(-start // SUGGESTION_GRANULARITY) * SUGGESTION_GRANULARITY
    if slot.end - start < minutes:
        return None
    return Slot(slot.on, start, slot.end)


# ----------------------------------------------------------------- the grid UI
def slot_grid(day_start: int, day_end: int, step: int) -> List[Interval]:
    """The blocks the availability grid is made of."""
    step = max(15, step)
    return [(t, min(t + step, day_end))
            for t in range(day_start, day_end, step)
            if min(t + step, day_end) > t]


def intervals_from_slots(chosen: Iterable[str]) -> Dict[int, List[Interval]]:
    """Turn ticked grid boxes ('2-540-600') back into merged weekly intervals.

    The grid posts one checkbox per (weekday, block); contiguous ticks become
    a single interval so that "free all Tuesday afternoon" is stored once
    rather than as five adjacent rows.
    """
    by_day: Dict[int, List[Interval]] = {}
    for token in chosen:
        try:
            weekday, start, end = (int(part) for part in token.split("-"))
        except ValueError:
            continue
        if 0 <= weekday <= 6 and end > start:
            by_day.setdefault(weekday, []).append((start, end))
    return {day: merge(intervals) for day, intervals in by_day.items()}


def covers(intervals: Sequence[Interval], block: Interval) -> bool:
    """Is this grid block entirely inside the player's free time?"""
    start, end = block
    return any(s <= start and end <= e for s, e in merge(intervals))


def describe_week(weekly: Dict[int, List[Interval]]) -> str:
    """'Tue 3:00pm-6:00pm, Thu 4:00pm-10:00pm' -- for showing at a glance."""
    parts = []
    for day in range(7):
        for start, end in merge(weekly.get(day, [])):
            parts.append(f"{WEEKDAY_SHORT[day]} {clock(start)}-{clock(end)}")
    return ", ".join(parts) if parts else "nothing set yet"
