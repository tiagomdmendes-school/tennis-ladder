"""Schema migration.

The ladder's whole value is its history, so an upgrade that loses or corrupts
matches is the worst thing this codebase could do. These tests build a database
in the old shape and check every field survives the move.
"""

import os
import sqlite3
import tempfile
import unittest

from ladder.migrations import SCHEMA_VERSION, hash_pin, verify_pin
from ladder.storage import Database

# Exactly the schema the pre-divisions version created: singles only,
# winner_id rather than winner_side, plain-text PINs, no seasons.
SCHEMA_V0 = """
CREATE TABLE players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL DEFAULT '',
    pin TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    joined_on TEXT NOT NULL);
CREATE TABLE matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    played_on TEXT NOT NULL,
    player_a INTEGER NOT NULL,
    player_b INTEGER NOT NULL,
    winner_id INTEGER NOT NULL,
    score TEXT NOT NULL,
    games_a INTEGER NOT NULL DEFAULT 0,
    games_b INTEGER NOT NULL DEFAULT 0,
    retired INTEGER NOT NULL DEFAULT 0,
    walkover INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    submitted_by INTEGER,
    confirmed_by INTEGER,
    submitted_at TEXT NOT NULL,
    confirmed_at TEXT,
    note TEXT NOT NULL DEFAULT '');
"""


def build_v0(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_V0)
    conn.execute("INSERT INTO players (name,email,pin,active,joined_on)"
                 " VALUES ('Ana Silva','ana@school.edu','1234',1,'2026-01-01')")
    conn.execute("INSERT INTO players (name,email,pin,active,joined_on)"
                 " VALUES ('Ben Okafor','','5678',0,'2026-01-02')")
    # Ben wins the first (player_a lost it), Ana wins the second.
    conn.execute("INSERT INTO matches (played_on,player_a,player_b,winner_id,score,"
                 "games_a,games_b,status,submitted_by,confirmed_by,submitted_at,"
                 "confirmed_at,note) VALUES ('2026-02-01',1,2,2,'6-7 1-6',7,13,"
                 "'confirmed',1,2,'2026-02-01T10:00','2026-02-01T11:00','legacy')")
    conn.execute("INSERT INTO matches (played_on,player_a,player_b,winner_id,score,"
                 "games_a,games_b,retired,status,submitted_by,submitted_at,note)"
                 " VALUES ('2026-02-08',1,2,1,'6-3 2-1 ret.',8,4,1,'pending',1,"
                 "'2026-02-08T10:00','')")
    conn.commit()
    conn.close()


class TestV0Upgrade(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.remove(self.path)
        build_v0(self.path)
        self.db = Database(self.path)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_it_reports_the_version_it_came_from(self):
        self.assertEqual(self.db.migrated_from, 0)

    def test_the_version_is_stamped_afterwards(self):
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         SCHEMA_VERSION)
        conn.close()

    def test_players_survive_with_their_fields(self):
        ana = self.db.find_player_by_name("Ana Silva")
        self.assertEqual(ana.email, "ana@school.edu")
        self.assertTrue(ana.active)
        self.assertEqual(ana.joined_on, "2026-01-01")
        self.assertFalse(self.db.find_player_by_name("Ben Okafor").active)

    def test_categories_default_to_unspecified(self):
        self.assertEqual(self.db.find_player_by_name("Ana Silva").category,
                         "unspecified")

    def test_pins_still_work_but_are_no_longer_readable(self):
        self.assertTrue(self.db.check_pin(1, "1234"))
        self.assertFalse(self.db.check_pin(1, "9999"))
        conn = sqlite3.connect(self.path)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(players)")]
        conn.close()
        self.assertNotIn("pin", columns)
        self.assertIn("pin_hash", columns)

    def test_matches_survive_with_the_right_winners(self):
        matches = {m.id: m for m in self.db.list_matches()}
        self.assertEqual(len(matches), 2)
        # winner_id 2 on side b -> winner_side 'b'
        self.assertEqual(matches[1].winner_side, "b")
        self.assertEqual(matches[1].winners, [2])
        self.assertEqual(matches[1].losers, [1])
        self.assertEqual(matches[2].winner_side, "a")
        self.assertEqual(matches[2].winners, [1])

    def test_match_details_are_preserved(self):
        match = self.db.get_match(1)
        self.assertEqual(match.score, "6-7 1-6")
        self.assertEqual((match.games_a, match.games_b), (7, 13))
        self.assertEqual(match.status, "confirmed")
        self.assertEqual(match.note, "legacy")
        self.assertEqual(match.submitted_by, 1)
        self.assertEqual(match.confirmed_by, 2)
        self.assertTrue(self.db.get_match(2).retired)
        self.assertEqual(self.db.get_match(2).status, "pending")

    def test_legacy_matches_become_singles(self):
        for match in self.db.list_matches():
            self.assertFalse(match.is_doubles)
            self.assertIsNone(match.player_a2)
            self.assertEqual(len(match.players), 2)

    def test_everything_lands_in_one_season(self):
        seasons = self.db.seasons()
        self.assertEqual(len(seasons), 1)
        for match in self.db.list_matches():
            self.assertEqual(match.season_id, seasons[0].id)

    def test_migrating_twice_is_harmless(self):
        again = Database(self.path)
        self.addCleanup(again.close)
        self.assertEqual(again.migrated_from, SCHEMA_VERSION)
        self.assertEqual(len(again.list_matches()), 2)
        self.assertTrue(again.check_pin(1, "1234"))


class TestFreshDatabase(unittest.TestCase):
    def test_a_new_database_is_created_at_the_current_version(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        self.assertEqual(db.migrated_from, 0)
        self.assertEqual(len(db.seasons()), 1)
        self.assertEqual(db.list_players(), [])


class TestPinHashing(unittest.TestCase):
    def test_the_same_pin_hashes_differently_each_time(self):
        first, salt_a = hash_pin("1234")
        second, salt_b = hash_pin("1234")
        self.assertNotEqual(salt_a, salt_b)
        self.assertNotEqual(first, second)

    def test_verification_round_trips(self):
        digest, salt = hash_pin("4321")
        self.assertTrue(verify_pin("4321", digest, salt))
        self.assertFalse(verify_pin("1234", digest, salt))

    def test_missing_hash_or_salt_never_verifies(self):
        self.assertFalse(verify_pin("1234", "", ""))
        self.assertFalse(verify_pin("1234", "abc", ""))

    def test_the_plain_pin_is_not_recoverable_from_storage(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        player = db.add_player("Ana", pin="7777")
        with db.connect() as conn:
            row = dict(conn.execute("SELECT * FROM players WHERE id = ?",
                                    (player.id,)).fetchone())
        self.assertNotIn("7777", str(row))
        self.assertTrue(db.check_pin(player.id, "7777"))


if __name__ == "__main__":
    unittest.main()
