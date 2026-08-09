"""The operations the app actually performs, independent of the web layer.

Everything the UI can do goes through here, so the CLI tools, the tests and
the HTTP handlers all share one set of rules.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, List, Optional, Sequence, Tuple

from . import divisions as div
from . import tournaments
from .config import Config
from .divisions import DivisionError
from .engine import LadderEngine
from .scheduling import Scheduler
from .scoring import ScoreError, parse_score
from .storage import (
    CONFIRMED, PENDING, REJECTED, SIDE_A, SIDE_B, Database, Match, Player,
)


class ServiceError(Exception):
    """Something the user did wrong, phrased for the user."""


@dataclass
class Submission:
    match_id: int
    status: str
    warning: Optional[str] = None


class LadderService:
    def __init__(self, db: Database, config: Config, notifier: Optional[Callable] = None):
        self.db = db
        self.config = config
        self.engine = LadderEngine(db, config)
        self.scheduler = Scheduler(db, config)
        # Called as notifier(event, **context). Injected so the service never
        # depends on email being configured, or working.
        self.notifier = notifier

    def _notify(self, event: str, **context) -> None:
        if not self.notifier:
            return
        try:
            self.notifier(event, **context)
        except Exception:                    # noqa: BLE001
            # A notification must never break the thing it is reporting on.
            pass

    # ----------------------------------------------------------- submissions
    def submit_result(
        self,
        *,
        division: str,
        side_a: Sequence[int],
        side_b: Sequence[int],
        score_text: str,
        played_on: str,
        submitted_by: Optional[int] = None,
        winner_side: Optional[str] = None,
        note: str = "",
        auto_confirm: bool = False,
        allow_category_override: bool = False,
        season_id: Optional[int] = None,
    ) -> Submission:
        """Record a result. Score is written from side A's point of view."""
        side_a = [int(p) for p in side_a if p]
        side_b = [int(p) for p in side_b if p]

        if not div.is_division(division):
            raise ServiceError(f"{division!r} is not one of the club's divisions.")
        if division not in self.config.enabled_divisions:
            raise ServiceError(
                f"{div.get(division).label} isn't one of this club's ladders.")

        players_a = [self._player(p) for p in side_a]
        players_b = [self._player(p) for p in side_b]
        try:
            div.validate_lineup(division, players_a, players_b,
                                override=allow_category_override)
        except DivisionError as exc:
            raise ServiceError(str(exc)) from None

        played_on = (played_on or "").strip() or date.today().isoformat()
        self._check_date(played_on)

        try:
            parsed = parse_score(
                score_text,
                match_tiebreak_in_decider=self.config.match_tiebreak_in_decider,
            )
        except ScoreError as exc:
            raise ServiceError(str(exc)) from None

        if parsed.a_won is None:
            # A bare walkover carries no direction, so the form must say who won.
            if winner_side not in (SIDE_A, SIDE_B):
                raise ServiceError("For a walkover, say which side advanced.")
            resolved = winner_side
        else:
            resolved = SIDE_A if parsed.a_won else SIDE_B
            if winner_side is not None and winner_side != resolved:
                won = players_a if resolved == SIDE_A else players_b
                names = " and ".join(p.name for p in won)
                raise ServiceError(
                    f"The score {parsed.normalised} says {names} won. "
                    "Check the score, or swap the sides around."
                )

        warning = self.engine.rematch_warning(side_a, side_b, played_on)
        season = (self.db.get_season(season_id) if season_id
                  else self.db.current_season())
        if not season:
            raise ServiceError("No season is open. Start one in Admin.")

        status = CONFIRMED if (auto_confirm or not self.config.require_confirmation) else PENDING
        match_id = self.db.add_match(
            played_on=played_on, season_id=season.id, division=division,
            side_a=side_a, side_b=side_b, winner_side=resolved,
            score=parsed.normalised, games_a=parsed.games_a, games_b=parsed.games_b,
            retired=parsed.retired, walkover=parsed.walkover, status=status,
            submitted_by=submitted_by, note=note.strip(),
        )
        self.engine.invalidate()

        if status == PENDING:
            self._notify("confirm_needed", match=self.db.get_match(match_id))
        else:
            # A result entered as already-confirmed (admin, or an import) never
            # passes through confirm(), so the tournament draw has to be moved
            # on from here as well.
            self._settle_confirmed(self.db.get_match(match_id))
        return Submission(match_id=match_id, status=status, warning=warning)

    def _settle_confirmed(self, match: Optional[Match]) -> None:
        """Everything that follows a result counting: draws and schedules."""
        if not match:
            return
        self._advance_tournaments(match)
        self.scheduler.close_played(match.players, match.division)

    def confirm(self, match_id: int, actor_id: Optional[int], *,
                is_admin: bool = False) -> Match:
        match = self._pending(match_id)
        if not is_admin:
            if actor_id is None:
                raise ServiceError("Sign in as one of the players to confirm.")
            if actor_id not in match.players:
                raise ServiceError("Only the players in this match (or an admin) "
                                   "can confirm it.")
            # One opponent is enough. Requiring the whole other side would stall
            # doubles constantly; allowing your own partner would let the pair
            # that entered the result also rubber-stamp it.
            if actor_id in self._submitters_side(match):
                raise ServiceError(
                    "Someone on your side submitted this one -- it needs a "
                    "player from the other team to confirm it."
                )
        self.db.set_match_status(match_id, CONFIRMED, actor_id=actor_id)
        self.engine.invalidate()
        confirmed = self.db.get_match(match_id)
        self._settle_confirmed(confirmed)
        self._notify("result_confirmed", match=confirmed, actor_id=actor_id)
        return confirmed                            # type: ignore[return-value]

    def reject(self, match_id: int, actor_id: Optional[int], *,
               is_admin: bool = False) -> None:
        match = self._pending(match_id)
        if not is_admin and actor_id not in match.players:
            raise ServiceError("Only the players in this match (or an admin) "
                               "can dispute it.")
        self.db.set_match_status(match_id, REJECTED, actor_id=actor_id)
        self.engine.invalidate()
        self._notify("result_disputed", match=self.db.get_match(match_id),
                     actor_id=actor_id)

    def delete_match(self, match_id: int) -> None:
        if not self.db.get_match(match_id):
            raise ServiceError("That match no longer exists.")
        self.db.delete_match(match_id)
        self.engine.invalidate()

    def pending_for(self, player_id: Optional[int]) -> List[Match]:
        """Pending results this player still needs to act on."""
        pending = self.db.list_matches(status=PENDING)
        if player_id is None:
            return pending
        return [
            m for m in pending
            if player_id in m.players and player_id not in self._submitters_side(m)
        ]

    def submitted_by_side_of(self, player_id: int) -> List[Match]:
        """Pending results this player's own side put in, awaiting the opponent."""
        return [
            m for m in self.db.list_matches(status=PENDING)
            if player_id in m.players and player_id in self._submitters_side(m)
        ]

    # ------------------------------------------------------------ tournaments
    def create_tournament(
        self, *, name: str, division: str, style: str, seeding: str,
        match_format: str, player_ids: Sequence[int],
        round_days: int = 7, start_on: Optional[str] = None,
    ):
        """Set up a tournament and generate its draw.

        Entrants are seeded by their current ladder position unless a random
        draw was asked for. Each round gets a play-by date; players arrange the
        actual times between themselves.
        """
        if style not in tournaments.STYLES:
            raise ServiceError(f"{style!r} isn't a tournament format.")
        if seeding not in tournaments.SEEDINGS:
            raise ServiceError(f"{seeding!r} isn't a seeding method.")
        if not div.is_division(division):
            raise ServiceError(f"{division!r} is not one of the club's divisions.")
        if div.get(division).is_doubles:
            raise ServiceError(
                "Tournaments run in the singles divisions for now.")
        if match_format not in self.config.match_formats:
            raise ServiceError(f"{match_format!r} isn't a known match format.")

        entrants = [int(p) for p in player_ids]
        if len(set(entrants)) != len(entrants):
            raise ServiceError("The same player is entered twice.")
        if len(entrants) < 2:
            raise ServiceError("A tournament needs at least two players.")
        for player_id in entrants:
            if not self.db.get_player(player_id):
                raise ServiceError("One of those players isn't on the ladder.")

        ordered = self._seed_order(entrants, division, seeding)
        season = self.db.current_season()
        tournament_id = self.db.create_tournament(
            name=name, division=division, season_id=season.id, style=style,
            seeding=seeding, match_format=match_format)
        self.db.set_entries(tournament_id, ordered)
        self._generate_draw(tournament_id, ordered, style, round_days, start_on)
        self.db.set_tournament_status(tournament_id, "running")
        return self.db.get_tournament(tournament_id)

    def _seed_order(self, entrants: Sequence[int], division: str,
                    seeding: str) -> List[int]:
        if seeding == tournaments.RANDOM_SEEDING:
            return tournaments.order_entrants(entrants, seeding)
        ladder = self.engine.ladder(division, include_inactive=True)
        # Ladder order first; anyone with no rating yet goes to the bottom,
        # since seeding them above proven players would be guesswork.
        ranked = sorted(
            entrants,
            key=lambda pid: (ladder.rank_of(pid) or 10_000,
                             (self.db.get_player(pid).name or "").lower()),
        )
        return ranked

    def _generate_draw(self, tournament_id: int, ordered: Sequence[int],
                       style: str, round_days: int,
                       start_on: Optional[str]) -> None:
        if style == tournaments.ELIMINATION:
            draw = tournaments.build_elimination(ordered)
        else:
            draw = tournaments.build_round_robin(ordered)

        rows = []
        for pairings in draw.rounds:
            for p in pairings:
                status = "bye" if p.is_bye else (
                    "ready" if p.player_a and p.player_b else "pending")
                rows.append((p.round_no, p.slot, p.player_a, p.player_b, status))
        self.db.replace_tournament_matches(tournament_id, rows)

        start = self._parse_date(start_on) if start_on else date.today()
        self.db.set_rounds(tournament_id, [
            (index, name,
             (start + timedelta(days=round_days * (index + 1))).isoformat())
            for index, name in enumerate(draw.round_names)
        ])

        if style == tournaments.ELIMINATION:
            self._settle_byes(tournament_id)
        self.engine.invalidate()

    def _settle_byes(self, tournament_id: int) -> None:
        """Walk bye recipients into the next round straight away."""
        for tmatch in self.db.tournament_matches(tournament_id, round_no=0):
            if tmatch.status != "bye":
                continue
            occupant = tmatch.player_a if tmatch.player_a else tmatch.player_b
            if occupant is None:
                continue
            self.db.update_tournament_match(tmatch.id, winner_id=occupant)
            self._promote(tournament_id, tmatch.round_no, tmatch.slot, occupant)

    def _promote(self, tournament_id: int, round_no: int, slot: int,
                 winner_id: int) -> None:
        """Put a winner into their next-round slot."""
        next_round, next_slot, side = tournaments.parent_slot(round_no, slot)
        for candidate in self.db.tournament_matches(tournament_id, round_no=next_round):
            if candidate.slot != next_slot:
                continue
            if side == "a":
                self.db.update_tournament_match(candidate.id, player_a=winner_id)
            else:
                self.db.update_tournament_match(candidate.id, player_b=winner_id)
            refreshed = self.db.get_tournament_match(candidate.id)
            if refreshed and refreshed.is_ready:
                self.db.update_tournament_match(candidate.id, status="ready")
            return

    def _advance_tournaments(self, match: Match) -> None:
        """Link a confirmed result to a tournament match and move the draw on.

        Matched by players and division rather than asking anyone to tag the
        result: people submit a score, they shouldn't have to remember it was
        also round two of something.
        """
        if match.is_doubles:
            return
        players = set(match.players)
        for tournament in self.db.list_tournaments():
            if tournament.division != match.division or tournament.status != "running":
                continue
            for tmatch in self.db.tournament_matches(tournament.id):
                if tmatch.winner_id is not None or set(tmatch.players) != players:
                    continue
                self.db.update_tournament_match(
                    tmatch.id, winner_id=match.winner_id, match_id=match.id,
                    status="played")
                if tournament.style == tournaments.ELIMINATION:
                    self._promote(tournament.id, tmatch.round_no, tmatch.slot,
                                  match.winner_id)
                self._check_complete(tournament.id)
                return

    def _check_complete(self, tournament_id: int) -> None:
        outstanding = [t for t in self.db.tournament_matches(tournament_id)
                       if t.winner_id is None]
        if not outstanding:
            self.db.set_tournament_status(tournament_id, "complete")

    def tournament_standings(self, tournament_id: int):
        """Round-robin table, ordered."""
        entrants = [pid for pid, _ in self.db.entries(tournament_id)]
        results = []
        for tmatch in self.db.tournament_matches(tournament_id):
            if tmatch.winner_id is None or tmatch.match_id is None:
                continue
            match = self.db.get_match(tmatch.match_id)
            if not match:
                continue
            won, lost = match.games_for(tmatch.winner_id)
            results.append({"winner": tmatch.winner_id, "loser": tmatch.loser(),
                            "winner_games": won, "loser_games": lost})
        return tournaments.standings(entrants, results)

    def overdue_matches(self, tournament_id: int) -> List:
        """Matches whose round deadline has passed and still aren't played."""
        deadlines = {r.round_no: r for r in self.db.rounds(tournament_id)}
        out = []
        for tmatch in self.db.tournament_matches(tournament_id):
            round_info = deadlines.get(tmatch.round_no)
            if (tmatch.winner_id is None and tmatch.is_ready
                    and round_info and round_info.is_overdue):
                out.append(tmatch)
        return out

    def force_tournament_winner(self, tmatch_id: int, winner_id: int) -> None:
        """Admin decision for a match that never got played."""
        tmatch = self.db.get_tournament_match(tmatch_id)
        if not tmatch:
            raise ServiceError("That tournament match no longer exists.")
        if winner_id not in tmatch.players:
            raise ServiceError("That player isn't in this match.")
        tournament = self.db.get_tournament(tmatch.tournament_id)
        self.db.update_tournament_match(tmatch_id, winner_id=winner_id,
                                        status="played")
        if tournament and tournament.style == tournaments.ELIMINATION:
            self._promote(tournament.id, tmatch.round_no, tmatch.slot, winner_id)
        self._check_complete(tmatch.tournament_id)

    @staticmethod
    def _parse_date(value: str) -> date:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise ServiceError(f"{value!r} isn't a date. Use YYYY-MM-DD.") from None

    # --------------------------------------------------------------- seasons
    def start_season(self, name: str, starts_on: Optional[str] = None):
        try:
            season = self.db.start_season(name, starts_on)
        except ValueError as exc:
            raise ServiceError(str(exc)) from None
        self.engine.invalidate()
        self._notify("season_started", season=season)
        return season

    # --------------------------------------------------------------- players
    def add_player(self, name: str, email: str = "", pin: str = "",
                   category: str = div.UNSPECIFIED) -> Player:
        if category not in div.CATEGORIES:
            raise ServiceError(f"{category!r} is not a valid category.")
        try:
            player = self.db.add_player(name, email, pin, category)
        except ValueError as exc:
            raise ServiceError(str(exc)) from None
        self.engine.invalidate()
        return player

    def authenticate(self, player_id: int, pin: str) -> Player:
        player = self.db.get_player(player_id)
        if not player:
            raise ServiceError("Unknown player.")
        if not self.config.require_pin:
            return player
        if not self.db.check_pin(player_id, pin):
            raise ServiceError("That PIN doesn't match. Ask the ladder admin "
                               "if you've lost it.")
        return player

    # ------------------------------------------------------------------- CSV
    CSV_HEADER = ["date", "division", "player_a", "player_a2",
                  "player_b", "player_b2", "score", "note"]

    def import_csv(self, text: str, *, auto_confirm: bool = True,
                   create_players: bool = True,
                   season_id: Optional[int] = None) -> Tuple[int, List[str]]:
        """Bulk-load results.

        Columns: date,division,player_a,player_b,score plus optional
        player_a2 / player_b2 for doubles and a note. The score is always
        written from side A's perspective.
        """
        stream = io.StringIO(text.strip())
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ServiceError("The CSV is empty.")
        present = {(f or "").strip().lower() for f in reader.fieldnames}
        missing = {"date", "division", "player_a", "player_b", "score"} - present
        if missing:
            raise ServiceError(
                "The CSV needs these columns: date, division, player_a, "
                "player_b, score (plus player_a2/player_b2 for doubles). "
                f"Missing: {', '.join(sorted(missing))}."
            )

        imported, errors = 0, []
        for line_no, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in raw.items() if k}
            if not any(row.values()):
                continue
            try:
                division = self._resolve_division(row["division"])
                side_a = [self._player_for_import(row["player_a"], create_players).id]
                side_b = [self._player_for_import(row["player_b"], create_players).id]
                if row.get("player_a2"):
                    side_a.append(self._player_for_import(row["player_a2"], create_players).id)
                if row.get("player_b2"):
                    side_b.append(self._player_for_import(row["player_b2"], create_players).id)
                self.submit_result(
                    division=division, side_a=side_a, side_b=side_b,
                    score_text=row["score"], played_on=row["date"],
                    note=row.get("note", ""), auto_confirm=auto_confirm,
                    allow_category_override=True, season_id=season_id,
                )
                imported += 1
            except (ServiceError, ValueError, KeyError) as exc:
                errors.append(f"Line {line_no}: {exc}")
        self.engine.invalidate()
        return imported, errors

    def export_matches_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["date", "season", "division", "player_a", "player_a2",
                         "player_b", "player_b2", "score", "winner", "status", "note"])
        names = {p.id: p.name for p in self.db.list_players()}
        seasons = {s.id: s.name for s in self.db.seasons()}
        for match in reversed(self.db.list_matches()):
            writer.writerow([
                match.played_on,
                seasons.get(match.season_id, ""),
                match.division,
                names.get(match.player_a, "?"),
                names.get(match.player_a2, "") if match.player_a2 else "",
                names.get(match.player_b, "?"),
                names.get(match.player_b2, "") if match.player_b2 else "",
                match.score,
                " & ".join(names.get(p, "?") for p in match.winners),
                match.status,
                match.note,
            ])
        return out.getvalue()

    def export_ladder_csv(self, division: str, season_id: Optional[int] = None) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(
            ["rank", "player", "ladder_points", "rating", "rd", "played",
             "won", "lost", "win_pct", "provisional"]
        )
        for entry in self.engine.ladder(division, season_id).entries:
            writer.writerow([
                entry.rank, entry.player.name, round(entry.ladder_points),
                round(entry.rating.rating), round(entry.rating.rd),
                entry.played, entry.won, entry.lost, round(entry.win_pct, 1),
                "yes" if entry.provisional else "no",
            ])
        return out.getvalue()

    # ------------------------------------------------------------- internals
    @staticmethod
    def _submitters_side(match: Match) -> List[int]:
        """The players on the same side as whoever submitted the result."""
        if match.submitted_by is None:
            return []
        side = match.side_of(match.submitted_by)
        if side is None:
            return [match.submitted_by]
        return match.side_a if side == SIDE_A else match.side_b

    @staticmethod
    def _resolve_division(text: str) -> str:
        key = (text or "").strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "ms": div.MENS_SINGLES, "mens": div.MENS_SINGLES,
            "ws": div.WOMENS_SINGLES, "womens": div.WOMENS_SINGLES,
            "md": div.MENS_DOUBLES, "wd": div.WOMENS_DOUBLES,
            "xd": div.MIXED_DOUBLES, "mixed": div.MIXED_DOUBLES,
        }
        key = aliases.get(key, key)
        if not div.is_division(key):
            raise ServiceError(
                f"{text!r} isn't a division. Use one of: "
                + ", ".join(div.DIVISION_ORDER)
            )
        return key

    def _player(self, player_id: int) -> Player:
        player = self.db.get_player(player_id)
        if not player:
            raise ServiceError("One of those players isn't on the ladder.")
        return player

    def _player_for_import(self, name: str, create: bool) -> Player:
        if not name:
            raise ServiceError("A row is missing a player name.")
        player = self.db.find_player_by_name(name)
        if player:
            return player
        if not create:
            raise ServiceError(f"{name!r} isn't on the ladder.")
        return self.add_player(name)

    def _pending(self, match_id: int) -> Match:
        match = self.db.get_match(match_id)
        if not match:
            raise ServiceError("That result no longer exists.")
        if match.status != PENDING:
            raise ServiceError(f"That result is already {match.status}.")
        return match

    @staticmethod
    def _check_date(value: str) -> None:
        try:
            played = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise ServiceError(f"{value!r} isn't a date. Use YYYY-MM-DD.") from None
        if played > date.today():
            raise ServiceError("That date is in the future.")
