"""Replay the match history and produce every ladder.

The whole rating state is a pure function of the confirmed match list. Nothing
is cached in the database, so correcting a result from last month automatically
corrects every rating that followed it.

The replay is **one chronological pass per season, across all divisions at
once**. Divisions never interact during play -- a men's doubles result changes
nothing in mixed -- but a single interleaved pass is what lets a player's first
doubles match seed from their singles rating *as it stood that day*. Rating
periods are global by date, so everyone's uncertainty ages at the same rate
regardless of which ladders they play in.

For a club ladder this costs nothing: a few thousand matches replay in a few
milliseconds, and the result is memoised until the next write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from . import divisions as div
from .config import Config
from .doubles import match_results, team_win_probability
from .glicko2 import Rating, conservative, decay, rate
from .storage import Database, Match, Player, Season


@dataclass
class RatingPoint:
    """One snapshot of a player's rating, for the history chart."""

    on: str            # ISO date of the period end
    rating: float
    rd: float
    matches_played: int


@dataclass
class PartnerStat:
    """How a player performs alongside one particular partner.

    `over_expectation` is the point of this: raw win/loss can't tell you whether
    a pair is good together, because it doesn't know how hard their matches
    were. Summing (actual - expected) does. A 3-3 record with a weak partner
    against strong opposition scores positive; 6-2 with a star partner against
    nobody scores negative.
    """

    partner_id: int
    division: str
    played: int = 0
    won: int = 0
    actual: float = 0.0
    expected: float = 0.0

    @property
    def lost(self) -> int:
        return self.played - self.won

    @property
    def record(self) -> str:
        return f"{self.won}-{self.lost}"

    @property
    def over_expectation(self) -> float:
        """Total wins above what the ratings predicted."""
        return self.actual - self.expected

    @property
    def per_match(self) -> float:
        """Average over-performance per match, in [-1, 1]."""
        return self.over_expectation / self.played if self.played else 0.0

    @property
    def thin(self) -> bool:
        """Too few matches to read anything into."""
        return self.played < 3


@dataclass
class LadderEntry:
    player: Player
    rank: int
    rating: Rating
    ladder_points: float          # the conservative rating we sort on
    division: str = ""
    played: int = 0
    won: int = 0
    lost: int = 0
    games_won: int = 0
    games_lost: int = 0
    form: List[str] = field(default_factory=list)     # most recent first: W/L
    last_played: Optional[str] = None
    history: List[RatingPoint] = field(default_factory=list)
    peak_rating: float = 0.0
    provisional: bool = True
    rank_change: int = 0           # vs. the previous rating period; + is a climb
    seeded_from: str = ""          # how their starting rating was chosen

    @property
    def win_pct(self) -> float:
        return 100.0 * self.won / self.played if self.played else 0.0

    @property
    def record(self) -> str:
        return f"{self.won}-{self.lost}"

    @property
    def confidence(self) -> str:
        if self.rating.rd <= 60:
            return "high"
        if self.rating.rd <= 110:
            return "medium"
        return "low"


@dataclass
class Ladder:
    division: str
    season_id: int
    entries: List[LadderEntry]
    by_id: Dict[int, LadderEntry]
    periods: int = 0
    last_updated: Optional[str] = None

    def entry(self, player_id: int) -> Optional[LadderEntry]:
        return self.by_id.get(player_id)

    def rank_of(self, player_id: int) -> Optional[int]:
        entry = self.by_id.get(player_id)
        return entry.rank if entry else None

    @property
    def label(self) -> str:
        return div.get(self.division).label


@dataclass
class LadderState:
    """Everything the replay produced, for every season and division."""

    ladders: Dict[Tuple[int, str], Ladder] = field(default_factory=dict)
    partners: Dict[Tuple[int, int, int], PartnerStat] = field(default_factory=dict)
    seasons: List[Season] = field(default_factory=list)


class LadderEngine:
    """Computes and memoises every ladder for a database."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self._state: Optional[LadderState] = None
        self._version = -1

    # ------------------------------------------------------------- accessors
    def state(self) -> LadderState:
        if self._state is None or self._version != self.db.version:
            self._state = self._compute()
            self._version = self.db.version
        return self._state

    def invalidate(self) -> None:
        self._state = None

    def ladder(
        self,
        division: str,
        season_id: Optional[int] = None,
        *,
        include_inactive: bool = False,
    ) -> Ladder:
        season_id = season_id or self.db.current_season().id
        full = self.state().ladders.get((season_id, division))
        if full is None:
            return Ladder(division=division, season_id=season_id, entries=[], by_id={})
        if include_inactive:
            return full
        entries = [e for e in full.entries if e.player.active]
        for position, entry in enumerate(entries, start=1):
            entry.rank = position
        return Ladder(
            division=division, season_id=season_id, entries=entries,
            by_id={e.player.id: e for e in entries},
            periods=full.periods, last_updated=full.last_updated,
        )

    def divisions_played_by(
        self, player_id: int, season_id: Optional[int] = None
    ) -> List[str]:
        """Divisions this player has actually appeared in, in display order."""
        season_id = season_id or self.db.current_season().id
        out = []
        for key in div.DIVISION_ORDER:
            ladder = self.state().ladders.get((season_id, key))
            entry = ladder.entry(player_id) if ladder else None
            if entry and entry.played:
                out.append(key)
        return out

    def partners_of(
        self, player_id: int, season_id: Optional[int] = None
    ) -> List[PartnerStat]:
        """Every partner this player has had, best chemistry first."""
        season_id = season_id or self.db.current_season().id
        stats = [
            stat for (sid, pid, _), stat in self.state().partners.items()
            if sid == season_id and pid == player_id
        ]
        return sorted(stats, key=lambda s: (-s.per_match, -s.played))

    # ------------------------------------------------------------------ core
    def _compute(self) -> LadderState:
        players = {p.id: p for p in self.db.list_players()}
        seasons = self.db.seasons()
        state = LadderState(seasons=seasons)

        # Final ratings from the previous season, keyed (player_id, division).
        carried: Dict[Tuple[int, str], Rating] = {}

        for season in seasons:
            matches = [
                m for m in self.db.confirmed_matches_chronological(season.id)
                if all(pid in players for pid in m.players)
                and div.is_division(m.division)
            ]
            season_state = self._replay_season(season, matches, players, carried)
            state.ladders.update(season_state["ladders"])
            for key, stat in season_state["partners"].items():
                state.partners[key] = stat
            carried = season_state["final"]

        return state

    def _replay_season(
        self,
        season: Season,
        matches: Sequence[Match],
        players: Dict[int, Player],
        carried: Dict[Tuple[int, str], Rating],
    ) -> dict:
        cfg = self.config
        Key = Tuple[int, str]           # (player_id, division)

        ratings: Dict[Key, Rating] = {}
        entered: Dict[Key, bool] = {}
        seeded_from: Dict[Key, str] = {}
        stats: Dict[Key, LadderEntry] = {}
        partners: Dict[Tuple[int, int, int], PartnerStat] = {}
        order_snapshots: Dict[str, List[List[int]]] = {}

        def ensure(player_id: int, division: str) -> Rating:
            """A player's starting rating in a division, chosen on first sight.

            Priority: what they finished last season on, then their own singles
            rating right now, then the club default. Every one of those is
            derived from the player's own results -- nothing here encodes
            anyone's opinion of how good someone is.
            """
            key = (player_id, division)
            if key in ratings:
                return ratings[key]

            prior = carried.get(key) if cfg.season_carryover else None
            if prior is not None:
                ratings[key] = Rating(
                    rating=prior.rating,
                    rd=min(max(prior.rd, cfg.season_carryover_rd), cfg.max_rd),
                    volatility=prior.volatility,
                )
                seeded_from[key] = "last season"
            else:
                seed_div = div.seed_division_for(
                    division, players[player_id].category) if cfg.cross_division_seed else None
                source = ratings.get((player_id, seed_div)) if seed_div else None
                # Only seed from a division they have actually played in --
                # a rating nobody has tested is not a useful hint.
                if source is not None and entered.get((player_id, seed_div)):
                    ratings[key] = Rating(
                        rating=source.rating,
                        rd=min(max(cfg.cross_division_rd, source.rd), cfg.max_rd),
                        volatility=cfg.initial_volatility,
                    )
                    seeded_from[key] = div.get(seed_div).label
                else:
                    ratings[key] = Rating(
                        cfg.initial_rating, cfg.initial_rd, cfg.initial_volatility)
                    seeded_from[key] = "new"

            entered.setdefault(key, False)
            stats[key] = LadderEntry(
                player=players[player_id], rank=0, rating=ratings[key],
                ladder_points=0.0, division=division,
                peak_rating=ratings[key].rating, seeded_from=seeded_from[key],
            )
            return ratings[key]

        periods = _split_into_periods(matches, cfg.rating_period_days)

        for period_end, period_matches in periods:
            buckets: Dict[Key, list] = {}
            touched_divisions = set()

            for match in period_matches:
                division = match.division
                touched_divisions.add(division)

                side_a = [ensure(pid, division) for pid in match.side_a]
                side_b = [ensure(pid, division) for pid in match.side_b]
                for pid in match.players:
                    entered[(pid, division)] = True

                score_a = _score_for_side_a(match, cfg.margin_weight)
                results_a, results_b = match_results(side_a, side_b, score_a)

                for pid, result in zip(match.side_a, results_a):
                    buckets.setdefault((pid, division), []).append(result)
                for pid, result in zip(match.side_b, results_b):
                    buckets.setdefault((pid, division), []).append(result)

                _record_match(stats, match, division)
                if match.is_doubles:
                    _record_partners(
                        partners, match, season.id, side_a, side_b, score_a)

            # Update everyone who played; age everyone who has entered but sat
            # this period out.
            for key, current in list(ratings.items()):
                played = buckets.get(key)
                if played:
                    ratings[key] = rate(
                        current, played,
                        tau=cfg.tau, min_rd=cfg.min_rd, max_rd=cfg.max_rd,
                    )
                elif entered.get(key):
                    ratings[key] = decay(current, max_rd=cfg.max_rd)

            for key, rating in ratings.items():
                if not entered.get(key):
                    continue
                entry = stats[key]
                entry.history.append(
                    RatingPoint(period_end, rating.rating, rating.rd, entry.played))
                entry.peak_rating = max(entry.peak_rating, rating.rating)

            for division in touched_divisions:
                order_snapshots.setdefault(division, []).append(
                    _order_ids(ratings, stats, cfg, division))

        # ---- finalise every division for this season ----------------------
        ladders: Dict[Tuple[int, str], Ladder] = {}
        by_division: Dict[str, List[LadderEntry]] = {}
        for (player_id, division), rating in ratings.items():
            entry = stats[(player_id, division)]
            entry.rating = rating
            entry.ladder_points = conservative(rating, cfg.conservative_k)
            # Provisional until they've played enough matches -- unless we
            # already know their rating well, which happens when it carried
            # over from last season. Note this is `and`, not `or`: making low
            # RD a second hurdle would defeat the match count entirely, because
            # in a club's first season everyone's opponents are unknown too and
            # RD sits around 227 after three matches, not the ~183 you get
            # against established players.
            entry.provisional = (
                entry.played < cfg.provisional_matches
                and rating.rd > cfg.provisional_rd
            )
            by_division.setdefault(division, []).append(entry)

        for division, entries in by_division.items():
            ordered = sorted(
                entries,
                key=lambda e: (-e.ladder_points, -e.played, e.player.name.lower()),
            )
            snapshots = order_snapshots.get(division, [])
            previous = snapshots[-2] if len(snapshots) >= 2 else []
            prior_rank = {pid: i + 1 for i, pid in enumerate(previous)}
            active_rank = {
                e.player.id: i + 1
                for i, e in enumerate(e for e in ordered if e.player.active)
            }
            for position, entry in enumerate(ordered, start=1):
                entry.rank = position
                was = prior_rank.get(entry.player.id)
                now = active_rank.get(entry.player.id)
                entry.rank_change = (was - now) if (was and now) else 0

            division_matches = [m for m in matches if m.division == division]
            ladders[(season.id, division)] = Ladder(
                division=division, season_id=season.id, entries=ordered,
                by_id={e.player.id: e for e in ordered},
                periods=len(snapshots),
                last_updated=division_matches[-1].played_on if division_matches else None,
            )

        return {"ladders": ladders, "partners": partners, "final": dict(ratings)}

    # ------------------------------------------------------------- utilities
    def head_to_head(
        self, a_id: int, b_id: int, division: Optional[str] = None
    ) -> Tuple[int, int, List[Match]]:
        """(a's wins, b's wins, matches newest-first) in matches featuring both.

        Counts doubles too, where "against" means on opposite sides -- matches
        where they partnered each other are not head-to-head and are excluded.
        """
        meetings = [
            m for m in self.db.list_matches(status="confirmed", player_id=a_id,
                                            division=division)
            if b_id in m.opponents_of(a_id)
        ]
        a_wins = sum(1 for m in meetings if m.won(a_id))
        return a_wins, len(meetings) - a_wins, meetings

    def expected_result(
        self, side_a: Sequence[int], side_b: Sequence[int], division: str,
        season_id: Optional[int] = None,
    ) -> Optional[float]:
        """Side A's win probability, using current ratings in this division."""
        ladder = self.ladder(division, season_id, include_inactive=True)
        try:
            ratings_a = [ladder.entry(pid).rating for pid in side_a]     # type: ignore
            ratings_b = [ladder.entry(pid).rating for pid in side_b]     # type: ignore
        except AttributeError:
            return None
        if not ratings_a or len(ratings_a) != len(ratings_b):
            return None
        return team_win_probability(ratings_a, ratings_b)

    def challengeable(
        self, player_id: int, division: str, season_id: Optional[int] = None
    ) -> List[LadderEntry]:
        """Who this player may challenge in a division, under the club's rule."""
        ladder = self.ladder(division, season_id)
        entry = ladder.entry(player_id)
        if not entry:
            return []
        highest = entry.rank - self.config.challenge_up_positions
        return [e for e in ladder.entries
                if e.player.id != player_id and highest <= e.rank < entry.rank]

    def rematch_warning(self, side_a: Sequence[int], side_b: Sequence[int],
                        on: str) -> Optional[str]:
        cooldown = self.config.rematch_cooldown_days
        if cooldown <= 0:               # 0 disables the warning entirely
            return None
        last = self.db.last_meeting(side_a[0], side_b[0])
        if not last:
            return None
        try:
            gap = (datetime.strptime(on, "%Y-%m-%d").date()
                   - datetime.strptime(last.played_on, "%Y-%m-%d").date()).days
        except ValueError:
            return None
        if 0 <= gap < cooldown:
            return (f"These players already met on {last.played_on} "
                    f"({gap} days ago). The club's cooldown is {cooldown} days.")
        return None


def _score_for_side_a(match: Match, margin_weight: float) -> float:
    """Side A's score in [0,1], blending outcome with share of games won."""
    outcome = 1.0 if match.winner_side == "a" else 0.0
    if match.retired or match.walkover:
        return outcome
    total = match.games_a + match.games_b
    if total <= 0:
        return outcome
    weight = max(0.0, min(1.0, margin_weight))
    return (1.0 - weight) * outcome + weight * (match.games_a / total)


def _record_match(stats: Dict, match: Match, division: str) -> None:
    for pid in match.players:
        entry = stats[(pid, division)]
        won, lost = match.games_for(pid)
        entry.played += 1
        is_win = match.won(pid)
        entry.won += int(is_win)
        entry.lost += int(not is_win)
        entry.games_won += won
        entry.games_lost += lost
        entry.form.insert(0, "W" if is_win else "L")
        del entry.form[5:]
        entry.last_played = match.played_on


def _record_partners(
    partners: Dict, match: Match, season_id: int,
    side_a: Sequence[Rating], side_b: Sequence[Rating], score_a: float,
) -> None:
    """Accumulate how each pair did against what the ratings predicted."""
    expected_a = team_win_probability(side_a, side_b)
    for side, expected, score in (
        (match.side_a, expected_a, score_a),
        (match.side_b, 1.0 - expected_a, 1.0 - score_a),
    ):
        for pid in side:
            partner = match.partner_of(pid)
            if partner is None:
                continue
            key = (season_id, pid, partner)
            stat = partners.get(key)
            if stat is None:
                stat = PartnerStat(partner_id=partner, division=match.division)
                partners[key] = stat
            stat.played += 1
            stat.won += int(match.won(pid))
            stat.actual += score
            stat.expected += expected


def _order_ids(
    ratings: Dict, stats: Dict, cfg: Config, division: str
) -> List[int]:
    scored = [
        (pid, conservative(r, cfg.conservative_k), stats[(pid, d)].played)
        for (pid, d), r in ratings.items()
        if d == division and stats[(pid, d)].player.active
    ]
    scored.sort(key=lambda t: (-t[1], -t[2], stats[(t[0], division)].player.name.lower()))
    return [pid for pid, _, _ in scored]


def _split_into_periods(
    matches: Sequence[Match], period_days: int
) -> List[Tuple[str, List[Match]]]:
    """Cut the match list into consecutive rating periods.

    Empty periods between bursts of activity are kept, so a quiet month still
    widens everyone's RD the way Glicko-2 intends.
    """
    if not matches:
        return []
    period_days = max(1, period_days)
    start = _parse_date(matches[0].played_on)
    end = _parse_date(matches[-1].played_on)

    buckets: List[Tuple[str, List[Match]]] = []
    cursor = start
    index = 0
    while cursor <= end:
        boundary = cursor + timedelta(days=period_days)
        current: List[Match] = []
        while index < len(matches) and _parse_date(matches[index].played_on) < boundary:
            current.append(matches[index])
            index += 1
        buckets.append(((boundary - timedelta(days=1)).isoformat(), current))
        cursor = boundary
    return buckets


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()
