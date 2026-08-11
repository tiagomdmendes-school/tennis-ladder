"""SQLite persistence.

Only facts are stored: players, seasons, and the match results people submit.
Ratings are never stored -- they are recomputed from the match history on
demand (see engine.py). That is a deliberate choice: if a result is corrected
or deleted six weeks later, every rating downstream of it is right again
immediately, with no drift and no migration.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from .availability import Availability
from .config import DATA_DIR, DB_PATH
from .divisions import UNSPECIFIED
from .migrations import hash_pin, migrate, verify_pin

PENDING = "pending"
CONFIRMED = "confirmed"
REJECTED = "rejected"

SIDE_A = "a"
SIDE_B = "b"

# Match request lifecycle.
REQUEST_PENDING = "pending"
REQUEST_ACCEPTED = "accepted"
REQUEST_DECLINED = "declined"
REQUEST_CANCELLED = "cancelled"
REQUEST_PLAYED = "played"


@dataclass
class MatchRequest:
    """A proposed time for a match, awaiting the opponent's answer."""

    id: int
    division: str
    from_player: int
    to_player: int
    starts_at: str
    minutes: int
    match_format: str
    status: str
    message: str
    tournament_match_id: Optional[int]
    created_at: str
    responded_at: Optional[str]

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.starts_at)

    @property
    def is_past(self) -> bool:
        return self.when < datetime.now()

    def other(self, player_id: int) -> int:
        return self.to_player if player_id == self.from_player else self.from_player


@dataclass
class Player:
    id: int
    name: str
    email: str
    category: str
    active: bool
    joined_on: str
    # False until they've signed in once and chosen a PIN.
    pin_set: bool = False
    notify_confirm: bool = True
    notify_result: bool = True
    notify_weekly: bool = False
    notify_season: bool = True
    notify_request: bool = True

    def wants(self, kind: str) -> bool:
        """Whether this player opted in to a notification type."""
        return bool(getattr(self, f"notify_{kind}", False)) and bool(self.email)


@dataclass
class Tournament:
    id: int
    name: str
    division: str
    season_id: int
    style: str
    seeding: str
    match_format: str
    status: str          # setup -> running -> complete
    created_at: str

    @property
    def is_running(self) -> bool:
        return self.status == "running"


@dataclass
class TournamentRound:
    round_no: int
    name: str
    deadline: Optional[str]

    @property
    def is_overdue(self) -> bool:
        if not self.deadline:
            return False
        return datetime.strptime(self.deadline, "%Y-%m-%d").date() < date.today()


@dataclass
class TournamentMatch:
    id: int
    tournament_id: int
    round_no: int
    slot: int
    player_a: Optional[int]
    player_b: Optional[int]
    winner_id: Optional[int]
    match_id: Optional[int]
    status: str          # pending | ready | played | bye

    @property
    def is_ready(self) -> bool:
        """Both players known and it hasn't been played."""
        return (self.player_a is not None and self.player_b is not None
                and self.winner_id is None)

    @property
    def players(self) -> List[int]:
        return [p for p in (self.player_a, self.player_b) if p is not None]

    def loser(self) -> Optional[int]:
        if self.winner_id is None:
            return None
        others = [p for p in self.players if p != self.winner_id]
        return others[0] if others else None


@dataclass
class Season:
    id: int
    name: str
    starts_on: str
    ends_on: Optional[str]
    is_current: bool
    created_at: str


@dataclass
class Match:
    id: int
    played_on: str
    season_id: int
    division: str
    player_a: int
    player_a2: Optional[int]
    player_b: int
    player_b2: Optional[int]
    winner_side: str
    score: str
    games_a: int
    games_b: int
    retired: bool
    walkover: bool
    status: str
    submitted_by: Optional[int]
    confirmed_by: Optional[int]
    submitted_at: str
    confirmed_at: Optional[str]
    note: str

    # -- lineup ---------------------------------------------------------
    @property
    def side_a(self) -> List[int]:
        return [self.player_a] + ([self.player_a2] if self.player_a2 else [])

    @property
    def side_b(self) -> List[int]:
        return [self.player_b] + ([self.player_b2] if self.player_b2 else [])

    @property
    def is_doubles(self) -> bool:
        return self.player_a2 is not None

    @property
    def players(self) -> List[int]:
        return self.side_a + self.side_b

    @property
    def winners(self) -> List[int]:
        return self.side_a if self.winner_side == SIDE_A else self.side_b

    @property
    def losers(self) -> List[int]:
        return self.side_b if self.winner_side == SIDE_A else self.side_a

    # -- convenience for singles, where there is exactly one of each ----
    @property
    def winner_id(self) -> int:
        return self.winners[0]

    @property
    def loser_id(self) -> int:
        return self.losers[0]

    # -- per-player views -----------------------------------------------
    def side_of(self, player_id: int) -> Optional[str]:
        if player_id in self.side_a:
            return SIDE_A
        if player_id in self.side_b:
            return SIDE_B
        return None

    def won(self, player_id: int) -> bool:
        return self.side_of(player_id) == self.winner_side

    def partner_of(self, player_id: int) -> Optional[int]:
        side = self.side_a if player_id in self.side_a else self.side_b
        others = [p for p in side if p != player_id]
        return others[0] if others else None

    def opponents_of(self, player_id: int) -> List[int]:
        return self.side_b if player_id in self.side_a else self.side_a

    def games_for(self, player_id: int) -> tuple:
        """(games won, games lost) from this player's point of view."""
        if self.side_of(player_id) == SIDE_A:
            return self.games_a, self.games_b
        return self.games_b, self.games_a


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or DATA_DIR, exist_ok=True)
        # An in-memory database only exists for as long as a connection to it
        # does, so it has to reuse one handle. The web server is threaded, so
        # that handle must allow cross-thread use and be serialised by a lock.
        # File-backed databases just open a connection per operation and let
        # SQLite do its own locking.
        self._lock = threading.RLock()
        self._shared = (
            sqlite3.connect(path, check_same_thread=False)
            if path == ":memory:" else None
        )
        self.migrated_from = self.init_schema()
        # Bumped on every write so cached rating computations know to refresh.
        self.version = 0

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._shared is not None:
            with self._lock:
                self._shared.row_factory = sqlite3.Row
                yield self._shared
                self._shared.commit()
            return
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> int:
        with self.connect() as conn:
            return migrate(conn)

    def close(self) -> None:
        """Only meaningful for in-memory databases, which hold one handle
        open for their whole life; file-backed ones connect per operation."""
        if self._shared is not None:
            self._shared.close()
            self._shared = None

    def __del__(self):
        try:
            self.close()
        except Exception:       # noqa: BLE001 -- interpreter shutdown
            pass

    def _touch(self) -> None:
        self.version += 1

    # ---------------------------------------------------------------- seasons
    def seasons(self) -> List[Season]:
        """Every season in the order they ran.

        Ordered by id, not by starts_on: seasons are always created one after
        another, whereas the first season's nominal start date is just the day
        the database was created and can easily sit after matches that were
        backdated into it. Getting this order wrong silently inverts season
        carry-over, so it keys off the one thing that is always true.
        """
        with self.connect() as conn:
            return [_to_season(r) for r in
                    conn.execute("SELECT * FROM seasons ORDER BY id")]

    def current_season(self) -> Season:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM seasons WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM seasons ORDER BY id DESC LIMIT 1").fetchone()
        return _to_season(row)

    def get_season(self, season_id: int) -> Optional[Season]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM seasons WHERE id = ?", (season_id,)).fetchone()
        return _to_season(row) if row else None

    def start_season(self, name: str, starts_on: Optional[str] = None) -> Season:
        """Open a new season and close the current one.

        Nothing is deleted: past seasons stay readable, and their final ratings
        seed the new one (see engine.py).
        """
        name = name.strip()
        if not name:
            raise ValueError("A season needs a name.")
        starts_on = (starts_on or date.today().isoformat()).strip()
        _validate_date(starts_on)
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                "UPDATE seasons SET is_current = 0, ends_on = COALESCE(ends_on, ?)"
                " WHERE is_current = 1",
                (starts_on,),
            )
            cur = conn.execute(
                "INSERT INTO seasons (name, starts_on, ends_on, is_current, created_at)"
                " VALUES (?, ?, NULL, 1, ?)",
                (name, starts_on, now),
            )
            season_id = cur.lastrowid
        self._touch()
        return self.get_season(season_id)          # type: ignore[return-value]

    def set_season_start(self, season_id: int, starts_on: str) -> None:
        """Correct a season's start date (the first one defaults to today)."""
        _validate_date(starts_on)
        with self.connect() as conn:
            conn.execute("UPDATE seasons SET starts_on = ? WHERE id = ?",
                         (starts_on, season_id))
        self._touch()

    def rename_season(self, season_id: int, name: str) -> None:
        if not name.strip():
            raise ValueError("A season needs a name.")
        with self.connect() as conn:
            conn.execute("UPDATE seasons SET name = ? WHERE id = ?",
                         (name.strip(), season_id))
        self._touch()

    # ---------------------------------------------------------------- players
    def add_player(
        self, name: str, email: str = "", pin: str = "",
        category: str = UNSPECIFIED,
    ) -> Player:
        """Add a player. With no `pin`, they choose their own on first sign-in.

        That default matters: a randomly generated 4-digit PIN is something
        nobody remembers for a credential they use twice a month, and it puts
        the admin in the business of distributing secrets. Letting people pick
        their own removes both problems.
        """
        name = name.strip()
        if not name:
            raise ValueError("A player needs a name.")
        if self.find_player_by_name(name):
            raise ValueError(f"{name} is already on the ladder.")
        pin = pin.strip()
        pin_hash, salt = hash_pin(pin) if pin else ("", "")
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO players (name, email, pin_hash, pin_salt, category,"
                " active, joined_on) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (name, email.strip(), pin_hash, salt, category,
                 date.today().isoformat()),
            )
            player_id = cur.lastrowid
        self._touch()
        return self.get_player(player_id)          # type: ignore[return-value]

    def has_pin(self, player_id: int) -> bool:
        """Whether this player has claimed their account yet."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT pin_hash FROM players WHERE id = ?", (player_id,)
            ).fetchone()
        return bool(row and row["pin_hash"])

    def get_player(self, player_id: int) -> Optional[Player]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
        return _to_player(row) if row else None

    def find_player_by_name(self, name: str) -> Optional[Player]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM players WHERE name = ? COLLATE NOCASE", (name.strip(),)
            ).fetchone()
        return _to_player(row) if row else None

    def list_players(self, *, active_only: bool = False,
                     category: Optional[str] = None) -> List[Player]:
        sql = "SELECT * FROM players"
        clauses, params = [], []
        if active_only:
            clauses.append("active = 1")
        if category:
            clauses.append("category = ?")
            params.append(category)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name COLLATE NOCASE"
        with self.connect() as conn:
            return [_to_player(r) for r in conn.execute(sql, params)]

    def set_player_active(self, player_id: int, active: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE players SET active = ? WHERE id = ?", (1 if active else 0, player_id)
            )
        self._touch()

    def set_player_category(self, player_id: int, category: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE players SET category = ? WHERE id = ?",
                         (category, player_id))
        self._touch()

    def rename_player(self, player_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("A player needs a name.")
        existing = self.find_player_by_name(name)
        if existing and existing.id != player_id:
            raise ValueError(f"{name} is already on the ladder.")
        with self.connect() as conn:
            conn.execute("UPDATE players SET name = ? WHERE id = ?", (name, player_id))
        self._touch()

    def set_email(self, player_id: int, email: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE players SET email = ? WHERE id = ?",
                         (email.strip(), player_id))
        self._touch()

    def set_notification(self, player_id: int, kind: str, on: bool) -> None:
        if kind not in ("confirm", "result", "weekly", "season", "request"):
            raise ValueError(f"Unknown notification type {kind!r}.")
        with self.connect() as conn:
            conn.execute(
                f"UPDATE players SET notify_{kind} = ? WHERE id = ?",
                (1 if on else 0, player_id),
            )
        self._touch()

    def set_pin(self, player_id: int, pin: str) -> None:
        pin = pin.strip()
        if not (pin.isdigit() and len(pin) == 4):
            raise ValueError("A PIN must be exactly 4 digits.")
        pin_hash, salt = hash_pin(pin)
        with self.connect() as conn:
            conn.execute("UPDATE players SET pin_hash = ?, pin_salt = ? WHERE id = ?",
                         (pin_hash, salt, player_id))
        self._touch()

    def clear_pin(self, player_id: int) -> None:
        """Forget a player's PIN so they choose a new one next time they sign in.

        This is the whole 'I forgot my PIN' path: the admin never learns or
        hands over a secret, and the player picks something memorable again.
        """
        with self.connect() as conn:
            conn.execute(
                "UPDATE players SET pin_hash = '', pin_salt = '' WHERE id = ?",
                (player_id,),
            )
        self._touch()

    def check_pin(self, player_id: int, pin: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT pin_hash, pin_salt FROM players WHERE id = ?", (player_id,)
            ).fetchone()
        if not row:
            return False
        return verify_pin((pin or "").strip(), row["pin_hash"], row["pin_salt"])

    # ---------------------------------------------------------------- matches
    def add_match(
        self,
        *,
        played_on: str,
        season_id: int,
        division: str,
        side_a: Sequence[int],
        side_b: Sequence[int],
        winner_side: str,
        score: str,
        games_a: int = 0,
        games_b: int = 0,
        retired: bool = False,
        walkover: bool = False,
        status: str = PENDING,
        submitted_by: Optional[int] = None,
        note: str = "",
    ) -> int:
        if winner_side not in (SIDE_A, SIDE_B):
            raise ValueError("winner_side must be 'a' or 'b'.")
        if len(side_a) != len(side_b) or not 1 <= len(side_a) <= 2:
            raise ValueError("Both sides must have the same one or two players.")
        everyone = list(side_a) + list(side_b)
        if len(set(everyone)) != len(everyone):
            raise ValueError("The same player appears twice in this match.")
        _validate_date(played_on)

        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO matches (played_on, season_id, division, player_a,"
                " player_a2, player_b, player_b2, winner_side, score, games_a,"
                " games_b, retired, walkover, status, submitted_by, confirmed_by,"
                " submitted_at, confirmed_at, note)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    played_on, season_id, division,
                    side_a[0], side_a[1] if len(side_a) > 1 else None,
                    side_b[0], side_b[1] if len(side_b) > 1 else None,
                    winner_side, score, games_a, games_b,
                    int(retired), int(walkover), status, submitted_by,
                    submitted_by if status == CONFIRMED else None,
                    now, now if status == CONFIRMED else None, note,
                ),
            )
            match_id = cur.lastrowid
        self._touch()
        return match_id

    def get_match(self, match_id: int) -> Optional[Match]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        return _to_match(row) if row else None

    def list_matches(
        self,
        *,
        status: Optional[str] = None,
        player_id: Optional[int] = None,
        division: Optional[str] = None,
        season_id: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> List[Match]:
        sql = "SELECT * FROM matches"
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if player_id is not None:
            clauses.append("(player_a = ? OR player_a2 = ? OR player_b = ? OR player_b2 = ?)")
            params.extend([player_id] * 4)
        if division:
            clauses.append("division = ?")
            params.append(division)
        if season_id is not None:
            clauses.append("season_id = ?")
            params.append(season_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY played_on DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            return [_to_match(r) for r in conn.execute(sql, params)]

    def confirmed_matches_chronological(
        self, season_id: Optional[int] = None
    ) -> List[Match]:
        """Every counting result, oldest first -- the input to the replay."""
        sql = "SELECT * FROM matches WHERE status = ?"
        params: list = [CONFIRMED]
        if season_id is not None:
            sql += " AND season_id = ?"
            params.append(season_id)
        sql += " ORDER BY played_on ASC, id ASC"
        with self.connect() as conn:
            return [_to_match(r) for r in conn.execute(sql, params)]

    def set_match_status(
        self, match_id: int, status: str, *, actor_id: Optional[int] = None
    ) -> None:
        if status not in (PENDING, CONFIRMED, REJECTED):
            raise ValueError(f"Unknown status {status!r}.")
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                "UPDATE matches SET status = ?, confirmed_by = ?, confirmed_at = ?"
                " WHERE id = ?",
                (status, actor_id, now if status == CONFIRMED else None, match_id),
            )
        self._touch()

    def set_match_division(self, match_id: int, division: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE matches SET division = ? WHERE id = ?",
                         (division, match_id))
        self._touch()

    def delete_match(self, match_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))
        self._touch()

    def last_meeting(self, player_a: int, player_b: int) -> Optional[Match]:
        """Most recent confirmed match featuring both players, for the cooldown."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM matches WHERE status = ?"
                " ORDER BY played_on DESC, id DESC", (CONFIRMED,)
            )
            for row in rows:
                match = _to_match(row)
                if player_a in match.players and player_b in match.players:
                    return match
        return None

    # ----------------------------------------------------------- availability
    def get_availability(self, player_id: int) -> Availability:
        """A player's weekly pattern plus any dated exceptions."""
        weekly: Dict[int, List[tuple]] = {}
        blocked: Dict[str, List[tuple]] = {}
        extra: Dict[str, List[tuple]] = {}
        with self.connect() as conn:
            for row in conn.execute(
                "SELECT weekday, start_min, end_min FROM availability"
                " WHERE player_id = ? ORDER BY weekday, start_min", (player_id,)
            ):
                weekly.setdefault(row["weekday"], []).append(
                    (row["start_min"], row["end_min"]))
            for row in conn.execute(
                "SELECT on_date, start_min, end_min, available"
                " FROM availability_exception WHERE player_id = ?"
                " ORDER BY on_date, start_min", (player_id,)
            ):
                target = extra if row["available"] else blocked
                target.setdefault(row["on_date"], []).append(
                    (row["start_min"], row["end_min"]))
        return Availability(weekly=weekly, blocked=blocked, extra=extra)

    def set_weekly_availability(
        self, player_id: int, weekly: Dict[int, List[tuple]]
    ) -> None:
        """Replace the whole weekly pattern in one go, as the grid posts it."""
        with self.connect() as conn:
            conn.execute("DELETE FROM availability WHERE player_id = ?", (player_id,))
            conn.executemany(
                "INSERT INTO availability (player_id, weekday, start_min, end_min)"
                " VALUES (?,?,?,?)",
                [(player_id, day, start, end)
                 for day, intervals in weekly.items()
                 for start, end in intervals],
            )
        self._touch()

    def add_exception(self, player_id: int, on_date: str, start: int, end: int,
                      available: bool = False) -> None:
        _validate_date(on_date)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO availability_exception (player_id, on_date,"
                " start_min, end_min, available) VALUES (?,?,?,?,?)",
                (player_id, on_date, start, end, int(available)),
            )
        self._touch()

    def clear_exceptions(self, player_id: int, on_date: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM availability_exception WHERE player_id = ? AND on_date = ?",
                (player_id, on_date),
            )
        self._touch()

    def prune_exceptions(self, before: str) -> None:
        """Drop exceptions for dates that have passed -- they can't matter again."""
        with self.connect() as conn:
            conn.execute("DELETE FROM availability_exception WHERE on_date < ?",
                         (before,))

    def players_with_availability(self) -> List[int]:
        with self.connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT player_id FROM availability")]

    # --------------------------------------------------------- match requests
    def add_match_request(
        self, *, division: str, from_player: int, to_player: int,
        starts_at: str, minutes: int, match_format: str, message: str = "",
        tournament_match_id: Optional[int] = None,
    ) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO match_requests (division, from_player, to_player,"
                " starts_at, minutes, match_format, status, message,"
                " tournament_match_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (division, from_player, to_player, starts_at, minutes,
                 match_format, REQUEST_PENDING, message.strip(),
                 tournament_match_id, now),
            )
            request_id = cur.lastrowid
        self._touch()
        return request_id

    def get_match_request(self, request_id: int) -> Optional["MatchRequest"]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM match_requests WHERE id = ?",
                               (request_id,)).fetchone()
        return _to_request(row) if row else None

    def list_match_requests(
        self, *, player_id: Optional[int] = None, status: Optional[str] = None,
        upcoming_only: bool = False, limit: Optional[int] = None,
    ) -> List["MatchRequest"]:
        sql = "SELECT * FROM match_requests"
        clauses, params = [], []
        if player_id is not None:
            clauses.append("(from_player = ? OR to_player = ?)")
            params.extend([player_id, player_id])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if upcoming_only:
            clauses.append("starts_at >= ?")
            params.append(datetime.now().isoformat(timespec="seconds"))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY starts_at ASC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self.connect() as conn:
            return [_to_request(r) for r in conn.execute(sql, params)]

    def set_request_status(self, request_id: int, status: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                "UPDATE match_requests SET status = ?, responded_at = ? WHERE id = ?",
                (status, now, request_id),
            )
        self._touch()

    # ------------------------------------------------------------ tournaments
    def create_tournament(
        self, *, name: str, division: str, season_id: int, style: str,
        seeding: str, match_format: str,
    ) -> int:
        if not name.strip():
            raise ValueError("A tournament needs a name.")
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO tournaments (name, division, season_id, style,"
                " seeding, match_format, status, created_at)"
                " VALUES (?,?,?,?,?,?,'setup',?)",
                (name.strip(), division, season_id, style, seeding,
                 match_format, now),
            )
            tournament_id = cur.lastrowid
        self._touch()
        return tournament_id

    def get_tournament(self, tournament_id: int) -> Optional["Tournament"]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tournaments WHERE id = ?",
                               (tournament_id,)).fetchone()
        return _to_tournament(row) if row else None

    def list_tournaments(self, season_id: Optional[int] = None) -> List["Tournament"]:
        sql = "SELECT * FROM tournaments"
        params: list = []
        if season_id is not None:
            sql += " WHERE season_id = ?"
            params.append(season_id)
        sql += " ORDER BY id DESC"
        with self.connect() as conn:
            return [_to_tournament(r) for r in conn.execute(sql, params)]

    def set_tournament_status(self, tournament_id: int, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE tournaments SET status = ? WHERE id = ?",
                         (status, tournament_id))
        self._touch()

    def set_entries(self, tournament_id: int, player_ids: Sequence[int]) -> None:
        """Replace the field, storing the seed order given."""
        with self.connect() as conn:
            conn.execute("DELETE FROM tournament_entries WHERE tournament_id = ?",
                         (tournament_id,))
            conn.executemany(
                "INSERT INTO tournament_entries (tournament_id, player_id, seed)"
                " VALUES (?,?,?)",
                [(tournament_id, pid, seed)
                 for seed, pid in enumerate(player_ids, start=1)],
            )
        self._touch()

    def entries(self, tournament_id: int) -> List[Tuple[int, int]]:
        """[(player_id, seed)] in seed order."""
        with self.connect() as conn:
            return [(r["player_id"], r["seed"]) for r in conn.execute(
                "SELECT player_id, seed FROM tournament_entries"
                " WHERE tournament_id = ? ORDER BY seed", (tournament_id,))]

    def set_rounds(self, tournament_id: int,
                   rounds: Sequence[Tuple[int, str, Optional[str]]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM tournament_rounds WHERE tournament_id = ?",
                         (tournament_id,))
            conn.executemany(
                "INSERT INTO tournament_rounds (tournament_id, round_no, name,"
                " deadline) VALUES (?,?,?,?)",
                [(tournament_id, no, name, deadline)
                 for no, name, deadline in rounds],
            )
        self._touch()

    def rounds(self, tournament_id: int) -> List["TournamentRound"]:
        with self.connect() as conn:
            return [
                TournamentRound(r["round_no"], r["name"], r["deadline"])
                for r in conn.execute(
                    "SELECT * FROM tournament_rounds WHERE tournament_id = ?"
                    " ORDER BY round_no", (tournament_id,))
            ]

    def set_round_deadline(self, tournament_id: int, round_no: int,
                           deadline: Optional[str]) -> None:
        if deadline:
            _validate_date(deadline)
        with self.connect() as conn:
            conn.execute(
                "UPDATE tournament_rounds SET deadline = ? WHERE tournament_id = ?"
                " AND round_no = ?", (deadline, tournament_id, round_no))
        self._touch()

    def replace_tournament_matches(
        self, tournament_id: int,
        pairings: Sequence[Tuple[int, int, Optional[int], Optional[int], str]],
    ) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM tournament_matches WHERE tournament_id = ?",
                         (tournament_id,))
            conn.executemany(
                "INSERT INTO tournament_matches (tournament_id, round_no, slot,"
                " player_a, player_b, status) VALUES (?,?,?,?,?,?)",
                [(tournament_id, rnd, slot, a, b, status)
                 for rnd, slot, a, b, status in pairings],
            )
        self._touch()

    def tournament_matches(self, tournament_id: int,
                           round_no: Optional[int] = None) -> List["TournamentMatch"]:
        sql = "SELECT * FROM tournament_matches WHERE tournament_id = ?"
        params: list = [tournament_id]
        if round_no is not None:
            sql += " AND round_no = ?"
            params.append(round_no)
        sql += " ORDER BY round_no, slot"
        with self.connect() as conn:
            return [_to_tmatch(r) for r in conn.execute(sql, params)]

    def get_tournament_match(self, tmatch_id: int) -> Optional["TournamentMatch"]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tournament_matches WHERE id = ?",
                               (tmatch_id,)).fetchone()
        return _to_tmatch(row) if row else None

    def update_tournament_match(
        self, tmatch_id: int, *, player_a: Optional[int] = None,
        player_b: Optional[int] = None, winner_id: Optional[int] = None,
        match_id: Optional[int] = None, status: Optional[str] = None,
    ) -> None:
        sets, params = [], []
        for column, value in (("player_a", player_a), ("player_b", player_b),
                              ("winner_id", winner_id), ("match_id", match_id),
                              ("status", status)):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        if not sets:
            return
        params.append(tmatch_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE tournament_matches SET {', '.join(sets)}"
                         " WHERE id = ?", params)
        self._touch()

    def tournament_match_for_match(self, match_id: int) -> Optional["TournamentMatch"]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tournament_matches WHERE match_id = ?",
                (match_id,)).fetchone()
        return _to_tmatch(row) if row else None

    # ------------------------------------------------------- destructive ops
    def delete_player_completely(self, player_id: int) -> dict:
        """Erase a player and everything that referenced them.

        Unlike deactivating, this cannot be undone and it removes matches that
        other players also appeared in -- their opponents' records change too.
        It exists for clearing out test data; for someone who has graduated,
        deactivate instead so the history survives.
        """
        removed = {"matches": 0, "requests": 0, "tournament_entries": 0}
        with self.connect() as conn:
            removed["matches"] = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE player_a = ? OR player_a2 = ?"
                " OR player_b = ? OR player_b2 = ?",
                (player_id,) * 4).fetchone()[0]
            removed["requests"] = conn.execute(
                "SELECT COUNT(*) FROM match_requests WHERE from_player = ?"
                " OR to_player = ?", (player_id, player_id)).fetchone()[0]
            removed["tournament_entries"] = conn.execute(
                "SELECT COUNT(*) FROM tournament_entries WHERE player_id = ?",
                (player_id,)).fetchone()[0]

            conn.execute("DELETE FROM matches WHERE player_a = ? OR player_a2 = ?"
                         " OR player_b = ? OR player_b2 = ?", (player_id,) * 4)
            conn.execute("DELETE FROM match_requests WHERE from_player = ?"
                         " OR to_player = ?", (player_id, player_id))
            conn.execute("DELETE FROM tournament_entries WHERE player_id = ?",
                         (player_id,))
            conn.execute("DELETE FROM tournament_matches WHERE player_a = ?"
                         " OR player_b = ?", (player_id, player_id))
            conn.execute("DELETE FROM availability WHERE player_id = ?", (player_id,))
            conn.execute("DELETE FROM availability_exception WHERE player_id = ?",
                         (player_id,))
            conn.execute("DELETE FROM sessions WHERE player_id = ?", (player_id,))
            conn.execute("UPDATE matches SET submitted_by = NULL WHERE submitted_by = ?",
                         (player_id,))
            conn.execute("UPDATE matches SET confirmed_by = NULL WHERE confirmed_by = ?",
                         (player_id,))
            conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
        self._touch()
        return removed

    def delete_season_completely(self, season_id: int) -> dict:
        """Erase a season with its matches and tournaments."""
        removed = {"matches": 0, "tournaments": 0}
        with self.connect() as conn:
            removed["matches"] = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE season_id = ?",
                (season_id,)).fetchone()[0]
            tournament_ids = [r[0] for r in conn.execute(
                "SELECT id FROM tournaments WHERE season_id = ?", (season_id,))]
            removed["tournaments"] = len(tournament_ids)

            for tournament_id in tournament_ids:
                conn.execute("DELETE FROM tournament_matches WHERE tournament_id = ?",
                             (tournament_id,))
                conn.execute("DELETE FROM tournament_entries WHERE tournament_id = ?",
                             (tournament_id,))
                conn.execute("DELETE FROM tournament_rounds WHERE tournament_id = ?",
                             (tournament_id,))
            conn.execute("DELETE FROM tournaments WHERE season_id = ?", (season_id,))
            conn.execute("DELETE FROM matches WHERE season_id = ?", (season_id,))
            conn.execute("DELETE FROM seasons WHERE id = ?", (season_id,))

            # Never leave the club without a current season to record into.
            still_current = conn.execute(
                "SELECT COUNT(*) FROM seasons WHERE is_current = 1").fetchone()[0]
            if not still_current:
                latest = conn.execute(
                    "SELECT id FROM seasons ORDER BY id DESC LIMIT 1").fetchone()
                if latest:
                    conn.execute("UPDATE seasons SET is_current = 1 WHERE id = ?",
                                 (latest[0],))
        self._touch()
        return removed

    def delete_tournament(self, tournament_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM tournament_matches WHERE tournament_id = ?",
                         (tournament_id,))
            conn.execute("DELETE FROM tournament_entries WHERE tournament_id = ?",
                         (tournament_id,))
            conn.execute("DELETE FROM tournament_rounds WHERE tournament_id = ?",
                         (tournament_id,))
            conn.execute("DELETE FROM tournaments WHERE id = ?", (tournament_id,))
        self._touch()

    def season_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM seasons").fetchone()[0]

    # --------------------------------------------------------------- sessions
    def save_session(self, token: str, player_id: Optional[int],
                     is_admin: bool, csrf: str) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token, player_id, is_admin, csrf, created_at,"
                " last_seen) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(token) DO UPDATE SET player_id=excluded.player_id,"
                " is_admin=excluded.is_admin, last_seen=excluded.last_seen",
                (token, player_id, int(is_admin), csrf, now, now),
            )

    def load_session(self, token: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
        if not row:
            return None
        return {
            "token": row["token"], "player_id": row["player_id"],
            "is_admin": bool(row["is_admin"]), "csrf": row["csrf"],
        }

    def delete_session(self, token: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def purge_sessions(self, older_than_days: int = 60) -> None:
        cutoff = (datetime.now().timestamp() - older_than_days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE last_seen < ?", (cutoff_iso,))


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a date. Use YYYY-MM-DD.") from None


def _to_player(row: sqlite3.Row) -> Player:
    return Player(
        id=row["id"], name=row["name"], email=row["email"],
        category=row["category"], active=bool(row["active"]),
        joined_on=row["joined_on"], pin_set=bool(row["pin_hash"]),
        notify_confirm=bool(row["notify_confirm"]),
        notify_result=bool(row["notify_result"]),
        notify_weekly=bool(row["notify_weekly"]),
        notify_season=bool(row["notify_season"]),
        notify_request=bool(row["notify_request"]),
    )


def _to_request(row: sqlite3.Row) -> MatchRequest:
    return MatchRequest(
        id=row["id"], division=row["division"], from_player=row["from_player"],
        to_player=row["to_player"], starts_at=row["starts_at"],
        minutes=row["minutes"], match_format=row["match_format"],
        status=row["status"], message=row["message"],
        tournament_match_id=row["tournament_match_id"],
        created_at=row["created_at"], responded_at=row["responded_at"],
    )


def _to_tournament(row: sqlite3.Row) -> Tournament:
    return Tournament(
        id=row["id"], name=row["name"], division=row["division"],
        season_id=row["season_id"], style=row["style"], seeding=row["seeding"],
        match_format=row["match_format"], status=row["status"],
        created_at=row["created_at"],
    )


def _to_tmatch(row: sqlite3.Row) -> TournamentMatch:
    return TournamentMatch(
        id=row["id"], tournament_id=row["tournament_id"],
        round_no=row["round_no"], slot=row["slot"], player_a=row["player_a"],
        player_b=row["player_b"], winner_id=row["winner_id"],
        match_id=row["match_id"], status=row["status"],
    )


def _to_season(row: sqlite3.Row) -> Season:
    return Season(
        id=row["id"], name=row["name"], starts_on=row["starts_on"],
        ends_on=row["ends_on"], is_current=bool(row["is_current"]),
        created_at=row["created_at"],
    )


def _to_match(row: sqlite3.Row) -> Match:
    return Match(
        id=row["id"], played_on=row["played_on"], season_id=row["season_id"],
        division=row["division"], player_a=row["player_a"],
        player_a2=row["player_a2"], player_b=row["player_b"],
        player_b2=row["player_b2"], winner_side=row["winner_side"],
        score=row["score"], games_a=row["games_a"], games_b=row["games_b"],
        retired=bool(row["retired"]), walkover=bool(row["walkover"]),
        status=row["status"], submitted_by=row["submitted_by"],
        confirmed_by=row["confirmed_by"], submitted_at=row["submitted_at"],
        confirmed_at=row["confirmed_at"], note=row["note"],
    )
