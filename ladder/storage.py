"""SQLite persistence.

Only facts are stored: players, seasons, and the match results people submit.
Ratings are never stored -- they are recomputed from the match history on
demand (see engine.py). That is a deliberate choice: if a result is corrected
or deleted six weeks later, every rating downstream of it is right again
immediately, with no drift and no migration.
"""

from __future__ import annotations

import os
import random
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterator, List, Optional, Sequence

from .config import DATA_DIR, DB_PATH
from .divisions import UNSPECIFIED
from .migrations import hash_pin, migrate, verify_pin

PENDING = "pending"
CONFIRMED = "confirmed"
REJECTED = "rejected"

SIDE_A = "a"
SIDE_B = "b"


@dataclass
class Player:
    id: int
    name: str
    email: str
    category: str
    active: bool
    joined_on: str
    notify_confirm: bool = True
    notify_result: bool = True
    notify_weekly: bool = False
    notify_season: bool = True

    def wants(self, kind: str) -> bool:
        """Whether this player opted in to a notification type."""
        return bool(getattr(self, f"notify_{kind}", False)) and bool(self.email)


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
        name = name.strip()
        if not name:
            raise ValueError("A player needs a name.")
        if self.find_player_by_name(name):
            raise ValueError(f"{name} is already on the ladder.")
        pin = pin.strip() or f"{random.randint(0, 9999):04d}"
        pin_hash, salt = hash_pin(pin)
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO players (name, email, pin_hash, pin_salt, category,"
                " active, joined_on) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (name, email.strip(), pin_hash, salt, category,
                 date.today().isoformat()),
            )
            player_id = cur.lastrowid
        self._touch()
        player = self.get_player(player_id)
        # The caller needs the plain PIN once, to hand to the player; it is not
        # recoverable afterwards.
        player.generated_pin = pin                 # type: ignore[attr-defined]
        return player                              # type: ignore[return-value]

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
        if kind not in ("confirm", "result", "weekly", "season"):
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

    def reset_pin(self, player_id: int) -> str:
        """Generate a new PIN and return it once, in plain text."""
        pin = f"{random.randint(0, 9999):04d}"
        self.set_pin(player_id, pin)
        return pin

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
        joined_on=row["joined_on"],
        notify_confirm=bool(row["notify_confirm"]),
        notify_result=bool(row["notify_result"]),
        notify_weekly=bool(row["notify_weekly"]),
        notify_season=bool(row["notify_season"]),
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
