"""Schema migrations.

The ladder's whole value is its match history, so upgrades must never require
starting over. Each migration moves the database forward one version, tracked in
SQLite's own `PRAGMA user_version`.

Migrations rebuild tables by copy-and-rename rather than `ALTER TABLE ... DROP
COLUMN`, because that pattern works on every SQLite version rather than only
3.35+, and it keeps the resulting schema identical to a freshly created one.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import date
from typing import Callable, List

SCHEMA_VERSION = 3

# Divisions can't be inferred for matches recorded before divisions existed --
# every one of them was singles, but nothing recorded which category. They go
# here and the admin reassigns them; nothing is lost either way.
LEGACY_DIVISION = "mens_singles"

PBKDF2_ROUNDS = 200_000


def hash_pin(pin: str, salt: str | None = None) -> tuple[str, str]:
    """Hash a PIN for storage. Returns (hash_hex, salt_hex).

    PINs only gate "did the right person confirm this result", but the app is
    meant to be hosted publicly, and a plain-text credential column is exactly
    the thing that turns a small breach into a large one.
    """
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS
    )
    return digest.hex(), salt


def verify_pin(pin: str, pin_hash: str, salt: str) -> bool:
    if not pin_hash or not salt:
        return False
    candidate, _ = hash_pin(pin, salt)
    return secrets.compare_digest(candidate, pin_hash)


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS seasons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    starts_on   TEXT    NOT NULL,
    ends_on     TEXT,
    is_current  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    email           TEXT    NOT NULL DEFAULT '',
    pin_hash        TEXT    NOT NULL DEFAULT '',
    pin_salt        TEXT    NOT NULL DEFAULT '',
    category        TEXT    NOT NULL DEFAULT 'unspecified',
    active          INTEGER NOT NULL DEFAULT 1,
    joined_on       TEXT    NOT NULL,
    notify_confirm  INTEGER NOT NULL DEFAULT 1,
    notify_result   INTEGER NOT NULL DEFAULT 1,
    notify_weekly   INTEGER NOT NULL DEFAULT 0,
    notify_season   INTEGER NOT NULL DEFAULT 1,
    notify_request  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    played_on     TEXT    NOT NULL,
    season_id     INTEGER NOT NULL REFERENCES seasons(id),
    division      TEXT    NOT NULL,
    player_a      INTEGER NOT NULL REFERENCES players(id),
    player_a2     INTEGER REFERENCES players(id),
    player_b      INTEGER NOT NULL REFERENCES players(id),
    player_b2     INTEGER REFERENCES players(id),
    winner_side   TEXT    NOT NULL,
    score         TEXT    NOT NULL,
    games_a       INTEGER NOT NULL DEFAULT 0,
    games_b       INTEGER NOT NULL DEFAULT 0,
    retired       INTEGER NOT NULL DEFAULT 0,
    walkover      INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'pending',
    submitted_by  INTEGER REFERENCES players(id),
    confirmed_by  INTEGER REFERENCES players(id),
    submitted_at  TEXT    NOT NULL,
    confirmed_at  TEXT,
    note          TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT    PRIMARY KEY,
    player_id   INTEGER REFERENCES players(id),
    is_admin    INTEGER NOT NULL DEFAULT 0,
    csrf        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matches_date    ON matches(played_on);
CREATE INDEX IF NOT EXISTS idx_matches_status  ON matches(status);
CREATE INDEX IF NOT EXISTS idx_matches_season  ON matches(season_id);
CREATE INDEX IF NOT EXISTS idx_matches_div     ON matches(division);
CREATE INDEX IF NOT EXISTS idx_matches_a       ON matches(player_a);
CREATE INDEX IF NOT EXISTS idx_matches_b       ON matches(player_b);
CREATE INDEX IF NOT EXISTS idx_matches_a2      ON matches(player_a2);
CREATE INDEX IF NOT EXISTS idx_matches_b2      ON matches(player_b2);
"""


SCHEMA_V2 = """
-- When people are normally free, as a weekly pattern (Mon = 0).
CREATE TABLE IF NOT EXISTS availability (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   INTEGER NOT NULL REFERENCES players(id),
    weekday     INTEGER NOT NULL,
    start_min   INTEGER NOT NULL,
    end_min     INTEGER NOT NULL
);

-- One-off differences from that pattern on a specific date.
-- available = 0 blocks time out, 1 adds time that isn't in the usual week.
CREATE TABLE IF NOT EXISTS availability_exception (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   INTEGER NOT NULL REFERENCES players(id),
    on_date     TEXT    NOT NULL,
    start_min   INTEGER NOT NULL,
    end_min     INTEGER NOT NULL,
    available   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS match_requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    division            TEXT    NOT NULL,
    from_player         INTEGER NOT NULL REFERENCES players(id),
    to_player           INTEGER NOT NULL REFERENCES players(id),
    starts_at           TEXT    NOT NULL,
    minutes             INTEGER NOT NULL,
    match_format        TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending',
    message             TEXT    NOT NULL DEFAULT '',
    tournament_match_id INTEGER REFERENCES tournament_matches(id),
    created_at          TEXT    NOT NULL,
    responded_at        TEXT
);

CREATE TABLE IF NOT EXISTS tournaments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    division      TEXT    NOT NULL,
    season_id     INTEGER NOT NULL REFERENCES seasons(id),
    style         TEXT    NOT NULL,
    seeding       TEXT    NOT NULL,
    match_format  TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'setup',
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tournament_entries (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
    player_id     INTEGER NOT NULL REFERENCES players(id),
    seed          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tournament_id, player_id)
);

CREATE TABLE IF NOT EXISTS tournament_rounds (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
    round_no      INTEGER NOT NULL,
    name          TEXT    NOT NULL DEFAULT '',
    deadline      TEXT,
    PRIMARY KEY (tournament_id, round_no)
);

CREATE TABLE IF NOT EXISTS tournament_matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
    round_no      INTEGER NOT NULL,
    slot          INTEGER NOT NULL,
    player_a      INTEGER REFERENCES players(id),
    player_b      INTEGER REFERENCES players(id),
    winner_id     INTEGER REFERENCES players(id),
    match_id      INTEGER REFERENCES matches(id),
    status        TEXT    NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_avail_player   ON availability(player_id);
CREATE INDEX IF NOT EXISTS idx_exc_player     ON availability_exception(player_id, on_date);
CREATE INDEX IF NOT EXISTS idx_req_to         ON match_requests(to_player, status);
CREATE INDEX IF NOT EXISTS idx_req_from       ON match_requests(from_player, status);
CREATE INDEX IF NOT EXISTS idx_tmatch_tourney ON tournament_matches(tournament_id, round_no);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add availability, match requests and tournaments.

    Purely additive -- no existing table changes shape, so there is nothing to
    copy and nothing that can go wrong with existing results.
    """
    conn.executescript(SCHEMA_V2)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _ensure_season(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM seasons ORDER BY id LIMIT 1").fetchone()
    if row:
        return row[0]
    today = date.today().isoformat()
    cur = conn.execute(
        "INSERT INTO seasons (name, starts_on, ends_on, is_current, created_at)"
        " VALUES (?, ?, NULL, 1, ?)",
        ("Season 1", today, today),
    )
    return cur.lastrowid


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """Upgrade a pre-divisions database: singles only, plain-text PINs."""
    legacy_players = _table_exists(conn, "players") and "pin" in _columns(conn, "players")
    legacy_matches = _table_exists(conn, "matches") and "winner_id" in _columns(conn, "matches")

    if legacy_players:
        conn.execute("ALTER TABLE players RENAME TO players_v0")
    if legacy_matches:
        conn.execute("ALTER TABLE matches RENAME TO matches_v0")

    conn.executescript(SCHEMA_V1)
    season_id = _ensure_season(conn)

    if legacy_players:
        for row in conn.execute(
            "SELECT id, name, email, pin, active, joined_on FROM players_v0"
        ).fetchall():
            pin_hash, salt = hash_pin(row[3] or f"{secrets.randbelow(10000):04d}")
            conn.execute(
                "INSERT INTO players (id, name, email, pin_hash, pin_salt, category,"
                " active, joined_on) VALUES (?,?,?,?,?,'unspecified',?,?)",
                (row[0], row[1], row[2], pin_hash, salt, row[4], row[5]),
            )
        conn.execute("DROP TABLE players_v0")

    if legacy_matches:
        for row in conn.execute("SELECT * FROM matches_v0").fetchall():
            m = dict(row)
            winner_side = "a" if m["winner_id"] == m["player_a"] else "b"
            conn.execute(
                "INSERT INTO matches (id, played_on, season_id, division, player_a,"
                " player_a2, player_b, player_b2, winner_side, score, games_a,"
                " games_b, retired, walkover, status, submitted_by, confirmed_by,"
                " submitted_at, confirmed_at, note)"
                " VALUES (?,?,?,?,?,NULL,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    m["id"], m["played_on"], season_id, LEGACY_DIVISION,
                    m["player_a"], m["player_b"], winner_side, m["score"],
                    m["games_a"], m["games_b"], m["retired"], m["walkover"],
                    m["status"], m["submitted_by"], m["confirmed_by"],
                    m["submitted_at"], m["confirmed_at"], m["note"],
                ),
            )
        conn.execute("DROP TABLE matches_v0")


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Add the match-request notification toggle.

    Defaults on: being asked for a match is the thing you most need to hear
    about, since otherwise you only find out by happening to open the site.
    """
    if "notify_request" not in _columns(conn, "players"):
        conn.execute("ALTER TABLE players ADD COLUMN notify_request"
                     " INTEGER NOT NULL DEFAULT 1")


MIGRATIONS: List[Callable[[sqlite3.Connection], None]] = [
    _migrate_v0_to_v1,      # index 0 upgrades version 0 -> 1
    _migrate_v1_to_v2,      # index 1 upgrades version 1 -> 2
    _migrate_v2_to_v3,      # index 2 upgrades version 2 -> 3
]


def migrate(conn: sqlite3.Connection) -> int:
    """Bring a database up to SCHEMA_VERSION. Returns the version it started at."""
    conn.row_factory = sqlite3.Row
    started_at = conn.execute("PRAGMA user_version").fetchone()[0]

    if started_at == 0 and not _table_exists(conn, "players"):
        # A brand-new database: create the current schema directly.
        conn.executescript(SCHEMA_V1)
        conn.executescript(SCHEMA_V2)
        _ensure_season(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return started_at

    for version in range(started_at, SCHEMA_VERSION):
        MIGRATIONS[version](conn)
        conn.execute(f"PRAGMA user_version = {version + 1}")
    # Bring an already-current database in line with any added indexes.
    conn.executescript(SCHEMA_V1)
    conn.executescript(SCHEMA_V2)
    _ensure_season(conn)
    conn.commit()
    return started_at
