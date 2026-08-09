"""Finding times to play, and asking someone for one.

This sits between availability (who is free when) and the match record (who
actually played). Its job is to turn "we should play sometime" into a specific
time both people have agreed to.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Sequence

from .availability import Slot, mutual_slots
from .config import Config
from .storage import (
    REQUEST_ACCEPTED, REQUEST_CANCELLED, REQUEST_DECLINED, REQUEST_PENDING,
    REQUEST_PLAYED, Database, MatchRequest,
)


class SchedulingError(Exception):
    """Something the user did wrong, phrased for the user."""


class Scheduler:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ----------------------------------------------------------- match length
    def format_minutes(self, match_format: str) -> int:
        spec = self.config.match_formats.get(match_format)
        if not spec:
            spec = self.config.match_formats.get(self.config.default_match_format, {})
        return int(spec.get("minutes", 60))

    def format_label(self, match_format: str) -> str:
        spec = self.config.match_formats.get(match_format, {})
        return spec.get("label", match_format.replace("_", " ").title())

    # ------------------------------------------------------------ suggestions
    def suggest(
        self, player_a: int, player_b: int, *, match_format: Optional[str] = None,
        days: Optional[int] = None, not_after: Optional[str] = None,
    ) -> List[Slot]:
        """Times both players are free for long enough, soonest first.

        Returns an empty list when there is no overlap -- which is a useful
        answer in itself, and the UI says so rather than pretending.
        """
        match_format = match_format or self.config.default_match_format
        minutes = self.format_minutes(match_format)
        deadline = None
        if not_after:
            try:
                deadline = datetime.strptime(not_after, "%Y-%m-%d").date()
            except ValueError:
                deadline = None
        return mutual_slots(
            self.db.get_availability(player_a),
            self.db.get_availability(player_b),
            minutes=minutes,
            days=days or self.config.exception_horizon_days,
            limit=self.config.suggestion_count,
            not_after=deadline,
        )

    def has_availability(self, player_id: int) -> bool:
        return self.db.get_availability(player_id).has_any()

    # --------------------------------------------------------------- requests
    def request_match(
        self, *, division: str, from_player: int, to_player: int,
        starts_at: str, match_format: Optional[str] = None, message: str = "",
        tournament_match_id: Optional[int] = None,
    ) -> MatchRequest:
        if from_player == to_player:
            raise SchedulingError("You can't request a match with yourself.")
        if not self.db.get_player(to_player):
            raise SchedulingError("That player isn't on the ladder.")

        when = self._parse_when(starts_at)
        if when < datetime.now():
            raise SchedulingError("That time has already passed.")

        match_format = match_format or self.config.default_match_format
        minutes = self.format_minutes(match_format)

        # Don't let someone stack three pending asks on the same opponent.
        existing = [
            r for r in self.db.list_match_requests(
                player_id=from_player, status=REQUEST_PENDING)
            if r.other(from_player) == to_player
        ]
        if len(existing) >= 3:
            raise SchedulingError(
                "You already have three pending requests with this player. "
                "Wait for a reply, or cancel one.")

        request_id = self.db.add_match_request(
            division=division, from_player=from_player, to_player=to_player,
            starts_at=when.isoformat(timespec="minutes"), minutes=minutes,
            match_format=match_format, message=message,
            tournament_match_id=tournament_match_id,
        )
        return self.db.get_match_request(request_id)      # type: ignore[return-value]

    def respond(self, request_id: int, player_id: int, accept: bool) -> MatchRequest:
        request = self.db.get_match_request(request_id)
        if not request:
            raise SchedulingError("That request no longer exists.")
        if request.status != REQUEST_PENDING:
            raise SchedulingError(f"That request is already {request.status}.")
        if player_id != request.to_player:
            raise SchedulingError("Only the player who was asked can answer this.")

        self.db.set_request_status(
            request_id, REQUEST_ACCEPTED if accept else REQUEST_DECLINED)
        if accept:
            # Any other pending request for the same pair is now moot.
            for other in self.db.list_match_requests(
                    player_id=player_id, status=REQUEST_PENDING):
                if other.id != request_id and other.other(player_id) in (
                        request.from_player, request.to_player):
                    self.db.set_request_status(other.id, REQUEST_CANCELLED)
        return self.db.get_match_request(request_id)      # type: ignore[return-value]

    def cancel(self, request_id: int, player_id: int) -> None:
        request = self.db.get_match_request(request_id)
        if not request:
            raise SchedulingError("That request no longer exists.")
        if player_id not in (request.from_player, request.to_player):
            raise SchedulingError("That isn't your request.")
        if request.status not in (REQUEST_PENDING, REQUEST_ACCEPTED):
            raise SchedulingError(f"That request is already {request.status}.")
        self.db.set_request_status(request_id, REQUEST_CANCELLED)

    def close_played(self, player_ids: Sequence[int], division: str) -> None:
        """Mark an accepted request as played once the result comes in.

        Best-effort tidying: if these two had a scheduled match in this
        division, it isn't outstanding any more.
        """
        if len(player_ids) < 2:
            return
        for request in self.db.list_match_requests(
                player_id=player_ids[0], status=REQUEST_ACCEPTED):
            if (request.division == division
                    and request.other(player_ids[0]) in player_ids[1:]):
                self.db.set_request_status(request.id, REQUEST_PLAYED)

    # ------------------------------------------------------------------ views
    def inbox(self, player_id: int) -> List[MatchRequest]:
        """Requests waiting on this player's answer."""
        return [r for r in self.db.list_match_requests(
            player_id=player_id, status=REQUEST_PENDING)
            if r.to_player == player_id]

    def outbox(self, player_id: int) -> List[MatchRequest]:
        return [r for r in self.db.list_match_requests(
            player_id=player_id, status=REQUEST_PENDING)
            if r.from_player == player_id]

    def scheduled(self, player_id: int) -> List[MatchRequest]:
        """Agreed matches that haven't been played yet."""
        return [r for r in self.db.list_match_requests(
            player_id=player_id, status=REQUEST_ACCEPTED) if not r.is_past]

    @staticmethod
    def _parse_when(value: str) -> datetime:
        text = (value or "").strip().replace(" ", "T")
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        raise SchedulingError(
            f"{value!r} isn't a date and time. Pick one of the suggested slots.")
