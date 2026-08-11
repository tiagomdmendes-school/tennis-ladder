"""The web app: a small router on top of Python's stdlib HTTP server.

No framework, so there is nothing to install. Sessions live in SQLite so a
restart doesn't sign the whole club out.
"""

from __future__ import annotations

import email.parser
import email.policy
import json
import re
import secrets
import urllib.parse
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple

from . import availability as av
from . import divisions as div
from . import tournaments as T
from .config import CONFIG, Config, apply_timezone, current_zone_name
from .mailer import KIND_HINTS, KIND_LABELS, Mailer, verify_unsubscribe
from .scheduling import SchedulingError
from .service import LadderService, ServiceError
from .storage import CONFIRMED, PENDING, Database
from .views import (
    availability_grid, bracket_view, category_options, division_nav,
    division_options, esc, ladder_table, match_rows, page, partner_table,
    player_options, rating_chart, request_rows, score_grid, season_picker,
    standings_table, suggestion_list, today_iso, upcoming_days,
)

COOKIE_NAME = "ladder_session"


class Request:
    """One HTTP request, plus its session.

    `handler` is duck-typed rather than a fixed class: it is a
    BaseHTTPRequestHandler under the standalone server and a small adapter
    under WSGI (see wsgi.py). All this needs from it is `.path`, `.headers`,
    `.rfile` and a writable `.set_cookie`.
    """

    def __init__(self, handler, method: str, db: Database):
        self.handler = handler
        self.method = method
        self.db = db
        parsed = urllib.parse.urlparse(handler.path)
        self.path = parsed.path.rstrip("/") or "/"
        self.query = urllib.parse.parse_qs(parsed.query)
        self.form: Dict[str, str] = {}
        self.form_multi: Dict[str, List[str]] = {}
        self.files: Dict[str, str] = {}
        if method == "POST":
            self._read_body()
        self.session = self._session()

    # ------------------------------------------------------------- plumbing
    def _read_body(self) -> None:
        length = int(self.handler.headers.get("Content-Length") or 0)
        if length <= 0 or length > 8 * 1024 * 1024:
            return
        raw = self.handler.rfile.read(length)
        ctype = self.handler.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            self._read_multipart(raw, ctype)
        else:
            for key, values in urllib.parse.parse_qs(
                raw.decode("utf-8", "replace"), keep_blank_values=True
            ).items():
                self.form[key] = values[0]
                self.form_multi[key] = values

    def _read_multipart(self, raw: bytes, ctype: str) -> None:
        header = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(header + raw)
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            text = payload.decode("utf-8", "replace")
            if part.get_filename():
                if text.strip():
                    self.files[name] = text
            else:
                self.form[name] = text
                self.form_multi.setdefault(name, []).append(text)

    def _session(self) -> dict:
        cookie = SimpleCookie(self.handler.headers.get("Cookie", ""))
        token = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else None
        if token:
            stored = self.db.load_session(token)
            if stored:
                stored["flashes"] = _FLASHES.pop(token, [])
                return stored
        token = secrets.token_urlsafe(24)
        session = {"token": token, "player_id": None, "is_admin": False,
                   "flashes": [], "csrf": secrets.token_urlsafe(16)}
        self.db.save_session(token, None, False, session["csrf"])
        self.handler.set_cookie = token
        return session

    def persist(self) -> None:
        self.db.save_session(
            self.session["token"], self.session["player_id"],
            self.session["is_admin"], self.session["csrf"],
        )
        if self.session["flashes"]:
            _FLASHES[self.session["token"]] = self.session["flashes"]
        else:
            _FLASHES.pop(self.session["token"], None)

    # ------------------------------------------------------------- shortcuts
    def get(self, key: str, default: str = "") -> str:
        if key in self.form:
            return (self.form.get(key) or "").strip()
        return (self.query.get(key, [default])[0] or "").strip()

    def get_int(self, key: str) -> Optional[int]:
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return None

    def flash(self, kind: str, text: str) -> None:
        self.session["flashes"].append((kind, text))

    def take_flashes(self) -> List[tuple]:
        flashes = list(self.session["flashes"])
        self.session["flashes"] = []
        return flashes

    @property
    def csrf(self) -> str:
        return self.session["csrf"]

    def check_csrf(self) -> bool:
        return secrets.compare_digest(self.get("csrf"), self.csrf)


# Flash messages are the one piece of session state that is deliberately
# in-memory: they are single-use, tiny, and losing them on restart is harmless.
_FLASHES: Dict[str, List[tuple]] = {}


class Response:
    def __init__(self, body: str = "", status: int = 200,
                 content_type: str = "text/html; charset=utf-8",
                 headers: Optional[List[Tuple[str, str]]] = None):
        self.body = body
        self.status = status
        self.content_type = content_type
        self.headers = headers or []


def redirect(location: str) -> Response:
    return Response("", status=303, headers=[("Location", location)])


def _score_from_grid(req: "Request") -> str:
    """Turn the set-by-set boxes back into a scoreline like '6-4 7-6(5)'.

    The grid is a friendlier way to type a score, not a second definition of
    what a score means: it assembles the same text the parser has always taken,
    so scoring.py stays the only place that decides what is valid.
    """
    pieces = []
    for index in range(1, 6):
        a, b = req.get(f"s{index}a"), req.get(f"s{index}b")
        if not a or not b:
            continue                      # a blank row is a set that wasn't played
        piece = f"{a}-{b}"
        tb_a, tb_b = req.get(f"t{index}a"), req.get(f"t{index}b")
        if tb_a and tb_b:
            try:
                # Standard notation records the tie-break loser's points, which
                # is the same number whichever side won the set.
                piece += f"({min(int(tb_a), int(tb_b))})"
            except ValueError:
                pass                      # let the parser report a bad number
        pieces.append(piece)

    text = " ".join(pieces)
    ending = req.get("ending")
    if ending == "retired":
        text = (text + " ret.").strip()
    elif ending == "walkover":
        text = (text + " w/o").strip()
    return text


class App:
    def __init__(self, db: Database, config: Config, mailer: Optional[Mailer] = None):
        self.db = db
        self.config = config
        self.mailer = mailer if mailer is not None else Mailer(config, db)
        self.service = LadderService(db, config, notifier=self.mailer.send)
        self.routes: List[Tuple[str, re.Pattern, Callable]] = []
        self._register()

    # ------------------------------------------------------------- utilities
    def route(self, method: str, pattern: str):
        def decorator(fn):
            self.routes.append((method, re.compile(f"^{pattern}$"), fn))
            return fn
        return decorator

    def dispatch(self, req: Request) -> Response:
        for method, pattern, fn in self.routes:
            if method != req.method:
                continue
            match = pattern.match(req.path)
            if match:
                try:
                    return fn(req, *match.groups())
                except ServiceError as exc:
                    # Show the problem and send them back to the form they came
                    # from. Only same-site paths, so a forged Referer can't
                    # bounce anyone off to another host.
                    req.flash("err", esc(str(exc)))
                    referer = urllib.parse.urlparse(
                        req.handler.headers.get("Referer", ""))
                    back = referer.path if referer.path.startswith("/") else "/"
                    return redirect(back or "/")
        return Response(self.render(req, "Not found", "<h1>Not found</h1>"
                                    "<p class='sub'>That page doesn't exist. "
                                    "<a href='/'>Back to the ladders</a>.</p>"),
                        status=404)

    def render(self, req: Request, title: str, body: str, active: str = "") -> str:
        player = (self.db.get_player(req.session["player_id"])
                  if req.session["player_id"] else None)
        return page(title, body, club=self.config.club_name, active=active,
                    player=player, is_admin=req.session["is_admin"],
                    flashes=req.take_flashes(), badges=self.badges(player))

    def badges(self, player) -> dict:
        """Counts for the nav, so things needing you are visible from any page.

        Only counts things the viewer can actually act on -- a badge that
        doesn't clear when you deal with it trains people to ignore badges.
        """
        if not player:
            return {}
        counts = {}
        try:
            waiting = len(self.service.pending_for(player.id))
            if waiting:
                counts["pending"] = waiting

            asked = len(self.service.scheduler.inbox(player.id))
            if asked:
                counts["schedule"] = asked

            # Tournament matches that are yours to play right now.
            ready = 0
            for tournament in self.db.list_tournaments():
                if tournament.status != "running":
                    continue
                for tmatch in self.db.tournament_matches(tournament.id):
                    if (tmatch.winner_id is None and tmatch.is_ready
                            and player.id in tmatch.players):
                        ready += 1
            if ready:
                counts["tournaments"] = ready
        except Exception:                       # noqa: BLE001
            # A broken badge must never take a page down with it.
            return {}
        return counts

    def viewer(self, req: Request):
        pid = req.session["player_id"]
        return self.db.get_player(pid) if pid else None

    def require_admin(self, req: Request) -> bool:
        return bool(req.session.get("is_admin"))

    def clock_zone(self) -> str:
        """What timezone the times on screen are in, for saying so out loud."""
        return current_zone_name()

    def enabled(self) -> List[str]:
        return [d for d in div.DIVISION_ORDER if d in self.config.enabled_divisions]

    def current_division(self, req: Request) -> str:
        wanted = req.get("division")
        if wanted and wanted in self.enabled():
            return wanted
        return self.enabled()[0] if self.enabled() else div.MENS_SINGLES

    def current_season_id(self, req: Request) -> int:
        wanted = req.get_int("season")
        if wanted and self.db.get_season(wanted):
            return wanted
        return self.db.current_season().id

    # --------------------------------------------------------------- routes
    def _register(self) -> None:
        app = self

        # ---------------------------------------------------------- ladders
        @self.route("GET", "/")
        def ladder_page(req: Request) -> Response:
            division = app.current_division(req)
            season_id = app.current_season_id(req)
            ladder = app.service.engine.ladder(division, season_id)
            viewer = app.viewer(req)
            cfg = app.config
            seasons = app.db.seasons()
            season = app.db.get_season(season_id)

            pending_count = len(app.service.pending_for(viewer.id if viewer else None))
            banner = ""
            if pending_count:
                banner = (f'<div class="flash warn">{pending_count} result'
                          f'{"s" if pending_count != 1 else ""} waiting on you. '
                          f'<a href="/pending">Review them</a>.</div>')

            you = ""
            entry = ladder.entry(viewer.id) if viewer else None
            if entry and entry.played:
                targets = app.service.engine.challengeable(viewer.id, division, season_id)
                target_html = ", ".join(
                    f'<a href="/player/{t.player.id}">{esc(t.player.name)}</a> (#{t.rank})'
                    for t in targets
                ) or "nobody above you &mdash; you're at the top"
                you = f"""<div class="card"><div class="stats">
  <div class="stat"><div class="k">Your rank</div><div class="v">#{entry.rank}</div>
    <div class="d">of {len(ladder.entries)}</div></div>
  <div class="stat"><div class="k">Rating</div>
    <div class="v">{round(entry.rating.rating)}</div>
    <div class="d">&plusmn;{round(entry.rating.rd)} &middot; {entry.confidence} confidence</div></div>
  <div class="stat"><div class="k">Record</div><div class="v">{entry.record}</div>
    <div class="d">{entry.win_pct:.0f}% won</div></div>
</div>
<p class="sub" style="margin:16px 0 0">You may challenge: {target_html}.</p></div>"""

            archive = ""
            if season and not season.is_current:
                archive = ('<div class="flash warn">You\'re looking at '
                           f'{esc(season.name)}, which has finished. '
                           '<a href="/">Back to the current season</a>.</div>')

            body = f"""{banner}{archive}
<h1>{esc(div.get(division).label)}</h1>
{division_nav(division, app.enabled(), season_id=season_id if season and not season.is_current else None)}
{season_picker(seasons, season_id, division)}
<p class="sub">Sorted by <b>points</b> = rating &minus; {cfg.conservative_k:g}&times;
uncertainty, so a rating has to be proven before it counts fully.
{ladder.periods} rating period{'s' if ladder.periods != 1 else ''} played.</p>
{you}
{ladder_table(ladder, viewer_id=viewer.id if viewer else None, cfg=cfg)}
<p class="row"><a class="btn small" href="/submit?division={division}">Submit a result</a>
<a class="btn small ghost" href="/export/ladder.csv?division={division}&season={season_id}">Download CSV</a>
<a class="btn small ghost" href="/api/ladder.json?division={division}&season={season_id}">JSON</a></p>"""
            return Response(app.render(req, div.get(division).label, body, "ladder"))

        # ---------------------------------------------------------- submit
        @self.route("GET", "/submit")
        def submit_form(req: Request) -> Response:
            return Response(app.render(req, "Submit result",
                                       app._submit_body(req), "submit"))

        @self.route("POST", "/submit")
        def submit_post(req: Request) -> Response:
            if not req.check_csrf():
                req.flash("err", "Your session expired. Please try again.")
                return redirect("/submit")
            viewer = app.viewer(req)
            division = req.get("division")
            side_a = [req.get_int("a1")]
            side_b = [req.get_int("b1")]
            if division in div.DIVISIONS and div.get(division).is_doubles:
                side_a.append(req.get_int("a2"))
                side_b.append(req.get_int("b2"))
            if any(p is None for p in side_a + side_b):
                req.flash("err", "Pick every player for this division.")
                return redirect(f"/submit?division={division}")

            result = app.service.submit_result(
                division=division,
                side_a=side_a, side_b=side_b,
                score_text=req.get("score") or _score_from_grid(req),
                played_on=req.get("played_on"),
                submitted_by=viewer.id if viewer else None,
                winner_side=req.get("winner_side") or None,
                note=req.get("note"),
                auto_confirm=app.require_admin(req) and req.get("auto_confirm") == "1",
                allow_category_override=(app.require_admin(req)
                                         and req.get("override") == "1"),
            )
            if result.warning:
                req.flash("warn", esc(result.warning))
            if result.status == CONFIRMED:
                req.flash("ok", "Result recorded. The ladder has been updated.")
            else:
                req.flash("ok", "Result submitted. It counts once someone from "
                                "the other side confirms it.")
            return redirect(f"/?division={division}")

        # --------------------------------------------------------- pending
        @self.route("GET", "/pending")
        def pending_page(req: Request) -> Response:
            viewer = app.viewer(req)
            names = {p.id: p.name for p in app.db.list_players()}
            mine = app.service.pending_for(viewer.id) if viewer else []
            yours_out = app.service.submitted_by_side_of(viewer.id) if viewer else []
            everything = app.db.list_matches(status=PENDING)

            intro = ""
            if not viewer and not app.require_admin(req):
                intro = ('<div class="flash warn">'
                         '<a href="/login">Sign in</a> to confirm results.</div>')

            yours = ""
            if viewer:
                yours = ('<h2>Waiting on you</h2><div class="card">'
                         + match_rows(mine, names, actions="confirm", csrf=req.csrf)
                         + "</div>")
                if yours_out:
                    yours += (
                        '<h2>Waiting on your opponents</h2><div class="card">'
                        '<p class="sub" style="margin:0 0 10px">Your side submitted '
                        'these. They start counting once someone from the other '
                        'team confirms.</p>'
                        + match_rows(yours_out, names) + "</div>")

            seen = {m.id for m in mine} | {m.id for m in yours_out}
            others = [m for m in everything if m.id not in seen]
            body = f"""{intro}<h1>Confirm results</h1>
<p class="sub">A result only affects ratings once someone from the other side
confirms it. One opponent is enough &mdash; your own partner can't sign off a
result your side entered.</p>
{yours}
<h2>{'Other unconfirmed results' if viewer else 'Unconfirmed results'}</h2>
<div class="card">{match_rows(others, names,
    actions='confirm' if app.require_admin(req) else '', csrf=req.csrf)}</div>"""
            return Response(app.render(req, "Confirm", body, "pending"))

        @self.route("POST", "/pending")
        def pending_post(req: Request) -> Response:
            if not req.check_csrf():
                req.flash("err", "Your session expired. Please try again.")
                return redirect("/pending")
            match_id = req.get_int("match_id")
            action = req.get("action")
            viewer = app.viewer(req)
            actor = viewer.id if viewer else None
            if match_id is None:
                return redirect("/pending")
            if action == "confirm":
                app.service.confirm(match_id, actor, is_admin=app.require_admin(req))
                req.flash("ok", "Confirmed. Ratings updated.")
            elif action == "reject":
                app.service.reject(match_id, actor, is_admin=app.require_admin(req))
                req.flash("ok", "Marked as disputed. It won't count. "
                                "Sort it out and submit the correct score.")
            return redirect("/pending")

        # --------------------------------------------------------- matches
        @self.route("GET", "/matches")
        def matches_page(req: Request) -> Response:
            names = {p.id: p.name for p in app.db.list_players()}
            wanted = req.get("division")
            division = wanted if wanted in app.enabled() else None
            season_id = app.current_season_id(req)
            matches = app.db.list_matches(status=CONFIRMED, division=division,
                                          season_id=season_id, limit=250)
            filters = ['<a href="/matches" class="' + ("active" if not division else "")
                       + '">All</a>']
            for key in app.enabled():
                cls = "active" if key == division else ""
                filters.append(f'<a href="/matches?division={key}" class="{cls}">'
                               f'{esc(div.get(key).label)}</a>')
            body = f"""<h1>Match history</h1>
<p class="sub">Every confirmed result, newest first. Ratings are recomputed from
this list in full each time a ladder is drawn.</p>
<div class="divnav">{''.join(filters)}</div>
<div class="card">{match_rows(matches, names)}</div>
<p><a class="btn small ghost" href="/export/matches.csv">Download CSV</a></p>"""
            return Response(app.render(req, "Matches", body, "matches"))

        # ---------------------------------------------------------- player
        @self.route("GET", r"/player/(\d+)")
        def player_page(req: Request, player_id: str) -> Response:
            pid = int(player_id)
            player = app.db.get_player(pid)
            if not player:
                return Response(app.render(req, "Unknown player",
                                           "<h1>No such player</h1>"), status=404)
            season_id = app.current_season_id(req)
            engine = app.service.engine
            names = {p.id: p.name for p in app.db.list_players()}
            played_in = engine.divisions_played_by(pid, season_id)

            if not played_in:
                body = (f"<h1>{esc(player.name)}</h1>"
                        "<p class='sub'>No confirmed matches this season yet. "
                        "Their rating starts at "
                        f"{round(app.config.initial_rating)} with maximum "
                        "uncertainty.</p>")
                return Response(app.render(req, player.name, body))

            cards = []
            charts = []
            for key in played_in:
                entry = engine.ladder(key, season_id, include_inactive=True).entry(pid)
                if not entry:
                    continue
                prov = (' <span class="pill prov">provisional</span>'
                        if entry.provisional else "")
                seed = (f'<div class="d">seeded from {esc(entry.seeded_from)}</div>'
                        if entry.seeded_from not in ("new", "") else "")
                cards.append(f"""<div class="card">
<h2 style="margin:0 0 12px"><a href="/?division={key}">{esc(div.get(key).label)}</a>
  <span class="pill">#{entry.rank}</span>{prov}</h2>
<div class="stats">
  <div class="stat"><div class="k">Rating</div>
    <div class="v">{round(entry.rating.rating)}</div>
    <div class="d">&plusmn;{round(entry.rating.rd)} ({entry.confidence})</div>{seed}</div>
  <div class="stat"><div class="k">Points</div>
    <div class="v">{round(entry.ladder_points)}</div>
    <div class="d">rating &minus;{app.config.conservative_k:g}&times;RD</div></div>
  <div class="stat"><div class="k">Record</div><div class="v">{entry.record}</div>
    <div class="d">{entry.win_pct:.0f}% won</div></div>
  <div class="stat"><div class="k">Games</div>
    <div class="v">{entry.games_won}&ndash;{entry.games_lost}</div>
    <div class="d">won&ndash;lost</div></div>
  <div class="stat"><div class="k">Peak</div>
    <div class="v">{round(entry.peak_rating)}</div><div class="d">best</div></div>
</div></div>""")
                charts.append(f'<div class="card">'
                              f'{rating_chart(entry.history, name=player.name, what=div.get(key).label)}'
                              f'</div>')

            viewer = app.viewer(req)
            h2h = ""
            if viewer and viewer.id != pid:
                wins, losses, meetings = engine.head_to_head(viewer.id, pid)
                h2h = f"""<div class="card"><h2 style="margin-top:0">You vs
{esc(player.name)}</h2><p class="sub" style="margin:0 0 12px">
Head to head across all divisions: <b>{wins}&ndash;{losses}</b> in
{len(meetings)} match{'es' if len(meetings) != 1 else ''} on opposite sides.
</p><a class="btn small" href="/find/{pid}?division={esc(played_in[0])}">
Find a time to play</a></div>"""

            partners = engine.partners_of(pid, season_id)
            matches = app.db.list_matches(status=CONFIRMED, player_id=pid,
                                          season_id=season_id, limit=60)
            body = f"""<h1>{esc(player.name)}</h1>
<p class="sub">{esc(div.CATEGORY_LABELS.get(player.category, ''))}
{'&middot; inactive' if not player.active else ''}</p>
{''.join(cards)}
{h2h}
<h2>Partners</h2>
<div class="card">{partner_table(partners, names)}</div>
{''.join(charts)}
<h2>Matches</h2>
<div class="card">{match_rows(matches, names)}</div>"""
            return Response(app.render(req, player.name, body))

        # ------------------------------------------------------ me/settings
        @self.route("GET", "/me")
        def me_page(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer:
                req.flash("warn", "Sign in to change your settings.")
                return redirect("/login")

            if app.config.email_enabled:
                toggles = "".join(f"""<div><label>
<input type="checkbox" name="notify_{kind}" value="1"
  {'checked' if getattr(viewer, f'notify_{kind}') else ''}>
{esc(label)}</label><div class="hint">{esc(KIND_HINTS[kind])}</div></div>"""
                                  for kind, label in KIND_LABELS.items())
                email_note = ("" if viewer.email else
                              '<div class="flash warn">Add your email address '
                              'below or nothing can be sent.</div>')
            else:
                toggles = ('<p class="empty">Email isn\'t set up for this club '
                           'yet, so no notifications can be sent. Your admin can '
                           'configure SMTP in the config file.</p>')
                email_note = ""

            body = f"""<h1>Your settings</h1>
<p class="sub">Signed in as <b>{esc(viewer.name)}</b>.</p>
{email_note}
<div class="card"><form class="stack" method="post" action="/me">
<input type="hidden" name="csrf" value="{esc(req.csrf)}">
<div><label for="email">Email address</label>
<input id="email" name="email" type="email" value="{esc(viewer.email)}"
  placeholder="you@school.edu"></div>
<h2 style="margin:6px 0 0">Email me when&hellip;</h2>
{toggles}
<div class="row"><button type="submit">Save settings</button></div>
</form></div>
<div class="card"><form class="stack" method="post" action="/me/pin">
<input type="hidden" name="csrf" value="{esc(req.csrf)}">
<div><label for="pin">Change your PIN</label>
<input id="pin" name="pin" inputmode="numeric" maxlength="4" placeholder="4 digits">
<div class="hint">Used to sign in and confirm results.</div></div>
<div class="row"><button class="ghost" type="submit">Update PIN</button></div>
</form></div>"""
            return Response(app.render(req, "Your settings", body))

        @self.route("POST", "/me")
        def me_post(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer or not req.check_csrf():
                req.flash("err", "Sign in again to change your settings.")
                return redirect("/login")
            app.db.set_email(viewer.id, req.get("email"))
            for kind in KIND_LABELS:
                app.db.set_notification(viewer.id, kind,
                                        req.get(f"notify_{kind}") == "1")
            req.flash("ok", "Settings saved.")
            return redirect("/me")

        @self.route("POST", "/me/pin")
        def me_pin(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer or not req.check_csrf():
                req.flash("err", "Sign in again to change your PIN.")
                return redirect("/login")
            try:
                app.db.set_pin(viewer.id, req.get("pin"))
            except ValueError as exc:
                req.flash("err", esc(str(exc)))
                return redirect("/me")
            req.flash("ok", "PIN updated.")
            return redirect("/me")

        @self.route("GET", "/unsubscribe")
        def unsubscribe(req: Request) -> Response:
            player_id = req.get_int("p")
            kind = req.get("k")
            token = req.get("t")
            player = app.db.get_player(player_id) if player_id else None
            if (not player or kind not in KIND_LABELS
                    or not verify_unsubscribe(app.config.secret_key,
                                              player.id, kind, token)):
                return Response(app.render(
                    req, "Unsubscribe",
                    "<h1>That link isn't valid</h1><p class='sub'>Change your "
                    "preferences from <a href='/me'>your settings</a> instead.</p>"),
                    status=400)
            app.db.set_notification(player.id, kind, False)
            return Response(app.render(req, "Unsubscribed", f"""
<h1>Unsubscribed</h1>
<p class="sub">{esc(player.name)} will no longer get
&ldquo;{esc(KIND_LABELS[kind])}&rdquo; emails. Your other notification settings
are unchanged &mdash; adjust them any time in
<a href="/me">your settings</a>.</p>"""))

        # ---------------------------------------------------- availability
        @self.route("GET", "/availability")
        def availability_page(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer:
                req.flash("warn", "Sign in to set when you can play.")
                return redirect("/login")
            cfg = app.config
            mine = app.db.get_availability(viewer.id)
            blocks = av.slot_grid(cfg.day_starts_at, cfg.day_ends_at,
                                  cfg.availability_slot_minutes)
            horizon = [date.today() + timedelta(days=i)
                       for i in range(cfg.exception_horizon_days)]

            body = f"""<h1>When can you play?</h1>
<p class="sub">Set your usual week once. It's used to suggest times you and an
opponent are both free, so matches can happen outside practice. Rough is fine
&mdash; you confirm an actual time with the other player before anything is
booked. All times are {esc(app.clock_zone())}, and it's currently
<b>{datetime.now():%-I:%M%p}</b> there.</p>

<div class="card"><h2 style="margin-top:0">Your usual week</h2>
<p class="sub">Currently: <b>{esc(av.describe_week(mine.weekly))}</b></p>
<form method="post" action="/availability">
<input type="hidden" name="csrf" value="{esc(req.csrf)}">
{availability_grid(mine.weekly, blocks)}
<div class="row" style="margin-top:14px"><button type="submit">Save my week</button></div>
</form></div>

<div class="card"><h2 style="margin-top:0">Next {cfg.exception_horizon_days} days</h2>
<p class="sub">This is your week applied to real dates. Something come up?
Block a day here without touching your usual pattern.</p>
{upcoming_days(mine, horizon, req.csrf)}</div>"""
            return Response(app.render(req, "Availability", body, "availability"))

        @self.route("POST", "/availability")
        def availability_save(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer or not req.check_csrf():
                req.flash("err", "Sign in again to save your availability.")
                return redirect("/login")
            weekly = av.intervals_from_slots(req.form_multi.get("slot", []))
            app.db.set_weekly_availability(viewer.id, weekly)
            req.flash("ok", "Saved. " + (
                f"You're free {esc(av.describe_week(weekly))}."
                if weekly else "You've cleared your whole week."))
            return redirect("/availability")

        @self.route("POST", "/availability/day")
        def availability_day(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer or not req.check_csrf():
                req.flash("err", "Sign in again to change your availability.")
                return redirect("/login")
            on_date = req.get("on_date")
            if req.get("action") == "restore":
                app.db.clear_exceptions(viewer.id, on_date)
                req.flash("ok", f"Restored your usual week on {esc(on_date)}.")
            else:
                # Block the whole day: one tap is the point.
                app.db.add_exception(viewer.id, on_date, 0, 24 * 60,
                                     available=False)
                req.flash("ok", f"Blocked out {esc(on_date)}.")
            return redirect("/availability")

        # ------------------------------------------------------- scheduling
        @self.route("GET", "/schedule")
        def schedule_page(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer:
                req.flash("warn", "Sign in to arrange matches.")
                return redirect("/login")
            names = {p.id: p.name for p in app.db.list_players()}
            sched = app.service.scheduler
            nudge = ""
            if not sched.has_availability(viewer.id):
                nudge = ('<div class="flash warn">You haven\'t set your '
                         'availability, so nobody can be offered a time that '
                         'works for you. <a href="/availability">Set it now</a> '
                         '&mdash; it takes a minute.</div>')
            body = f"""{nudge}<h1>Matches</h1>
<p class="sub">Ask someone for a game at a time you're both free. Pick an
opponent from the ladder and you'll be shown the overlaps.</p>

<h2>Waiting on you</h2>
<div class="card">{request_rows(sched.inbox(viewer.id), names, viewer.id,
                                req.csrf, 'inbox')}</div>

<h2>Agreed</h2>
<div class="card"><p class="sub" style="margin:0 0 10px">Matches you've both
said yes to. Played it? Submit the result straight from here &mdash; who you
played is filled in already.</p>
{request_rows(sched.scheduled(viewer.id), names, viewer.id,
              req.csrf, 'agreed')}</div>

<h2>You asked</h2>
<div class="card">{request_rows(sched.outbox(viewer.id), names, viewer.id,
                                req.csrf, 'outbox')}
<p style="margin:14px 0 0"><a class="btn small" href="/find">Ask someone for a
match</a></p></div>"""
            return Response(app.render(req, "Matches", body, "schedule"))

        @self.route("GET", "/find")
        def find_anyone(req: Request) -> Response:
            """Pick an opponent from a list, rather than hunting for their
            profile page first."""
            viewer = app.viewer(req)
            if not viewer:
                req.flash("warn", "Sign in to arrange a match.")
                return redirect("/login")
            sched = app.service.scheduler
            others = [p for p in app.db.list_players(active_only=True)
                      if p.id != viewer.id]
            if not others:
                body = ("<h1>Ask someone for a match</h1><p class='sub'>"
                        "There's nobody else on the ladder yet.</p>")
                return Response(app.render(req, "Ask for a match", body, "schedule"))

            # Say who can actually be matched up, so nobody picks a name and
            # then finds there's nothing to suggest.
            options = []
            for player in others:
                ready = sched.has_availability(player.id)
                options.append(
                    f'<option value="{player.id}">{esc(player.name)}'
                    f'{"" if ready else "  (no availability set)"}</option>')

            formats = "".join(
                f'<option value="{esc(key)}"'
                f'{" selected" if key == app.config.default_match_format else ""}>'
                f'{esc(spec["label"])} (~{spec["minutes"]} min)</option>'
                for key, spec in app.config.match_formats.items())
            divisions = "".join(
                f'<option value="{key}">{esc(div.get(key).label)}</option>'
                for key in app.enabled())

            body = f"""<h1>Ask someone for a match</h1>
<p class="sub">Pick who you want to play and you'll be shown the times you're
both free.</p>
<div class="card"><form class="stack" method="get" action="/find/go">
<div><label for="opponent">Opponent</label>
<select id="opponent" name="opponent">{''.join(options)}</select></div>
<div><label for="division">Division</label>
<select id="division" name="division">{divisions}</select></div>
<div><label for="format">Format</label>
<select id="format" name="format">{formats}</select></div>
<div class="row"><button type="submit">Find times</button>
<a class="btn ghost" href="/schedule">Back</a></div>
</form></div>"""
            return Response(app.render(req, "Ask for a match", body, "schedule"))

        @self.route("GET", "/find/go")
        def find_go(req: Request) -> Response:
            opponent = req.get_int("opponent")
            if opponent is None:
                return redirect("/find")
            query = urllib.parse.urlencode({
                "division": req.get("division"), "format": req.get("format")})
            return redirect(f"/find/{opponent}?{query}")

        @self.route("GET", r"/find/(\d+)")
        def find_time(req: Request, player_id: str) -> Response:
            viewer = app.viewer(req)
            if not viewer:
                req.flash("warn", "Sign in to arrange a match.")
                return redirect("/login")
            opponent = app.db.get_player(int(player_id))
            if not opponent or opponent.id == viewer.id:
                req.flash("err", "Pick someone else to play.")
                return redirect("/")
            division = app.current_division(req)
            match_format = req.get("format") or app.config.default_match_format
            sched = app.service.scheduler

            # If this pair owe each other a tournament match, the round's
            # play-by date bounds the whole page: no point suggesting a time
            # after the cut-off, and the deadline should be impossible to miss.
            fixture = app.service.pending_tournament_match(viewer.id, opponent.id)
            deadline = None
            tournament_note = ""
            if fixture:
                tournament, _, round_info = fixture
                deadline = round_info.deadline if round_info else None
                match_format = tournament.match_format
                division = tournament.division
                tournament_note = (
                    f'<div class="flash warn"><b>{esc(tournament.name)} &mdash; '
                    f'{esc(round_info.name if round_info else "this round")}.</b> '
                    + (f'This match needs to be played by <b>{esc(deadline)}</b>, '
                       'so only times before then are shown.'
                       if deadline else
                       'Arrange it between yourselves and submit the result as '
                       'normal.')
                    + f' <a href="/tournament/{tournament.id}">See the draw</a>.'
                    "</div>")

            slots = sched.suggest(viewer.id, opponent.id,
                                  match_format=match_format, not_after=deadline)

            missing = []
            if not sched.has_availability(viewer.id):
                missing.append("you haven't")
            if not sched.has_availability(opponent.id):
                missing.append(f"{esc(opponent.name)} hasn't")
            warn = (f'<div class="flash warn">{" and ".join(missing)} set '
                    'availability yet, so there\'s nothing to match up.'
                    ' <a href="/availability">Set yours</a>.</div>'
                    if missing else "")

            formats = "".join(
                f'<option value="{esc(key)}"'
                f'{" selected" if key == match_format else ""}>'
                f'{esc(spec["label"])} (~{spec["minutes"]} min)</option>'
                for key, spec in app.config.match_formats.items())

            no_slots_note = ""
            if deadline and not slots and not missing:
                no_slots_note = (
                    '<div class="flash err">You have no shared free time before '
                    f'<b>{esc(deadline)}</b>. Either add availability, propose a '
                    'time below and sort it out between you, or ask the ladder '
                    'admin to extend the round.</div>')

            body = f"""{warn}{tournament_note}{no_slots_note}
<h1>Play {esc(opponent.name)}</h1>
<p class="sub">Times you're both free for a
<b>{esc(app.service.scheduler.format_label(match_format))}</b>, soonest first{
', ending by ' + esc(deadline) if deadline else ''}.
Picking one sends {esc(opponent.name)} a request &mdash; nothing is booked
until they accept.</p>

<div class="card"><form method="get" action="/find/{opponent.id}" class="row">
  <input type="hidden" name="division" value="{esc(division)}">
  <label for="format" style="margin:0 8px 0 0">Format</label>
  <select id="format" name="format" style="width:auto"
          onchange="this.form.submit()">{formats}</select>
  <noscript><button class="small ghost">Update</button></noscript>
</form></div>

<h2>Suggested times</h2>
<div class="card">{suggestion_list(slots, opponent, division, req.csrf,
                                   match_format)}</div>

<h2>Or propose your own</h2>
<div class="card"><form class="stack" method="post" action="/request">
<input type="hidden" name="csrf" value="{esc(req.csrf)}">
<input type="hidden" name="opponent" value="{opponent.id}">
<input type="hidden" name="division" value="{esc(division)}">
<input type="hidden" name="match_format" value="{esc(match_format)}">
<div><label for="starts_at">Date and time</label>
<input id="starts_at" name="starts_at" type="datetime-local" required></div>
<div><label for="message">Message (optional)</label>
<input id="message" name="message" placeholder="Courts are free after 4"></div>
<div class="row"><button type="submit">Send request</button>
<a class="btn ghost" href="/player/{opponent.id}">Back</a></div>
</form></div>"""
            return Response(app.render(req, f"Play {opponent.name}", body,
                                       "schedule"))

        @self.route("POST", "/request")
        def send_request(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer or not req.check_csrf():
                req.flash("err", "Sign in again to send a request.")
                return redirect("/login")
            opponent_id = req.get_int("opponent")
            if opponent_id is None:
                return redirect("/schedule")
            try:
                request = app.service.scheduler.request_match(
                    division=req.get("division") or app.config.enabled_divisions[0],
                    from_player=viewer.id, to_player=opponent_id,
                    starts_at=req.get("starts_at"),
                    match_format=req.get("match_format"),
                    message=req.get("message"))
            except SchedulingError as exc:
                req.flash("err", esc(str(exc)))
                return redirect(f"/find/{opponent_id}")
            opponent = app.db.get_player(opponent_id)
            when = request.when.strftime("%a %d %b at %H:%M")
            req.flash("ok", f"Asked {esc(opponent.name if opponent else 'them')} "
                            f"for {esc(when)}. It's on once they accept.")
            return redirect("/schedule")

        @self.route("POST", "/request/respond")
        def respond_request(req: Request) -> Response:
            viewer = app.viewer(req)
            if not viewer or not req.check_csrf():
                req.flash("err", "Sign in again.")
                return redirect("/login")
            request_id = req.get_int("request_id")
            action = req.get("action")
            if request_id is None:
                return redirect("/schedule")
            try:
                if action == "cancel":
                    app.service.scheduler.cancel(request_id, viewer.id)
                    req.flash("ok", "Cancelled.")
                else:
                    accepted = action == "accept"
                    app.service.scheduler.respond(request_id, viewer.id, accepted)
                    req.flash("ok", "Match agreed. Turn up and play, then submit "
                                    "the result." if accepted else "Declined.")
            except SchedulingError as exc:
                req.flash("err", esc(str(exc)))
            return redirect("/schedule")

        # ------------------------------------------------------ tournaments
        @self.route("GET", "/tournaments")
        def tournaments_page(req: Request) -> Response:
            season = app.db.current_season()
            events = app.db.list_tournaments(season.id)
            if not events:
                body = ("<h1>Tournaments</h1><p class='sub'>None yet. An admin "
                        "can start one from <a href='/admin'>Admin</a> &mdash; "
                        "seeded off the ladder, or a random draw.</p>")
                return Response(app.render(req, "Tournaments", body, "tournaments"))
            rows = "".join(f"""<tr>
  <td class="name"><a href="/tournament/{t.id}">{esc(t.name)}</a></td>
  <td><span class="pill">{esc(div.get(t.division).short)}</span></td>
  <td>{esc(T.STYLE_LABELS.get(t.style, t.style))}</td>
  <td><span class="pill">{esc(t.status)}</span></td>
</tr>""" for t in events)
            body = f"""<h1>Tournaments</h1>
<p class="sub">{esc(season.name)}. Rounds have a play-by date; you arrange the
actual times between yourselves.</p>
<div class="card scroll"><table><thead><tr><th>Name</th><th></th>
<th>Format</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>"""
            return Response(app.render(req, "Tournaments", body, "tournaments"))

        @self.route("GET", r"/tournament/(\d+)")
        def tournament_page(req: Request, tournament_id: str) -> Response:
            tournament = app.db.get_tournament(int(tournament_id))
            if not tournament:
                return Response(app.render(req, "Unknown tournament",
                                           "<h1>No such tournament</h1>"), status=404)
            names = {p.id: p.name for p in app.db.list_players()}
            rounds = app.db.rounds(tournament.id)
            matches = app.db.tournament_matches(tournament.id)
            viewer = app.viewer(req)

            if tournament.style == T.ELIMINATION:
                main = ('<h2>Draw</h2><div class="card">'
                        + bracket_view(rounds, matches, names,
                                       viewer_id=viewer.id if viewer else None,
                                       division=tournament.division,
                                       match_format=tournament.match_format)
                        + "</div>")
            else:
                main = ('<h2>Standings</h2><div class="card">'
                        + standings_table(app.service.tournament_standings(tournament.id),
                                          names) + "</div>")

            yours = ""
            if viewer:
                mine = [m for m in matches
                        if viewer.id in m.players and m.winner_id is None
                        and m.is_ready]
                if mine:
                    cards = []
                    for m in mine:
                        opponent = [p for p in m.players if p != viewer.id][0]
                        deadline = next((r.deadline for r in rounds
                                         if r.round_no == m.round_no), None)
                        cards.append(
                            f'<p class="sub" style="margin:0 0 8px">You play '
                            f'<b>{esc(names.get(opponent, "?"))}</b>'
                            + (f", by <b>{esc(deadline)}</b>" if deadline else "")
                            + f'. <a href="/find/{opponent}?division='
                            f'{esc(tournament.division)}&format='
                            f'{esc(tournament.match_format)}">Find a time</a></p>')
                    yours = ('<div class="card"><h2 style="margin-top:0">Your next '
                             'match</h2>' + "".join(cards) + "</div>")

            overdue = ""
            if app.require_admin(req):
                late = app.service.overdue_matches(tournament.id)
                if late:
                    items = []
                    for m in late:
                        options = "".join(
                            f'<button class="small ghost" name="winner_id" '
                            f'value="{pid}">{esc(names.get(pid,"?"))} advances</button>'
                            for pid in m.players)
                        items.append(f"""<li style="margin-bottom:8px">
{esc(names.get(m.player_a,'?'))} v {esc(names.get(m.player_b,'?'))}
<form method="post" action="/admin/tournament" class="row"
      style="gap:6px;margin:4px 0 0">
  <input type="hidden" name="csrf" value="{esc(req.csrf)}">
  <input type="hidden" name="action" value="force">
  <input type="hidden" name="tmatch_id" value="{m.id}">
  {options}</form></li>""")
                    overdue = (f'<div class="flash warn"><b>{len(late)} match'
                               f'{"es" if len(late) != 1 else ""} past the round '
                               f'deadline.</b> Nothing happens automatically '
                               f'&mdash; extend the round, or send someone '
                               f'through:<ul>{"".join(items)}</ul></div>')

            # Built outside the f-string: a backslash inside an f-string
            # expression is only legal from Python 3.12 (PEP 701), and this
            # has to run on the 3.10 that ships with Ubuntu 22.04.
            overdue_pill = '<span class="pill">overdue</span>'
            deadlines = "".join(
                f'<tr><td>{esc(r.name)}</td><td>{esc(r.deadline or "&mdash;")}</td>'
                f'<td>{overdue_pill if r.is_overdue else ""}</td></tr>'
                for r in rounds)

            body = f"""{overdue}<h1>{esc(tournament.name)}</h1>
<p class="sub">{esc(div.get(tournament.division).label)} &middot;
{esc(T.STYLE_LABELS.get(tournament.style, tournament.style))} &middot;
seeded {esc(T.SEEDING_LABELS.get(tournament.seeding, tournament.seeding).lower())}
&middot; {esc(app.service.scheduler.format_label(tournament.match_format))}
&middot; <span class="pill">{esc(tournament.status)}</span></p>
{yours}
{main}
<h2>Rounds</h2>
<div class="card scroll"><table><thead><tr><th>Round</th><th>Play by</th>
<th></th></tr></thead><tbody>{deadlines}</tbody></table></div>
<p class="sub">Results go in the normal way &mdash; submit the score from
<a href="/submit">Submit result</a> and the draw updates itself. Tournament
matches count towards your ladder rating like any other match.</p>"""
            return Response(app.render(req, tournament.name, body, "tournaments"))

        @self.route("POST", "/admin/tournament")
        def admin_tournament(req: Request) -> Response:
            if not app.require_admin(req) or not req.check_csrf():
                req.flash("err", "Admin access required.")
                return redirect("/login")
            action = req.get("action")
            if action == "force":
                tmatch_id = req.get_int("tmatch_id")
                winner_id = req.get_int("winner_id")
                if tmatch_id and winner_id:
                    app.service.force_tournament_winner(tmatch_id, winner_id)
                    req.flash("ok", "Sent them through.")
                tmatch = app.db.get_tournament_match(tmatch_id or 0)
                return redirect(f"/tournament/{tmatch.tournament_id}" if tmatch
                                else "/tournaments")
            if action == "create":
                entrants = [int(v) for v in req.form_multi.get("entrant", []) if v]
                tournament = app.service.create_tournament(
                    name=req.get("name"), division=req.get("division"),
                    style=req.get("style"), seeding=req.get("seeding"),
                    match_format=req.get("match_format"),
                    player_ids=entrants,
                    round_days=req.get_int("round_days") or 7)
                req.flash("ok", f"{esc(tournament.name)} created with "
                                f"{len(entrants)} players.")
                return redirect(f"/tournament/{tournament.id}")
            return redirect("/admin")

        # ----------------------------------------------------------- login
        @self.route("GET", "/login")
        def login_form(req: Request) -> Response:
            players = app.db.list_players(active_only=True)
            pin_field = ("""<div><label for="pin">PIN</label>
<input id="pin" name="pin" inputmode="numeric" maxlength="4" placeholder="4 digits">
<div class="hint"><b>First time?</b> Leave this blank &mdash; you'll be asked to
choose a PIN. Forgotten yours? Ask the ladder admin to clear it and you can pick
a new one.</div></div>"""
                         if app.config.require_pin else "")
            body = f"""<h1>Sign in</h1>
<p class="sub">Signing in lets you submit and confirm results as yourself.
Browsing the ladders needs no account.</p>
<div class="card"><form class="stack" method="post" action="/login">
<input type="hidden" name="csrf" value="{esc(req.csrf)}">
<div><label for="player_id">Who are you?</label>
<select id="player_id" name="player_id">{player_options(players)}</select></div>
{pin_field}
<div class="row"><button type="submit">Sign in</button></div>
</form></div>
<div class="card"><form class="stack" method="post" action="/login">
<input type="hidden" name="csrf" value="{esc(req.csrf)}">
<input type="hidden" name="as_admin" value="1">
<div><label for="admin_password">Ladder admin</label>
<input id="admin_password" name="admin_password" type="password"
       placeholder="Admin password">
<div class="hint">Set in the config file.</div></div>
<div class="row"><button class="ghost" type="submit">Sign in as admin</button></div>
</form></div>"""
            return Response(app.render(req, "Sign in", body))

        @self.route("POST", "/login")
        def login_post(req: Request) -> Response:
            if not req.check_csrf():
                req.flash("err", "Your session expired. Please try again.")
                return redirect("/login")
            if req.get("as_admin") == "1":
                if secrets.compare_digest(req.get("admin_password"),
                                          app.config.admin_password):
                    req.session["is_admin"] = True
                    req.flash("ok", "Signed in as admin.")
                    return redirect("/admin")
                req.flash("err", "Wrong admin password.")
                return redirect("/login")

            player_id = req.get_int("player_id")
            if player_id is None:
                req.flash("err", "Pick your name.")
                return redirect("/login")
            # First time in? Send them to choose a PIN rather than rejecting
            # them for not knowing one they were never given.
            if app.config.require_pin and not app.db.has_pin(player_id):
                return redirect(f"/claim?player_id={player_id}")
            player = app.service.authenticate(player_id, req.get("pin"))
            req.session["player_id"] = player.id
            req.flash("ok", f"Signed in as {esc(player.name)}.")
            return redirect("/")

        # ------------------------------------------------- claim an account
        @self.route("GET", "/claim")
        def claim_form(req: Request) -> Response:
            player = app.db.get_player(req.get_int("player_id") or 0)
            if not player:
                req.flash("err", "Pick your name to get started.")
                return redirect("/login")
            if player.pin_set:
                req.flash("ok", f"{esc(player.name)} already has a PIN. "
                                "Sign in with it, or ask the ladder admin to "
                                "reset it if you've forgotten.")
                return redirect("/login")
            body = f"""<h1>Welcome, {esc(player.name)}</h1>
<p class="sub">You don't have a PIN yet. Pick one now &mdash; you'll use it to
sign in and to confirm results. <b>Choose something you'll remember</b>; nobody
else can look it up for you, but the ladder admin can clear it so you can set a
new one.</p>
<div class="card"><form class="stack" method="post" action="/claim">
<input type="hidden" name="csrf" value="{esc(req.csrf)}">
<input type="hidden" name="player_id" value="{player.id}">
<div><label for="pin">Choose a 4-digit PIN</label>
<input id="pin" name="pin" inputmode="numeric" maxlength="4" required
       autocomplete="new-password"></div>
<div><label for="pin2">Type it again</label>
<input id="pin2" name="pin2" inputmode="numeric" maxlength="4" required
       autocomplete="new-password"></div>
<div class="row"><button type="submit">Set my PIN and sign in</button>
<a class="btn ghost" href="/login">That's not me</a></div>
</form></div>"""
            return Response(app.render(req, "Choose a PIN", body))

        @self.route("POST", "/claim")
        def claim_post(req: Request) -> Response:
            if not req.check_csrf():
                req.flash("err", "Your session expired. Please try again.")
                return redirect("/login")
            player_id = req.get_int("player_id")
            player = app.db.get_player(player_id or 0)
            if not player:
                return redirect("/login")
            if player.pin_set:
                req.flash("err", "That account already has a PIN.")
                return redirect("/login")

            pin, again = req.get("pin"), req.get("pin2")
            if pin != again:
                req.flash("err", "Those two PINs don't match. Try again.")
                return redirect(f"/claim?player_id={player.id}")
            try:
                app.db.set_pin(player.id, pin)
            except ValueError as exc:
                req.flash("err", esc(str(exc)))
                return redirect(f"/claim?player_id={player.id}")

            req.session["player_id"] = player.id
            req.flash("ok", f"PIN set. You're signed in as {esc(player.name)}.")
            return redirect("/")

        @self.route("GET", "/logout")
        def logout(req: Request) -> Response:
            app.db.delete_session(req.session["token"])
            req.session["player_id"] = None
            req.session["is_admin"] = False
            req.flash("ok", "Signed out.")
            return redirect("/")

        # ----------------------------------------------------------- admin
        @self.route("GET", "/admin")
        def admin_page(req: Request) -> Response:
            if not app.require_admin(req):
                body = ("<h1>Admin</h1><p class='sub'>This area needs the admin "
                        "password. <a href='/login'>Sign in</a>.</p>")
                return Response(app.render(req, "Admin", body, "admin"), status=403)

            players = app.db.list_players()
            rows = "".join(f"""<tr>
  <td class="name">{esc(p.name)}</td>
  <td><form method="post" action="/admin/player" class="row" style="margin:0;gap:4px">
    <input type="hidden" name="csrf" value="{esc(req.csrf)}">
    <input type="hidden" name="player_id" value="{p.id}">
    <select name="category" style="width:auto" onchange="this.form.submit()">
      {category_options(p.category)}</select>
    <input type="hidden" name="action" value="category">
    <noscript><button class="small ghost">Set</button></noscript>
  </form></td>
  <td>{'<span class="pill">PIN set</span>' if p.pin_set
       else '<span class="pill prov">not signed in yet</span>'}</td>
  <td>{'active' if p.active else '<span class="pill">inactive</span>'}</td>
  <td><div class="row" style="gap:4px">
    <form method="post" action="/admin/player" style="margin:0">
      <input type="hidden" name="csrf" value="{esc(req.csrf)}">
      <input type="hidden" name="player_id" value="{p.id}">
      <button class="small ghost" name="action" value="toggle">
        {'Deactivate' if p.active else 'Reactivate'}</button></form>
    {'''<form method="post" action="/admin/player" style="margin:0">
      <input type="hidden" name="csrf" value="''' + esc(req.csrf) + '''">
      <input type="hidden" name="player_id" value="''' + str(p.id) + '''">
      <button class="small ghost" name="action" value="clearpin">Clear PIN</button>
    </form>''' if p.pin_set else ''}
    <form method="post" action="/admin/player" class="row" style="margin:0;gap:4px"
          onsubmit="return confirm('Permanently delete {esc(p.name)} and every match they played? This cannot be undone.')">
      <input type="hidden" name="csrf" value="{esc(req.csrf)}">
      <input type="hidden" name="player_id" value="{p.id}">
      <input name="confirm" placeholder="DELETE" style="width:88px"
             aria-label="Type DELETE to confirm">
      <button class="small danger" name="action" value="destroy">Delete</button>
    </form>
  </div></td></tr>""" for p in players) or \
                '<tr><td colspan="5" class="empty">No players yet.</td></tr>'

            names = {p.id: p.name for p in players}
            pending = app.db.list_matches(status=PENDING)
            recent = app.db.list_matches(limit=25)
            seasons = app.db.seasons()

            singles = [d for d in app.enabled() if not div.get(d).is_doubles]
            singles_options = "".join(
                f'<option value="{k}">{esc(div.get(k).label)}</option>'
                for k in singles)
            format_options = "".join(
                f'<option value="{esc(k)}"'
                f'{" selected" if k == app.config.default_match_format else ""}>'
                f'{esc(spec["label"])} (~{spec["minutes"]} min)</option>'
                for k, spec in app.config.match_formats.items())
            if app.config.email_enabled:
                opted_in = sum(1 for p in players
                               if p.active and any(p.wants(k) for k in KIND_LABELS))
                email_status = f"""<p class="sub">Sending through
<b>{esc(app.config.smtp_host)}:{app.config.smtp_port}</b> as
<b>{esc(app.config.smtp_from)}</b>. {opted_in} player(s) have opted in to at
least one notification.</p>
<form method="post" action="/admin/email-test" class="row" style="margin:0">
  <input type="hidden" name="csrf" value="{esc(req.csrf)}">
  <input name="to" type="email" placeholder="you@example.com"
         style="width:auto;min-width:220px" required>
  <button class="small">Send a test email</button>
</form>
<p class="hint">The result is reported here, including the mail server's own
error if it fails &mdash; which is the quickest way to tell a wrong password
from a blocked port.</p>"""
            else:
                email_status = """<p class="sub">Not configured, so nothing is
sent and the notification toggles are hidden from players.</p>
<p class="sub">Add <code>smtp_host</code>, <code>smtp_user</code>,
<code>smtp_password</code> and <code>smtp_from</code> to
<code>config.json</code>, then restart. Any provider that accepts SMTP on port
587 works; a free Brevo account gives you credentials without needing a domain.
Set <code>base_url</code> too, or links in the emails point nowhere.</p>"""

            entrant_boxes = "".join(
                f'<label style="font-weight:400;display:block;margin-bottom:4px">'
                f'<input type="checkbox" name="entrant" value="{p.id}">'
                f'{esc(p.name)} '
                f'<span class="hint" style="display:inline">'
                f'{esc(div.CATEGORY_LABELS.get(p.category, ""))}</span></label>'
                for p in players if p.active) or \
                '<p class="empty">Add some players first.</p>'
            current = app.db.current_season()
            current_mark = "<b>current</b>"
            deletable = len(seasons) > 1
            season_rows = "".join(
                f'<tr><td class="name">{esc(s.name)}</td><td>{esc(s.starts_on)}</td>'
                f'<td>{current_mark if s.is_current else ""}</td>'
                f'<td>' + (f"""<form method="post" action="/admin/season" class="row"
     style="margin:0;gap:4px" onsubmit="return confirm('Permanently delete {esc(s.name)} and all its matches?')">
  <input type="hidden" name="csrf" value="{esc(req.csrf)}">
  <input type="hidden" name="season_id" value="{s.id}">
  <input name="confirm" placeholder="DELETE" style="width:88px"
         aria-label="Type DELETE to confirm">
  <button class="small danger" name="action" value="destroy">Delete</button>
</form>""" if deletable else '<span class="hint">only season</span>')
                + "</td></tr>"
                for s in seasons)

            body = f"""<h1>Admin</h1>
<p class="sub">Deactivating a player keeps their history and removes them from
the ladders &mdash; that's what to do when someone graduates. Deleting a match
recalculates every rating that came after it.</p>
<div class="flash warn"><b>Delete</b> buttons erase permanently: a player takes
every match they played with them, which changes their opponents' records too,
and a season takes its matches and tournaments. There's no undo, so they're
there for clearing out test data. For a real player who has left, use
<b>Deactivate</b>.</div>

<div class="grid2">
<div class="card"><h2 style="margin-top:0">Add a player</h2>
<form class="stack" method="post" action="/admin/player">
  <input type="hidden" name="csrf" value="{esc(req.csrf)}">
  <div><label for="name">Name</label><input id="name" name="name" required></div>
  <div><label for="category">Category</label>
    <select id="category" name="category">{category_options()}</select>
    <div class="hint">Decides which ladders they can enter.</div></div>
  <div><label for="email">Email (optional)</label>
    <input id="email" name="email" type="email"></div>
  <div class="hint">No PIN needed &mdash; they choose their own when they first
  sign in.</div>
  <div class="row"><button name="action" value="add">Add player</button></div>
</form></div>

<div class="card"><h2 style="margin-top:0">Seasons</h2>
<div class="scroll"><table><thead><tr><th>Season</th><th>From</th><th></th>
<th></th></tr></thead><tbody>{season_rows}</tbody></table></div>
<form class="stack" method="post" action="/admin/season" style="margin-top:14px"
  onsubmit="return confirm('Start a new season? Ratings carry over with widened uncertainty.')">
  <input type="hidden" name="csrf" value="{esc(req.csrf)}">
  <div><label for="season_name">New season name</label>
    <input id="season_name" name="name" placeholder="Spring 2027" required>
    <div class="hint">Closes {esc(current.name)}. Ratings carry over as seeds;
    nothing is deleted.</div></div>
  <div class="row"><button name="action" value="start">Start new season</button></div>
</form></div>
</div>

<div class="card"><h2 style="margin-top:0">New tournament</h2>
<form class="stack" method="post" action="/admin/tournament" style="max-width:none">
  <input type="hidden" name="csrf" value="{esc(req.csrf)}">
  <input type="hidden" name="action" value="create">
  <div class="grid2">
    <div><label for="t_name">Name</label>
      <input id="t_name" name="name" placeholder="Fall Open" required></div>
    <div><label for="t_division">Division</label>
      <select id="t_division" name="division">{singles_options}</select>
      <div class="hint">Singles only for now.</div></div>
    <div><label for="t_style">Format</label>
      <select id="t_style" name="style">
        <option value="round_robin">Round robin &mdash; everyone plays everyone</option>
        <option value="elimination">Single elimination &mdash; knockout bracket</option>
      </select></div>
    <div><label for="t_seeding">Seeding</label>
      <select id="t_seeding" name="seeding">
        <option value="ladder">By ladder ranking</option>
        <option value="random">Random draw</option>
      </select></div>
    <div><label for="t_format">Match format</label>
      <select id="t_format" name="match_format">{format_options}</select>
      <div class="hint">Sets how long a match is assumed to take when
      suggesting times.</div></div>
    <div><label for="t_days">Days per round</label>
      <input id="t_days" name="round_days" type="number" value="7" min="1"
             max="60"></div>
  </div>
  <div><label>Who's in?</label>
    <div class="scroll" style="max-height:220px;overflow-y:auto;border:1px solid
         var(--border);border-radius:8px;padding:10px">{entrant_boxes}</div>
    <div class="hint">Byes are handed to the top seeds automatically if the
    field isn't a power of two.</div></div>
  <div class="row"><button type="submit">Create tournament</button></div>
</form></div>

<div class="card"><h2 style="margin-top:0">Email</h2>
{email_status}</div>

<div class="card"><h2 style="margin-top:0">Import results (CSV)</h2>
<form class="stack" method="post" action="/admin/import" enctype="multipart/form-data"
      style="max-width:none">
  <input type="hidden" name="csrf" value="{esc(req.csrf)}">
  <div><label for="file">Upload a file</label><input id="file" type="file"
    name="file" accept=".csv,text/csv"></div>
  <div><label for="text">&hellip;or paste rows</label>
  <textarea id="text" name="text" placeholder="date,division,player_a,player_a2,player_b,player_b2,score,note
2026-03-01,mens_singles,Ana Silva,,Ben Okafor,,6-4 3-6 10-8,challenge
2026-03-02,mixed_doubles,Ana Silva,Ben Okafor,Chiara Rossi,Devon Park,6-4 6-2,"></textarea>
  <div class="hint">Score is written from <b>side A</b>'s point of view. Leave
  player_a2/player_b2 blank for singles. Unknown names are added automatically.
  </div></div>
  <div class="row"><button name="action" value="import">Import</button></div>
</form></div>

<h2>Players</h2>
<p class="sub">Players choose their own PIN the first time they sign in, so
there's nothing to hand out. If someone forgets theirs, clear it here and they
pick a new one.</p>
<div class="card scroll"><table><thead><tr><th>Name</th><th>Category</th>
<th>PIN</th><th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>

<h2>Unconfirmed ({len(pending)})</h2>
<div class="card">{match_rows(pending, names, actions='admin', csrf=req.csrf)}</div>

<h2>Recent results</h2>
<div class="card">{match_rows(recent, names, actions='admin', csrf=req.csrf)}</div>"""
            return Response(app.render(req, "Admin", body, "admin"))

        @self.route("POST", "/admin/player")
        def admin_player(req: Request) -> Response:
            if not app.require_admin(req) or not req.check_csrf():
                req.flash("err", "Admin access required.")
                return redirect("/login")
            action = req.get("action")
            if action == "add":
                player = app.service.add_player(
                    req.get("name"), req.get("email"), "", req.get("category")
                    or div.UNSPECIFIED)
                req.flash("ok", f"Added {esc(player.name)}. Nothing to hand "
                                "over &mdash; they pick their own PIN the first "
                                "time they sign in.")
                return redirect("/admin")

            pid = req.get_int("player_id")
            player = app.db.get_player(pid) if pid else None
            if not player:
                return redirect("/admin")
            if action == "toggle":
                app.db.set_player_active(player.id, not player.active)
                app.service.engine.invalidate()
                req.flash("ok", f"{esc(player.name)} is now "
                                f"{'inactive' if player.active else 'active'}.")
            elif action == "category":
                category = req.get("category")
                if category in div.CATEGORIES:
                    app.db.set_player_category(player.id, category)
                    app.service.engine.invalidate()
                    req.flash("ok", f"{esc(player.name)} is now "
                                    f"{esc(div.CATEGORY_LABELS[category])}.")
            elif action == "clearpin":
                app.db.clear_pin(player.id)
                req.flash("ok", f"Cleared {esc(player.name)}'s PIN. They'll "
                                "choose a new one next time they sign in.")
            elif action == "destroy":
                if req.get("confirm").strip().upper() != "DELETE":
                    req.flash("err", "Type DELETE in the box to confirm.")
                    return redirect("/admin")
                name = player.name
                removed = app.service.delete_player_completely(player.id)
                req.flash("ok", f"Deleted {esc(name)} permanently, along with "
                                f"{removed['matches']} match"
                                f"{'es' if removed['matches'] != 1 else ''} and "
                                f"{removed['requests']} request"
                                f"{'s' if removed['requests'] != 1 else ''}.")
            return redirect("/admin")

        @self.route("POST", "/admin/email-test")
        def admin_email_test(req: Request) -> Response:
            if not app.require_admin(req) or not req.check_csrf():
                req.flash("err", "Admin access required.")
                return redirect("/login")
            outcome = app.mailer.send_test(req.get("to"))
            req.flash("ok" if outcome.startswith("Sent") else "err", esc(outcome))
            return redirect("/admin")

        @self.route("POST", "/admin/season")
        def admin_season(req: Request) -> Response:
            if not app.require_admin(req) or not req.check_csrf():
                req.flash("err", "Admin access required.")
                return redirect("/login")
            if req.get("action") == "destroy":
                if req.get("confirm").strip().upper() != "DELETE":
                    req.flash("err", "Type DELETE in the box to confirm.")
                    return redirect("/admin")
                season_id = req.get_int("season_id")
                season = app.db.get_season(season_id or 0)
                if not season:
                    return redirect("/admin")
                removed = app.service.delete_season_completely(season.id)
                req.flash("ok", f"Deleted {esc(season.name)} permanently, with "
                                f"{removed['matches']} match"
                                f"{'es' if removed['matches'] != 1 else ''} and "
                                f"{removed['tournaments']} tournament"
                                f"{'s' if removed['tournaments'] != 1 else ''}.")
                return redirect("/admin")

            season = app.service.start_season(req.get("name"))
            req.flash("ok", f"{esc(season.name)} started. Ratings carried over "
                            "with widened uncertainty; past seasons stay readable.")
            return redirect("/admin")

        @self.route("POST", "/admin/match")
        def admin_match(req: Request) -> Response:
            if not app.require_admin(req) or not req.check_csrf():
                req.flash("err", "Admin access required.")
                return redirect("/login")
            match_id = req.get_int("match_id")
            if match_id is None:
                return redirect("/admin")
            if req.get("action") == "delete":
                app.service.delete_match(match_id)
                req.flash("ok", "Match deleted and ratings recalculated.")
            elif req.get("action") == "confirm":
                app.service.confirm(match_id, None, is_admin=True)
                req.flash("ok", "Confirmed.")
            return redirect("/admin")

        @self.route("POST", "/admin/import")
        def admin_import(req: Request) -> Response:
            if not app.require_admin(req) or not req.check_csrf():
                req.flash("err", "Admin access required.")
                return redirect("/login")
            text = req.files.get("file") or req.get("text")
            if not text.strip():
                req.flash("err", "Nothing to import -- choose a file or paste rows.")
                return redirect("/admin")
            imported, errors = app.service.import_csv(text)
            if imported:
                req.flash("ok", f"Imported {imported} result"
                                f"{'s' if imported != 1 else ''}.")
            if errors:
                shown = "".join(f"<li>{esc(e)}</li>" for e in errors[:12])
                more = (f"<li>&hellip;and {len(errors) - 12} more</li>"
                        if len(errors) > 12 else "")
                req.flash("err", f"Skipped {len(errors)} row"
                                 f"{'s' if len(errors) != 1 else ''}:<ul>{shown}{more}</ul>")
            return redirect("/admin")

        # ------------------------------------------------------ data + docs
        @self.route("GET", "/api/ladder.json")
        def api_ladder(req: Request) -> Response:
            division = app.current_division(req)
            season_id = app.current_season_id(req)
            ladder = app.service.engine.ladder(division, season_id)
            season = app.db.get_season(season_id)
            payload = {
                "club": app.config.club_name,
                "season": season.name if season else None,
                "division": division,
                "division_label": div.get(division).label,
                "updated": ladder.last_updated,
                "rating_system": "glicko2",
                "standings": [
                    {
                        "rank": e.rank, "player": e.player.name,
                        "ladder_points": round(e.ladder_points, 1),
                        "rating": round(e.rating.rating, 1),
                        "rd": round(e.rating.rd, 1),
                        "volatility": round(e.rating.volatility, 5),
                        "played": e.played, "won": e.won, "lost": e.lost,
                        "form": e.form, "provisional": e.provisional,
                        "last_played": e.last_played,
                    }
                    for e in ladder.entries
                ],
            }
            return Response(json.dumps(payload, indent=2),
                            content_type="application/json; charset=utf-8")

        @self.route("GET", "/export/ladder.csv")
        def export_ladder(req: Request) -> Response:
            division = app.current_division(req)
            return Response(
                app.service.export_ladder_csv(division, app.current_season_id(req)),
                content_type="text/csv; charset=utf-8",
                headers=[("Content-Disposition",
                          f'attachment; filename="{division}.csv"')],
            )

        @self.route("GET", "/export/matches.csv")
        def export_matches(req: Request) -> Response:
            return Response(
                app.service.export_matches_csv(),
                content_type="text/csv; charset=utf-8",
                headers=[("Content-Disposition", 'attachment; filename="matches.csv"')],
            )

        @self.route("GET", "/about")
        def about(req: Request) -> Response:
            cfg = app.config
            return Response(app.render(req, "How it works", f"""
<h1>How the rating works</h1>
<p class="sub">The ladders use <b>Glicko-2</b>, the system Mark Glickman designed
as a successor to Elo. It is a better fit than win/loss or plain Elo for a club
where people play a handful of matches a season.</p>

<div class="card">
<h2 style="margin-top:0">Three numbers per player, per division</h2>
<p><b>Rating</b> &mdash; the skill estimate. Everyone starts at
{round(cfg.initial_rating)}.</p>
<p><b>RD (rating deviation)</b> &mdash; how sure we are. A newcomer starts at
&plusmn;{round(cfg.initial_rd)}; it shrinks as they play and creeps back up
while they don't.</p>
<p><b>Volatility</b> &mdash; how erratic their results are. Consistent players
move in small steps; players who spring surprises move faster.</p>
<p class="sub" style="max-width:none">You carry a separate rating in each of the
five divisions, because being strong at singles says surprisingly little about
how you play mixed doubles.</p>
</div>

<div class="card">
<h2 style="margin-top:0">Doubles &mdash; how your partner counts</h2>
<p class="sub" style="max-width:none">Doubles is rated per player, but what you
are measured against is the gap between the two <em>teams</em>. Each player is
updated as though they had played one opponent chosen so that the gap they face
equals the gap their team faced.</p>
<p class="sub" style="max-width:none">Say you're 1600 with a 1200 partner
against two 1500s. Your team averages 1400 against their 1500, so you are scored
as though you personally faced a <b>1700</b> player &mdash; win and you gain a
lot. Your partner is scored against a virtual 1300, so you both share exactly
the same team win expectation, and differ only in how far each of you moves.
Carrying a weak partner past strong opponents pays properly; coasting behind a
strong one does not.</p>
</div>

<div class="card">
<h2 style="margin-top:0">What you can enter as a score</h2>
<p class="sub" style="max-width:none">Club formats vary, so the scoreline box
takes whatever you actually played. Write side A's games first.</p>
<div class="scroll"><table><thead><tr><th>Format</th><th>Type this</th>
</tr></thead><tbody>
<tr><td>A single set &mdash; the usual challenge match</td><td><code>6-4</code></td></tr>
<tr><td>One set decided on a tie-break</td><td><code>7-6</code> or <code>7-6(5)</code></td></tr>
<tr><td>Two sets and a deciding match tie-break</td><td><code>6-4 4-6 10-8</code></td></tr>
<tr><td>Full best of three</td><td><code>6-4 3-6 6-2</code></td></tr>
<tr><td>Eight-game pro set</td><td><code>8-6</code></td></tr>
<tr><td>Fast4</td><td><code>4-2</code></td></tr>
<tr><td>Someone retired</td><td><code>6-3 2-1 ret.</code></td></tr>
<tr><td>Walkover</td><td><code>w/o</code></td></tr>
</tbody></table></div>
<p class="sub" style="max-width:none">A deciding-set match tie-break counts as
one game rather than ten, so a super-tie-break can't outweigh the two real sets
before it. <b>Every match counts the same regardless of format</b> &mdash; a
single set moves your rating as much as a three-setter. That's deliberate: one
set is this club's normal format, and weighting short matches down would slow
everyone's rating from settling, which matters most in a short season.</p>
<p class="sub" style="max-width:none">A score with the sets level and no decider
(<code>6-4 3-6</code>) is refused &mdash; that's an unfinished match, not a
format. Add the deciding set, or mark it <code>ret.</code></p>
</div>

<div class="card">
<h2 style="margin-top:0">Why not just win/loss?</h2>
<p class="sub" style="max-width:none">Because who you beat is the whole story.
Beating the #1 player moves you far more than beating the #20, and losing to a
strong opponent barely costs you. A ladder ordered on win percentage rewards
ducking tough opponents; this one rewards playing them.</p>
</div>

<div class="card">
<h2 style="margin-top:0">The scoreline counts too</h2>
<p class="sub" style="max-width:none">A win is worth
{(1 - cfg.margin_weight) * 100:.0f}% of the credit outright; the remaining
{cfg.margin_weight * 100:.0f}% is split by share of games won. So 6-0 6-0 scores
1.00, 6-4 6-4 scores 0.92 and 7-6 7-6 scores 0.91 &mdash; winning is what
matters, but a thrashing is recorded as a thrashing. Retirements and walkovers
ignore the margin entirely. A deciding-set match tie-break counts as one game,
not ten.</p>
</div>

<div class="card">
<h2 style="margin-top:0">Ladder order</h2>
<p class="sub" style="max-width:none">Position is set by
<b>rating &minus; {cfg.conservative_k:g}&times;RD</b>, not raw rating, so an
unproven player can't top the ladder on one upset. Players are marked
<span class="pill prov">provisional</span> until they have played
{cfg.provisional_matches} matches &mdash; or straight away, if their rating
carried over from last season already well enough known
(RD under {round(cfg.provisional_rd)}).</p>
</div>

<div class="card">
<h2 style="margin-top:0">Seasons and new players</h2>
<p class="sub" style="max-width:none">Ratings carry into a new season with their
uncertainty widened to at least {round(cfg.season_carryover_rd)}, so where you
finished seeds where you start without freezing it. Your first match in a new
division starts from your singles rating held loosely
(&plusmn;{round(cfg.cross_division_rd)}) rather than from scratch, so recruits
settle in a few matches instead of a dozen. Every seed comes from a player's own
results &mdash; nothing here encodes anyone's opinion of how good someone is.</p>
</div>

<div class="card">
<h2 style="margin-top:0">Rating periods</h2>
<p class="sub" style="max-width:none">Glicko-2 updates in periods rather than
match by match, so results inside the same period are weighed against each other
fairly rather than in the order they were entered. These ladders use
{cfg.rating_period_days}-day periods. Ratings are always recalculated from the
complete match history, so correcting an old result fixes everything downstream
of it automatically.</p>
</div>"""))

    # ------------------------------------------------------------- helpers
    def _submit_body(self, req: Request) -> str:
        players = self.db.list_players(active_only=True)
        viewer = self.viewer(req)
        if len(players) < 2:
            return ("<h1>Submit a result</h1><p class='sub'>You need at least two "
                    "players on the ladder first &mdash; add them in "
                    "<a href='/admin'>Admin</a>.</p>")
        division = self.current_division(req)
        is_doubles = div.get(division).is_doubles

        # Everything can arrive pre-filled: the Matches tab links here with the
        # opponent and format already known, so submitting an arranged match is
        # a score and a button.
        a1 = req.get_int("a1") or (viewer.id if viewer else None)
        a2, b1, b2 = req.get_int("a2"), req.get_int("b1"), req.get_int("b2")
        match_format = req.get("format") or self.config.default_match_format
        rows, tiebreak_row = self._score_shape(match_format)

        def side_label(first_id, second_id, fallback):
            """Name the column after whoever is in it.

            Doubles uses first names joined -- "Tiago & Sam" says which column
            is yours in a glance and, unlike "Tiago Mendes & partner", actually
            names the partner. It also has to fit a phone.
            """
            people = []
            for player_id in (first_id, second_id):
                player = self.db.get_player(player_id) if player_id else None
                if player:
                    people.append(player.name)
            if not people:
                return fallback
            if len(people) == 1:
                return people[0] if not is_doubles else f"{people[0]} & partner"
            return " & ".join(name.split()[0] for name in people)

        side_a_label = side_label(a1, a2 if is_doubles else None, "Side A")
        side_b_label = side_label(b1, b2 if is_doubles else None, "Side B")

        formats = "".join(
            f'<option value="{esc(key)}"'
            f'{" selected" if key == match_format else ""}>{esc(spec["label"])}'
            "</option>"
            for key, spec in self.config.match_formats.items())

        admin_extra = ""
        if self.require_admin(req):
            admin_extra = """<div><label><input type="checkbox" name="auto_confirm"
 value="1">Confirm immediately (admin)</label></div>
<div><label><input type="checkbox" name="override" value="1">
Override category checks (admin)</label></div>"""

        reload_to = f"/submit?division={division}&format=' + this.value + '"
        return f"""<h1>Submit a result</h1>
<p class="sub">Enter it set by set, <b>side A on the left</b>. The winner is
worked out from the score. It counts once someone from the other side confirms
it.</p>
<div class="card"><form class="stack" method="post" action="/submit"
      style="max-width:none">
<input type="hidden" name="csrf" value="{esc(req.csrf)}">

<div class="pair">
<div><label for="division">Division</label>
<select id="division" name="division"
        onchange="window.location='/submit?format={esc(match_format)}&division='+this.value">
{division_options(self.enabled(), division)}</select></div>
<div><label for="format">Match format</label>
<select id="format" name="match_format"
        onchange="window.location='{reload_to}'">{formats}</select>
<div class="hint">Sets how many rows you get.</div></div>
</div>

<div><label>Side A</label>
<div class="pair">
  <select name="a1">{player_options(players, a1)}</select>
  {'<select name="a2">' + player_options(players, a2, blank="-- partner --")
   + '</select>' if is_doubles else ''}
</div></div>

<div><label>Side B</label>
<div class="pair">
  <select name="b1">{player_options(players, b1, blank="-- opponent --")}</select>
  {'<select name="b2">' + player_options(players, b2, blank="-- partner --")
   + '</select>' if is_doubles else ''}
</div></div>

<div><label>Score</label>
{score_grid(side_a_label, side_b_label, rows=rows, tiebreak_row=tiebreak_row)}
<div class="hint">Leave a row blank if the match ended sooner. A tie-break box
appears when a set is 7-6 &mdash; put the actual points, to 5 or to 7, whichever
you played.</div></div>

<div><label for="ending">How did it end?</label>
<select id="ending" name="ending">
  <option value="completed">Played to a finish</option>
  <option value="retired">Someone retired</option>
  <option value="walkover">Walkover &mdash; nobody played</option>
</select>
<div class="hint">For a retirement, enter the score up to the point it stopped.</div></div>

<div id="whowon" hidden><label for="winner_side">Who advanced?</label>
<select id="winner_side" name="winner_side">
  <option value="">Pick one</option>
  <option value="a">{esc(side_a_label)}</option>
  <option value="b">{esc(side_b_label)}</option>
</select>
<div class="hint">Nobody played, so there's no score to work it out from.</div></div>
<script>
(function () {{
  var ending = document.getElementById('ending');
  var who = document.getElementById('whowon');
  var grid = document.getElementById('scoregrid');
  if (!ending || !who) return;
  function sync() {{
    var walkover = ending.value === 'walkover';
    who.hidden = !walkover;
    // A walkover has no score, so the grid only gets in the way.
    if (grid) grid.hidden = walkover;
  }}
  ending.addEventListener('change', sync);
  sync();
}})();

// Keep the score column headers matching the player dropdowns, so you can
// see which column is yours before typing anything into it.
(function () {{
  var doubles = {'true' if is_doubles else 'false'};
  function picked(name) {{
    var el = document.querySelector('[name="' + name + '"]');
    if (!el || !el.value) return null;
    var text = el.options[el.selectedIndex].text.trim();
    return text.indexOf('--') === 0 ? null : text;
  }}
  function label(one, two, fallback) {{
    var names = [picked(one), picked(two)].filter(Boolean);
    if (!names.length) return fallback;
    if (names.length === 1) return doubles ? names[0] + ' & partner' : names[0];
    return names.map(function (n) {{ return n.split(' ')[0]; }}).join(' & ');
  }}
  function sync() {{
    var a = document.getElementById('sideAname');
    var b = document.getElementById('sideBname');
    if (a) a.textContent = label('a1', 'a2', 'Side A');
    if (b) b.textContent = label('b1', 'b2', 'Side B');
  }}
  ['a1', 'a2', 'b1', 'b2'].forEach(function (name) {{
    var el = document.querySelector('[name="' + name + '"]');
    if (el) el.addEventListener('change', sync);
  }});
  sync();
}})();
</script>

<div class="pair">
<div><label for="played_on">Date played</label>
<input id="played_on" name="played_on" type="date" value="{today_iso()}"
       max="{today_iso()}"></div>
<div><label for="note">Note (optional)</label><input id="note" name="note"
     placeholder="Challenge match, court 3"></div>
</div>
{admin_extra}
<div class="row"><button type="submit">Submit result</button>
<a class="btn ghost" href="/">Cancel</a></div>
</form></div>"""

    def _score_shape(self, match_format: str):
        """(number of set rows, which row is a match tie-break) for a format."""
        if match_format == "one_set":
            return 1, None
        if match_format == "two_sets_tb":
            return 3, 3          # two sets then a deciding tie-break to 10
        return 3, None           # best of three


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TennisLadder/2.0"
        set_cookie: Optional[str] = None

        def _handle(self, method: str) -> None:
            self.set_cookie = None
            req = None
            try:
                req = Request(self, method, app.db)
                response = app.dispatch(req)
                req.persist()
            except Exception as exc:                    # noqa: BLE001
                self.log_error("unhandled: %s", exc)
                response = Response("<h1>Something went wrong</h1>"
                                    "<p><a href='/'>Back to the ladders</a></p>",
                                    status=500)
            payload = response.body.encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            for key, value in response.headers:
                self.send_header(key, value)
            if self.set_cookie:
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}={self.set_cookie}; Path=/; HttpOnly; SameSite=Lax",
                )
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)

        def do_GET(self) -> None:      # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:     # noqa: N802
            self._handle("POST")

        def log_message(self, fmt: str, *args) -> None:
            print(f"  {self.address_string()} {fmt % args}")

    return Handler


def serve(host: str = "0.0.0.0", port: int = 8000,
          db_path: Optional[str] = None, config: Optional[Config] = None) -> None:
    config = config or CONFIG
    zone = apply_timezone(config)
    db = Database(db_path) if db_path else Database()
    db.purge_sessions()
    app = App(db, config)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    shown = "localhost" if host in ("0.0.0.0", "") else host
    # flush explicitly: stdout is block-buffered when redirected to a log file,
    # and the admin-password warning is the one thing you must not miss.
    print(f"\n  {config.club_name}", flush=True)
    print(f"  http://{shown}:{port}\n", flush=True)
    print(f"  clock: {datetime.now():%Y-%m-%d %H:%M} {zone}", flush=True)
    if not config.timezone:
        print("  ! no timezone set -- using the machine's own, which on a "
              "server is usually UTC.\n    Set \"timezone\" in config.json "
              "(e.g. America/New_York) or every time will be wrong.", flush=True)
    if config.admin_password == "changeme":
        print("  ! Admin password is still 'changeme' -- set admin_password in"
              " your config.json\n", flush=True)
    if not config.email_enabled:
        print("  (email notifications off -- set smtp_host and smtp_from to"
              " enable)\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.shutdown()
