"""Tunable settings for the ladder.

Everything a club might want to change lives here. On first run a `config.json`
is written into the data directory; edit that file (no Python needed) and
restart. Values in config.json override the defaults below.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from typing import Dict, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The data directory holds everything that must survive a redeploy: the
# database and this config. Overridable so it can point at a mounted volume
# (Fly, Docker) via --data-dir or LADDER_DATA_DIR.
DATA_DIR = os.environ.get("LADDER_DATA_DIR") or os.path.join(PROJECT_ROOT, "data")


def data_path(*parts: str) -> str:
    return os.path.join(DATA_DIR, *parts)


DB_PATH = data_path("ladder.db")
CONFIG_PATH = data_path("config.json")


def set_data_dir(path: str) -> None:
    """Point the app at a different data directory (used by --data-dir)."""
    global DATA_DIR, DB_PATH, CONFIG_PATH
    DATA_DIR = os.path.abspath(path)
    DB_PATH = data_path("ladder.db")
    CONFIG_PATH = data_path("config.json")


@dataclass
class Config:
    # --- club identity -------------------------------------------------
    club_name: str = "Tennis Ladder"

    # --- Glicko-2 rating system ----------------------------------------
    initial_rating: float = 1500.0
    initial_rd: float = 350.0          # uncertainty of a brand-new player
    initial_volatility: float = 0.06
    tau: float = 0.5                   # system constant; 0.3-1.2. Lower = steadier
    max_rd: float = 350.0              # inactivity can't push uncertainty past this
    min_rd: float = 30.0               # never claim more certainty than this

    # How many days of matches form one rating period. Glicko-2 is defined in
    # terms of periods: everyone who played in the period is updated together,
    # everyone who didn't gets a little more uncertain.
    rating_period_days: int = 7

    # --- how much the scoreline matters --------------------------------
    # 0.0 = pure win/loss. 1.0 = pure games ratio. 0.2 means a 6-0 6-0 win is
    # worth noticeably more than a 7-6 7-6 win, but winning still dominates.
    margin_weight: float = 0.20
    # Treat a deciding set of 10+ points as a match tie-break worth one game,
    # so a super-tiebreak doesn't outweigh the two real sets.
    match_tiebreak_in_decider: bool = True

    # --- ladder presentation -------------------------------------------
    # Ladder order uses a conservative rating: rating - (k x RD), so a player is
    # ranked on what we're confident they're worth rather than on one good day.
    #
    # k = 1.0 rather than 2.0 because college seasons are short: at k = 2 a
    # player with three matches (RD ~183) carries a 366-point penalty and sits
    # near the bottom whatever they do, which ranks the ladder by attendance
    # instead of ability. At k = 1 they land near their honest estimate.
    conservative_k: float = 1.0
    # "Provisional" until this many matches have been played -- or sooner, if
    # uncertainty is already below provisional_rd, which is what happens for a
    # player whose rating carried over from last season. The two conditions are
    # combined with `and` (see engine.py): treating low RD as a second hurdle
    # would keep the label on for ~7 matches in a club's first season, since
    # early opponents are unknown too.
    provisional_matches: int = 3
    provisional_rd: float = 200.0

    # --- divisions and seasons ------------------------------------------
    # Which of the five ladders this club runs. Trim the list to hide any.
    enabled_divisions: List[str] = field(default_factory=lambda: [
        "mens_singles", "womens_singles",
        "mens_doubles", "womens_doubles", "mixed_doubles",
    ])
    # Carrying ratings into a new season: the rating comes across, but
    # uncertainty is widened to at least this, so last season seeds the next
    # without freezing it. Set season_carryover False for a clean slate.
    season_carryover: bool = True
    season_carryover_rd: float = 150.0
    # A player's first match in a doubles division starts from their singles
    # rating at that moment, held loosely. A hint, not a claim -- a genuinely
    # different doubles player corrects it within a few matches.
    cross_division_seed: bool = True
    cross_division_rd: float = 300.0

    # --- match formats -------------------------------------------------
    # How long each format takes on court, used to find gaps in two players'
    # availability that are actually long enough to play in. Include warm-up
    # and changeover in the estimate -- a slot that only just fits is no use.
    match_formats: Dict[str, dict] = field(default_factory=lambda: {
        "one_set": {"label": "One set", "minutes": 60,
                    "example": "6-4"},
        "two_sets_tb": {"label": "Two sets + match tie-break", "minutes": 90,
                        "example": "6-4 4-6 10-8"},
        "best_of_three": {"label": "Best of three sets", "minutes": 120,
                          "example": "6-4 3-6 6-2"},
    })
    default_match_format: str = "one_set"

    # --- availability and scheduling ------------------------------------
    # The window the availability grid covers, and how finely it is chopped.
    # Coarse blocks are deliberate: nobody fills in a 15-minute grid.
    day_starts_at: int = 8 * 60          # 08:00, in minutes from midnight
    day_ends_at: int = 22 * 60           # 22:00
    availability_slot_minutes: int = 30
    # How far ahead the "block out a specific day" view runs.
    exception_horizon_days: int = 14
    # How many suggested times to offer when two players look for a match.
    suggestion_count: int = 6

    # --- challenge rules (advisory: shown in the UI, never enforced) ----
    challenge_up_positions: int = 3     # you may challenge up to N spots above you
    # 0 disables the quick-rematch warning entirely. College teams replay each
    # other within days and the warning would just be noise.
    rematch_cooldown_days: int = 0

    # --- workflow -------------------------------------------------------
    require_confirmation: bool = True   # opponent must confirm before it counts
    require_pin: bool = True            # players use a 4-digit PIN to act
    admin_password: str = "changeme"    # set this! used for /admin
    # Signs sessions and unsubscribe links. Generated on first run.
    secret_key: str = ""

    # --- email (optional; all off until smtp_host is set) ----------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_from: str = ""
    # Used to build links in emails, e.g. "https://ladder.example.edu".
    base_url: str = ""

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @classmethod
    def load(cls) -> "Config":
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(CONFIG_PATH):
            cfg = cls(secret_key=secrets.token_urlsafe(32))
            cfg.save()
            return cfg
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in raw.items() if k in known})
        if not cfg.secret_key:
            # Upgrading a config written before signing existed.
            cfg.secret_key = secrets.token_urlsafe(32)
            cfg.save()
        return cfg

    def save(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
            fh.write("\n")


CONFIG = Config.load()
