"""Shared fixtures for the test suite."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict

from ladder import divisions as div
from ladder.config import Config
from ladder.service import LadderService
from ladder.storage import Database

MEN = ["Al", "Bo", "Cy", "Dan"]
WOMEN = ["Eve", "Fay", "Gia", "Hana"]


def make_config(**overrides) -> Config:
    base = {"admin_password": "secret", "secret_key": "test-secret",
            "club_name": "Test Club"}
    base.update(overrides)
    return Config(**base)


def fresh(**overrides) -> LadderService:
    """An empty ladder with an in-memory database."""
    return LadderService(Database(":memory:"), make_config(**overrides))


def with_roster(**overrides):
    """A ladder with four men and four women already added.

    Returns (service, ids) where ids maps name -> player id.
    """
    svc = fresh(**overrides)
    ids: Dict[str, int] = {}
    for name in MEN:
        ids[name] = svc.add_player(name, category=div.MENS).id
    for name in WOMEN:
        ids[name] = svc.add_player(name, category=div.WOMENS).id
    return svc, ids


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def play(svc: LadderService, division: str, side_a, side_b, day: int,
         score: str = "6-3 6-3", **kwargs):
    """Record a confirmed result. Sides are lists of player ids."""
    return svc.submit_result(
        division=division,
        side_a=side_a if isinstance(side_a, list) else [side_a],
        side_b=side_b if isinstance(side_b, list) else [side_b],
        score_text=score, played_on=days_ago(day), auto_confirm=True, **kwargs,
    )
