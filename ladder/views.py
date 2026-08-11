"""HTML rendering.

Plain functions returning strings -- no template engine, so the app has zero
dependencies. Every value that came from a user goes through `esc()`.
"""

from __future__ import annotations

import html
from datetime import date
from typing import Iterable, List, Optional, Sequence

from . import availability as av
from . import divisions as div
from .engine import Ladder, PartnerStat, RatingPoint
from .scoring import flip_score
from .storage import Match, Player, Season


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


# ---------------------------------------------------------------------- CSS
# Colours are the validated data-viz palette: series blue #2a78d6 / #3987e5,
# surfaces #fcfcfb / #1a1a19, muted ink #898781, hairline grid #e1e0d9 / #2c2c2a.
CSS = """
:root {
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb; --raised:#ffffff;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --line:#c3c2b7; --border:rgba(11,11,11,.10);
  --series-1:#2a78d6; --good:#0ca30c; --good-ink:#006300;
  --warn:#fab219; --critical:#d03b3b;
  --radius:10px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --plane:#0d0d0d; --surface:#1a1a19; --raised:#232322;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --line:#383835; --border:rgba(255,255,255,.10);
    --series-1:#3987e5; --good:#0ca30c; --good-ink:#0ca30c;
    --warn:#fab219; --critical:#d03b3b;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--plane); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
}
a { color:var(--series-1); text-decoration:none; }
a:hover { text-decoration:underline; }
.wrap { max-width:1020px; margin:0 auto; padding:0 20px 64px; }

header.top { border-bottom:1px solid var(--border); background:var(--surface); }
header.top .wrap { padding:14px 20px; display:flex; align-items:center;
  gap:20px; flex-wrap:wrap; }
.brand { font-weight:650; font-size:17px; color:var(--ink); }
nav { display:flex; gap:18px; flex-wrap:wrap; margin-left:auto; align-items:center; }
nav a { color:var(--ink-2); font-size:14px; }
nav a.active { color:var(--ink); font-weight:600; }
.who { font-size:13px; color:var(--muted); }
.badge { display:inline-grid; place-items:center; min-width:17px; height:17px;
  margin-left:5px; padding:0 5px; border-radius:999px; background:var(--critical);
  color:#fff; font-size:11px; font-weight:700; line-height:1;
  vertical-align:1px; font-variant-numeric:tabular-nums; }

.divnav { display:flex; gap:6px; flex-wrap:wrap; margin:0 0 20px; }
.divnav a { padding:6px 13px; border-radius:999px; font-size:13.5px;
  border:1px solid var(--border); color:var(--ink-2); background:var(--surface); }
.divnav a.active { background:var(--series-1); border-color:var(--series-1);
  color:#fff; font-weight:600; }
.divnav a:hover { text-decoration:none; border-color:var(--line); }
.divnav a.active:hover { border-color:var(--series-1); }

h1 { font-size:24px; margin:24px 0 4px; letter-spacing:-.01em; }
h2 { font-size:17px; margin:30px 0 10px; }
.sub { color:var(--ink-2); margin:0 0 20px; font-size:14px; max-width:64ch; }

.card { background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius); padding:18px 20px; margin-bottom:18px; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; }

table { width:100%; border-collapse:collapse; font-size:14px; }
th { text-align:left; font-weight:600; font-size:12px; letter-spacing:.04em;
  text-transform:uppercase; color:var(--muted); padding:0 10px 8px;
  border-bottom:1px solid var(--grid); white-space:nowrap; }
td { padding:10px; border-bottom:1px solid var(--grid); vertical-align:middle; }
tr:last-child td { border-bottom:none; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.rank { width:46px; color:var(--muted); font-variant-numeric:tabular-nums; }
.name { font-weight:600; }
.you td { background:color-mix(in srgb, var(--series-1) 7%, transparent); }

.pill { display:inline-block; padding:1px 7px; border-radius:999px;
  font-size:11px; font-weight:600; border:1px solid var(--border);
  color:var(--ink-2); background:var(--plane); }
.pill.prov { color:var(--muted); }
.form { display:inline-flex; gap:3px; }
.form b { width:17px; height:17px; border-radius:4px; font-size:10.5px;
  font-weight:700; display:grid; place-items:center; color:#fff; }
.form b.W { background:var(--good); }
.form b.L { background:var(--critical); }
.delta { font-size:12px; font-variant-numeric:tabular-nums; }
.delta.up { color:var(--good-ink); }
.delta.down { color:var(--critical); }
.delta.flat { color:var(--muted); }

form.stack { display:grid; gap:14px; max-width:560px; }
label { display:block; font-size:13px; font-weight:600; margin-bottom:5px; }
.hint { font-weight:400; color:var(--muted); font-size:12px; margin-top:4px; }
input, select, textarea, button {
  font:inherit; color:var(--ink); background:var(--raised);
  border:1px solid var(--line); border-radius:8px; padding:9px 11px; width:100%;
}
input[type=checkbox] { width:auto; margin-right:8px; vertical-align:-2px; }
textarea { min-height:150px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:13px; }
button, .btn {
  background:var(--series-1); color:#fff; border:none; font-weight:600;
  cursor:pointer; width:auto; padding:10px 18px; display:inline-block; }
button:hover { filter:brightness(1.07); }
button.ghost, .btn.ghost { background:transparent; color:var(--ink-2);
  border:1px solid var(--line); }
button.danger { background:var(--critical); }
button.small, .btn.small { padding:6px 12px; font-size:13px; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.pair { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
@media (max-width:560px) { .pair { grid-template-columns:1fr; } }

.flash { border-radius:var(--radius); padding:12px 16px; margin:18px 0;
  font-size:14px; border:1px solid var(--border); }
.flash.ok { background:color-mix(in srgb,var(--good) 12%,var(--surface));
  border-color:color-mix(in srgb,var(--good) 40%,transparent); }
.flash.err { background:color-mix(in srgb,var(--critical) 12%,var(--surface));
  border-color:color-mix(in srgb,var(--critical) 40%,transparent); }
.flash.warn { background:color-mix(in srgb,var(--warn) 16%,var(--surface));
  border-color:color-mix(in srgb,var(--warn) 45%,transparent); }
.flash ul { margin:6px 0 0; padding-left:20px; }

.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(115px,1fr)); gap:14px; }
.stat .k { font-size:12px; color:var(--muted); text-transform:uppercase;
  letter-spacing:.04em; }
.stat .v { font-size:26px; font-weight:650; letter-spacing:-.02em; }
.stat .d { font-size:12px; color:var(--ink-2); }
.empty { color:var(--muted); font-size:14px; padding:8px 0; }
.scroll { overflow-x:auto; }
footer { color:var(--muted); font-size:12.5px; margin-top:40px;
  border-top:1px solid var(--border); padding-top:16px; }
figcaption { color:var(--ink-2); font-size:13px; margin-bottom:10px; }
.chem { font-variant-numeric:tabular-nums; font-weight:600; }
.chem.pos { color:var(--good-ink); }
.chem.neg { color:var(--critical); }

/* Availability grid. Half-hour rows, so it has to stay compact. */
table.grid { border-collapse:separate; border-spacing:2px; }
table.grid th { text-align:center; padding:0 0 4px; }
table.grid td.tlabel { padding:0 8px 0 0; text-align:right; white-space:nowrap;
  color:var(--muted); font-size:11.5px; font-variant-numeric:tabular-nums;
  border:none; height:15px; }
table.grid td.slot { padding:0; width:13%; height:15px; border:none;
  border-radius:3px; background:var(--plane);
  box-shadow:inset 0 0 0 1px var(--grid); }
table.grid td.slot.on { background:var(--series-1);
  box-shadow:inset 0 0 0 1px var(--series-1); }
table.grid input[type=checkbox] { margin:0; width:100%; height:15px; }
button.daytoggle { background:none; border:none; padding:2px 4px; width:auto;
  color:var(--muted); font-size:12px; font-weight:600; letter-spacing:.04em;
  text-transform:uppercase; cursor:pointer; }
button.daytoggle:hover { color:var(--ink); }
/* Seven columns have to fit a phone without sideways scrolling, or nobody
   fills this in on the walk to practice. */
@media (max-width:560px) {
  table.grid { border-spacing:1px; width:100%; }
  table.grid td.tlabel { font-size:10px; padding-right:4px; }
  table.grid td.slot { height:17px; }
  button.daytoggle { font-size:10px; padding:2px 1px; letter-spacing:0; }
}
/* Once the script is running the boxes are redundant -- the cell is the
   control -- so hide them but keep them in the form. Size must be pinned to
   1px: absolute positioning with width:100% resolves against the viewport
   (there is no positioned ancestor), which made every hidden box as wide as
   the screen and pushed the page into horizontal scroll on a phone. */
table.grid.painting input[type=checkbox] { position:absolute; opacity:0;
  pointer-events:none; width:1px; height:1px; margin:0; padding:0; border:0; }
table.grid.painting td.slot { cursor:pointer; }
table.grid.painting { user-select:none; -webkit-user-select:none; }

/* Knockout bracket: absolutely positioned boxes over an SVG of connectors. */
.bbox { position:absolute; transform:translateY(-50%);
  background:var(--surface); border:1px solid var(--border);
  border-left:3px solid var(--line); border-radius:8px; padding:6px 10px;
  font-size:13.5px; box-sizing:border-box; }
.bbox.done { border-left-color:var(--good); }
.bbox.live { border-left-color:var(--series-1); }
.bbox .brow { white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  line-height:1.55; }
.bbox a.bwin { font-weight:700; color:var(--ink); }
.bbox a.bfind { display:block; font-size:11px; margin-top:1px; }

/* Set-by-set score entry. */
.scoregrid { display:grid; gap:8px; max-width:560px; }
.setrow { display:grid; grid-template-columns:96px 84px 18px 84px 1fr;
  align-items:center; gap:8px; }
/* Names wrap rather than truncate -- "TIAGO ..." tells you nothing about
   which column is yours, which is the one job this row has. */
.setrow.head { align-items:end; }
.setrow.head .sidename { font-size:11.5px; font-weight:600; color:var(--muted);
  text-transform:uppercase; letter-spacing:.03em; text-align:center;
  line-height:1.25; }
.setlabel { font-size:13px; font-weight:600; }
.setlabel .hint { display:block; margin:0; font-weight:400; }
.scoregrid .dash { text-align:center; color:var(--muted); }
.sbox, .tbox { text-align:center; font-variant-numeric:tabular-nums;
  padding:9px 6px; width:100%; }
.tbox { padding:5px 4px; font-size:13px; }
.tbwrap { display:grid; grid-template-columns:auto 46px 12px 46px;
  align-items:center; gap:5px; }
.tbwrap .hint { margin:0; white-space:nowrap; }
/* With the script running, only show a tie-break line where one happened. */
.scoregrid.live .tbwrap { display:none; }
.scoregrid.live .tbwrap.show { display:grid; }
@media (max-width:560px) {
  .setrow { grid-template-columns:80px 1fr 14px 1fr; }
  .tbwrap { grid-column:1 / -1; justify-content:start; }
}

h3 { font-size:15px; margin:20px 0 8px; }
"""


# ------------------------------------------------------------------- layout
def page(
    title: str,
    body: str,
    *,
    club: str,
    active: str = "",
    player: Optional[Player] = None,
    is_admin: bool = False,
    flashes: Sequence[tuple] = (),
    badges: Optional[dict] = None,
) -> str:
    badges = badges or {}

    def nav_link(href: str, key: str, label: str) -> str:
        cls = ' class="active"' if key == active else ""
        count = badges.get(key) or 0
        # A count rather than a bare dot: "2 to confirm" is worth acting on,
        # a dot only tells you to go and look.
        mark = f'<span class="badge">{count}</span>' if count else ""
        return f'<a href="{href}"{cls}>{esc(label)}{mark}</a>'

    links = [
        nav_link("/", "ladder", "Ladders"),
        nav_link("/tournaments", "tournaments", "Tournaments"),
        nav_link("/schedule", "schedule", "Matches"),
        nav_link("/availability", "availability", "Availability"),
        nav_link("/submit", "submit", "Submit"),
        nav_link("/pending", "pending", "Confirm"),
        nav_link("/matches", "matches", "History"),
        nav_link("/admin", "admin", "Admin"),
    ]
    if player:
        who = (f'<span class="who"><a href="/me">{esc(player.name)}</a> &middot; '
               f'<a href="/logout">sign out</a></span>')
    elif is_admin:
        who = '<span class="who">admin &middot; <a href="/logout">sign out</a></span>'
    else:
        who = '<span class="who"><a href="/login">sign in</a></span>'

    flash_html = "".join(
        f'<div class="flash {esc(kind)}">{text}</div>' for kind, text in flashes
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} &middot; {esc(club)}</title>
<style>{CSS}</style>
</head><body>
<header class="top"><div class="wrap">
  <a class="brand" href="/">{esc(club)}</a>
  <nav>{''.join(links)}{who}</nav>
</div></header>
<div class="wrap">{flash_html}{body}
<footer>Ratings use Glicko-2 &mdash; rating, uncertainty (RD) and volatility per
player, replayed from the full match history every time a ladder is drawn.
Doubles is rated per player against the strength of both teams.
<a href="/about">How the rating works</a></footer>
</div></body></html>"""


def division_nav(current: str, enabled: Sequence[str], *,
                 base: str = "/", season_id: Optional[int] = None,
                 suffix: str = "") -> str:
    parts = []
    for key in div.DIVISION_ORDER:
        if key not in enabled:
            continue
        cls = ' class="active"' if key == current else ""
        query = f"?division={key}"
        if season_id:
            query += f"&season={season_id}"
        parts.append(f'<a href="{base}{query}{suffix}"{cls}>'
                     f'{esc(div.get(key).label)}</a>')
    return f'<div class="divnav">{"".join(parts)}</div>'


def season_picker(seasons: Sequence[Season], current_id: int,
                  division: str) -> str:
    if len(seasons) <= 1:
        return ""
    options = "".join(
        f'<option value="{s.id}"{" selected" if s.id == current_id else ""}>'
        f'{esc(s.name)}{" (current)" if s.is_current else ""}</option>'
        for s in reversed(seasons)
    )
    return f"""<form method="get" action="/" class="row" style="margin:0 0 18px">
<input type="hidden" name="division" value="{esc(division)}">
<label for="season" style="margin:0 8px 0 0">Season</label>
<select id="season" name="season" style="width:auto" onchange="this.form.submit()">
{options}</select>
<noscript><button class="small ghost" type="submit">Show</button></noscript>
</form>"""


# ------------------------------------------------------------------- ladder
def ladder_table(ladder: Ladder, *, viewer_id: Optional[int], cfg) -> str:
    if not ladder.entries:
        return ('<div class="card"><p class="empty">No results in this division '
                'yet. <a href="/submit">Submit one</a> and the ladder appears.</p>'
                '</div>')

    rows = []
    for e in ladder.entries:
        change = ""
        if e.rank_change > 0:
            change = f'<span class="delta up">&#9650;{e.rank_change}</span>'
        elif e.rank_change < 0:
            change = f'<span class="delta down">&#9660;{abs(e.rank_change)}</span>'
        prov = ' <span class="pill prov" title="Rating still settling">provisional</span>' \
            if e.provisional else ""
        form = ('<span class="form">'
                + "".join(f'<b class="{r}">{r}</b>' for r in e.form)
                + "</span>") if e.form else '<span class="empty">&mdash;</span>'
        rows.append(f"""<tr class="{'you' if e.player.id == viewer_id else ''}">
  <td class="rank">{e.rank}</td>
  <td class="name"><a href="/player/{e.player.id}">{esc(e.player.name)}</a>{prov}</td>
  <td class="num">{round(e.ladder_points)}</td>
  <td class="num">{round(e.rating.rating)} <span class="hint">&plusmn;{round(e.rating.rd)}</span></td>
  <td class="num">{e.record}</td>
  <td>{form}</td>
  <td class="num">{change}</td>
</tr>""")

    return f"""<div class="card scroll"><table>
<thead><tr>
  <th>#</th><th>Player</th>
  <th class="num" title="rating minus {cfg.conservative_k:g}x uncertainty">Points</th>
  <th class="num">Rating</th><th class="num">W&ndash;L</th>
  <th>Form</th><th class="num">Move</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


# -------------------------------------------------------------- rating chart
def rating_chart(history: List[RatingPoint], *, name: str, what: str) -> str:
    """Rating over time: one 2px line, a 10%-opacity uncertainty band, one
    end-dot with a surface ring, and a direct end-label. One series, so no
    legend -- the caption says what is plotted."""
    if len(history) < 2:
        return ('<p class="empty">Two or more rating periods needed before the '
                'trend is worth drawing.</p>')

    w, h = 760, 250
    pad_l, pad_r, pad_t, pad_b = 46, 74, 16, 30
    inner_w, inner_h = w - pad_l - pad_r, h - pad_t - pad_b

    lo = min(p.rating - p.rd for p in history)
    hi = max(p.rating + p.rd for p in history)
    span = max(hi - lo, 80.0)
    lo, hi = lo - span * 0.06, hi + span * 0.06
    step = _nice_step((hi - lo) / 4)
    lo = step * (lo // step)
    hi = step * (hi // step + 1)

    def x(i: int) -> float:
        return pad_l + inner_w * (i / (len(history) - 1))

    def y(value: float) -> float:
        return pad_t + inner_h * (1 - (value - lo) / (hi - lo))

    gridlines, ticks = [], []
    value = lo
    while value <= hi + 1e-9:
        gy = round(y(value), 1)
        gridlines.append(
            f'<line x1="{pad_l}" y1="{gy}" x2="{pad_l + inner_w}" y2="{gy}" '
            f'stroke="var(--grid)" stroke-width="1"/>'
        )
        ticks.append(
            f'<text x="{pad_l - 9}" y="{gy + 4}" text-anchor="end" font-size="11" '
            f'fill="var(--muted)" style="font-variant-numeric:tabular-nums">'
            f'{round(value):,}</text>'
        )
        value += step

    band_top = " ".join(f"{round(x(i),1)},{round(y(p.rating + p.rd),1)}"
                        for i, p in enumerate(history))
    band_bottom = " ".join(f"{round(x(i),1)},{round(y(p.rating - p.rd),1)}"
                           for i, p in reversed(list(enumerate(history))))
    line = " ".join(f"{round(x(i),1)},{round(y(p.rating),1)}"
                    for i, p in enumerate(history))

    hovers = []
    slot = inner_w / max(len(history) - 1, 1)
    for i, p in enumerate(history):
        hovers.append(
            f'<rect x="{round(x(i) - slot/2,1)}" y="{pad_t}" width="{round(slot,1)}" '
            f'height="{inner_h}" fill="transparent"><title>{esc(p.on)}\n'
            f'Rating {round(p.rating)} ±{round(p.rd)}\n'
            f'{p.matches_played} matches played</title></rect>'
        )

    last = history[-1]
    ex, ey = x(len(history) - 1), y(last.rating)
    return f"""<figure style="margin:0">
<figcaption>{esc(name)}'s {esc(what)} rating over time. The band is the
uncertainty range (&plusmn;RD) &mdash; it narrows as they play more.</figcaption>
<svg viewBox="0 0 {w} {h}" width="100%" height="auto" role="img"
     aria-label="Rating history for {esc(name)} in {esc(what)}"
     style="max-width:100%;display:block;background:var(--surface)">
  {''.join(gridlines)}
  <polygon points="{band_top} {band_bottom}" fill="var(--series-1)" opacity="0.10"/>
  <polyline points="{line}" fill="none" stroke="var(--series-1)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>
  <line x1="{pad_l}" y1="{pad_t + inner_h}" x2="{pad_l + inner_w}"
        y2="{pad_t + inner_h}" stroke="var(--line)" stroke-width="1"/>
  <circle cx="{round(ex,1)}" cy="{round(ey,1)}" r="4.5" fill="var(--series-1)"
          stroke="var(--surface)" stroke-width="2"/>
  <text x="{round(ex + 10,1)}" y="{round(ey + 4,1)}" font-size="12.5" font-weight="600"
        fill="var(--ink)" style="font-variant-numeric:tabular-nums">{round(last.rating)}</text>
  {''.join(ticks)}
  <text x="{pad_l}" y="{h - 9}" font-size="11" fill="var(--muted)">{esc(history[0].on)}</text>
  <text x="{pad_l + inner_w}" y="{h - 9}" font-size="11" fill="var(--muted)"
        text-anchor="end">{esc(last.on)}</text>
  {''.join(hovers)}
</svg></figure>"""


def _nice_step(raw: float) -> float:
    for candidate in (10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if raw <= candidate:
            return float(candidate)
    return 2000.0


# ------------------------------------------------------------------ matches
def _side_html(ids: Sequence[int], names: dict, *, bold: bool) -> str:
    parts = []
    for pid in ids:
        label = esc(names.get(pid, "?"))
        inner = f"<b>{label}</b>" if bold else label
        parts.append(f'<a href="/player/{pid}">{inner}</a>')
    return ' <span class="hint">&amp;</span> '.join(parts)


def match_rows(
    matches: Iterable[Match],
    names: dict,
    *,
    actions: str = "",
    csrf: str = "",
    show_division: bool = True,
) -> str:
    rows = []
    for m in matches:
        # Scores are stored from side A's point of view but read here as
        # "winners def. losers", so turn them round when A was the losing side.
        score = m.score if m.winner_side == "a" else flip_score(m.score)
        status = ""
        if m.status != "confirmed":
            tone = "prov" if m.status == "pending" else ""
            status = f' <span class="pill {tone}">{esc(m.status)}</span>'
        buttons = ""
        if actions == "confirm":
            buttons = f"""<form method="post" action="/pending" class="row"
   style="gap:6px;margin:0">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="match_id" value="{m.id}">
  <button class="small" name="action" value="confirm">Confirm</button>
  <button class="small ghost" name="action" value="reject">Dispute</button>
</form>"""
        elif actions == "admin":
            buttons = f"""<form method="post" action="/admin/match" class="row"
   style="gap:6px;margin:0" onsubmit="return confirm('Delete this result? Ratings will be recalculated.')">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="match_id" value="{m.id}">
  {'<button class="small" name="action" value="confirm">Force confirm</button>'
   if m.status == 'pending' else ''}
  <button class="small danger" name="action" value="delete">Delete</button>
</form>"""
        division_cell = (f'<td><span class="pill">{esc(div.get(m.division).short)}</span></td>'
                         if show_division else "")
        rows.append(f"""<tr>
  <td style="white-space:nowrap;color:var(--ink-2)">{esc(m.played_on)}</td>
  {division_cell}
  <td>{_side_html(m.winners, names, bold=True)}
      <span class="hint">def.</span>
      {_side_html(m.losers, names, bold=False)}{status}</td>
  <td style="white-space:nowrap">{esc(score)}</td>
  <td>{buttons}</td>
</tr>""")
    if not rows:
        return '<p class="empty">Nothing here yet.</p>'
    head = ("<th>Date</th>" + ("<th></th>" if show_division else "")
            + "<th>Result</th><th>Score</th><th></th>")
    return ('<div class="scroll"><table><thead><tr>' + head
            + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


# ------------------------------------------------------------------ partners
def partner_table(stats: Sequence[PartnerStat], names: dict) -> str:
    """Who this player is good with -- judged against expectation, not W-L."""
    if not stats:
        return ('<p class="empty">No doubles matches yet. Play some and this '
                'fills in.</p>')
    rows = []
    for s in stats:
        per = s.per_match
        tone = "pos" if per > 0.02 else ("neg" if per < -0.02 else "")
        thin = ' <span class="pill prov">thin</span>' if s.thin else ""
        rows.append(f"""<tr>
  <td class="name"><a href="/player/{s.partner_id}">
    {esc(names.get(s.partner_id, '?'))}</a>{thin}</td>
  <td><span class="pill">{esc(div.get(s.division).short)}</span></td>
  <td class="num">{s.played}</td>
  <td class="num">{s.record}</td>
  <td class="num chem {tone}">{per:+.2f}</td>
</tr>""")
    return f"""<div class="scroll"><table><thead><tr>
<th>Partner</th><th></th><th class="num">Together</th><th class="num">W&ndash;L</th>
<th class="num" title="Average wins above what the ratings predicted">
  vs expected</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="hint" style="margin-top:10px">"vs expected" is how much better the pair
does than the four players' ratings predict, per match. Positive means you
over-perform together &mdash; which plain W&ndash;L can't tell you, because it
doesn't know how hard the matches were.</p>"""


# -------------------------------------------------------------- score entry
def score_grid(side_a_label: str, side_b_label: str, *, rows: int = 3,
               tiebreak_row: Optional[int] = None) -> str:
    """A two-column set-by-set score entry grid.

    Replaces a free-text box, which left every player inventing their own
    format and made typos indistinguishable from unusual scorelines. Here the
    shape of a tennis score is built into the form: a row per set, a box each
    side of a dash, and a tie-break line that appears only when a set is 7-6.

    `rows` comes from the match format, so a one-set match shows one row.
    `tiebreak_row` marks a deciding match tie-break (played to 10) so it can be
    labelled as such rather than looking like a wrong set score.
    """
    lines = []
    for index in range(1, rows + 1):
        is_match_tb = tiebreak_row == index
        label = "Match tie-break" if is_match_tb else f"Set {index}"
        hint = " to 10" if is_match_tb else ""
        lines.append(f"""<div class="setrow" data-set="{index}">
  <div class="setlabel">{esc(label)}<span class="hint">{hint}</span></div>
  <input class="sbox" name="s{index}a" inputmode="numeric" maxlength="2"
         aria-label="{esc(side_a_label)} games in set {index}">
  <span class="dash">&ndash;</span>
  <input class="sbox" name="s{index}b" inputmode="numeric" maxlength="2"
         aria-label="{esc(side_b_label)} games in set {index}">
  <div class="tbwrap" data-for="{index}">
    <span class="hint">tie-break</span>
    <input class="tbox" name="t{index}a" inputmode="numeric" maxlength="2"
           aria-label="{esc(side_a_label)} tie-break points in set {index}">
    <span class="dash">&ndash;</span>
    <input class="tbox" name="t{index}b" inputmode="numeric" maxlength="2"
           aria-label="{esc(side_b_label)} tie-break points in set {index}">
  </div>
</div>""")

    return f"""<div class="scoregrid" id="scoregrid">
  <div class="setrow head">
    <div class="setlabel"></div>
    <div class="sidename" id="sideAname">{esc(side_a_label)}</div>
    <span class="dash"></span>
    <div class="sidename" id="sideBname">{esc(side_b_label)}</div>
  </div>
  {''.join(lines)}
</div>
<script>{_SCORE_SCRIPT}</script>"""


# Shows the tie-break line only when a set actually went to one. Without this
# every row carries four boxes and the form looks far more daunting than the
# thing it is recording.
_SCORE_SCRIPT = """
(function () {
  var grid = document.getElementById('scoregrid');
  if (!grid) return;
  grid.classList.add('live');

  function refresh(row) {
    var a = row.querySelector('input[name^="s"][name$="a"]');
    var b = row.querySelector('input[name^="s"][name$="b"]');
    var wrap = row.querySelector('.tbwrap');
    if (!a || !b || !wrap) return;
    var x = parseInt(a.value, 10), y = parseInt(b.value, 10);
    // A tie-break decided the set when it finished 7-6 either way.
    var went = (x === 7 && y === 6) || (x === 6 && y === 7);
    wrap.classList.toggle('show', went);
    if (!went) {
      wrap.querySelectorAll('input').forEach(function (i) { i.value = ''; });
    }
  }

  grid.querySelectorAll('.setrow[data-set]').forEach(function (row) {
    row.addEventListener('input', function () { refresh(row); });
    refresh(row);
  });
})();
"""


# -------------------------------------------------------------- availability
def availability_grid(weekly: dict, blocks: Sequence[tuple]) -> str:
    """The 'usual week' grid: drag across the times you're normally free.

    Checkboxes are the real inputs, so the form submits and works with no
    JavaScript at all -- but at half-hour resolution that's 200 boxes, which
    nobody is going to tick one at a time. The script below paints across them
    by dragging, and a click on a day name toggles the whole column. The
    checkboxes are visually hidden once the script runs, so what you actually
    see is a block of coloured cells.
    """
    header = "".join(
        f'<th><button type="button" class="daytoggle" data-day="{index}">'
        f"{esc(day)}</button></th>"
        for index, day in enumerate(av.WEEKDAY_SHORT))
    rows = []
    for start, end in blocks:
        cells = []
        for weekday in range(7):
            on = av.covers(weekly.get(weekday, []), (start, end))
            cells.append(
                f'<td class="slot{" on" if on else ""}" data-day="{weekday}">'
                f'<input type="checkbox" name="slot" '
                f'value="{weekday}-{start}-{end}"{" checked" if on else ""} '
                f'aria-label="{esc(av.WEEKDAYS[weekday])} {esc(av.clock(start))}">'
                f"</td>")
        # Only label the hour, so the half-hour rows read as subdivisions.
        label = esc(av.clock(start)) if start % 60 == 0 else ""
        rows.append(f'<tr><td class="tlabel">{label}</td>{"".join(cells)}</tr>')

    return f"""<div class="scroll"><table class="grid" id="availgrid">
<thead><tr><th></th>{header}</tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="hint" id="availhelp">Tick the times you're usually free.</p>
<script>{_GRID_SCRIPT}</script>"""


# Drag-painting over the grid. Kept deliberately small and dependency-free;
# without it the plain checkboxes still work.
_GRID_SCRIPT = """
(function () {
  var grid = document.getElementById('availgrid');
  if (!grid) return;
  grid.classList.add('painting');
  var help = document.getElementById('availhelp');
  if (help) help.textContent =
    'Drag across the times you are usually free. Click a day name to toggle the whole column.';

  var down = false, turnOn = true;
  function box(cell) { return cell.querySelector('input'); }
  function paint(cell) {
    var input = box(cell);
    if (!input) return;
    input.checked = turnOn;
    cell.classList.toggle('on', turnOn);
  }
  function cellFrom(target) {
    return target && target.closest ? target.closest('td.slot') : null;
  }

  grid.addEventListener('mousedown', function (e) {
    var cell = cellFrom(e.target);
    if (!cell) return;
    e.preventDefault();
    down = true;
    turnOn = !box(cell).checked;
    paint(cell);
  });
  grid.addEventListener('mouseover', function (e) {
    if (!down) return;
    var cell = cellFrom(e.target);
    if (cell) paint(cell);
  });
  document.addEventListener('mouseup', function () { down = false; });

  // Touch: same idea, but coordinates have to be resolved by hand.
  grid.addEventListener('touchstart', function (e) {
    var cell = cellFrom(e.target);
    if (!cell) return;
    down = true;
    turnOn = !box(cell).checked;
    paint(cell);
    e.preventDefault();
  }, {passive: false});
  grid.addEventListener('touchmove', function (e) {
    if (!down) return;
    var touch = e.touches[0];
    var cell = cellFrom(document.elementFromPoint(touch.clientX, touch.clientY));
    if (cell) paint(cell);
    e.preventDefault();
  }, {passive: false});
  document.addEventListener('touchend', function () { down = false; });

  grid.addEventListener('click', function (e) {
    var toggle = e.target.closest ? e.target.closest('.daytoggle') : null;
    if (!toggle) return;
    e.preventDefault();
    var day = toggle.getAttribute('data-day');
    var cells = grid.querySelectorAll('td.slot[data-day="' + day + '"]');
    var allOn = Array.prototype.every.call(cells, function (c) { return box(c).checked; });
    turnOn = !allOn;
    Array.prototype.forEach.call(cells, paint);
  });
})();
"""


def upcoming_days(
    availability: "av.Availability", days: Sequence, csrf: str
) -> str:
    """The next couple of weeks, resolved, with one-tap 'can't make it'.

    This is the half that keeps the pattern honest -- blocking a single
    afternoon has to be quicker than editing your whole week, or it won't
    happen and the suggestions go stale.
    """
    rows = []
    for day in days:
        free = availability.on(day)
        label = day.strftime("%a %d %b")
        key = day.isoformat()
        # Only offer "undo" where there is actually something to undo -- a day
        # you were simply never free on has nothing blocked.
        has_exception = bool(availability.blocked.get(key)
                             or availability.extra.get(key))
        if not free:
            undo = f"""<form method="post" action="/availability/day" style="margin:0">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="on_date" value="{key}">
  <button class="small ghost" name="action" value="restore">Undo</button>
</form>""" if has_exception else ""
            note = ("blocked out" if has_exception
                    else "not in your usual week")
            rows.append(f"""<tr><td style="white-space:nowrap">{esc(label)}</td>
<td class="empty">{note}</td><td>{undo}</td></tr>""")
            continue
        chips = " ".join(
            f'<span class="pill">{esc(av.clock(s))}&ndash;{esc(av.clock(e))}</span>'
            for s, e in free)
        rows.append(f"""<tr><td style="white-space:nowrap">{esc(label)}</td>
<td>{chips}</td>
<td><form method="post" action="/availability/day" style="margin:0">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="on_date" value="{day.isoformat()}">
  <button class="small ghost" name="action" value="block">Can't make it</button>
</form></td></tr>""")
    return ('<div class="scroll"><table><thead><tr><th>Day</th><th>Free</th>'
            "<th></th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def suggestion_list(slots: Sequence, opponent, division: str, csrf: str,
                    match_format: str) -> str:
    """Times both players can play, each a one-click request."""
    if not slots:
        return ('<p class="empty">No overlap in the next couple of weeks. '
                'Either one of you hasn\'t set availability, or your weeks '
                'genuinely don\'t meet &mdash; you can still propose a time '
                'below.</p>')
    buttons = []
    for slot in slots:
        buttons.append(f"""<form method="post" action="/request" style="margin:0">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="opponent" value="{opponent.id}">
  <input type="hidden" name="division" value="{esc(division)}">
  <input type="hidden" name="match_format" value="{esc(match_format)}">
  <input type="hidden" name="starts_at"
         value="{slot.starts_at.strftime('%Y-%m-%dT%H:%M')}">
  <button class="small ghost" type="submit">{esc(slot.label())}</button>
</form>""")
    return f'<div class="row">{"".join(buttons)}</div>'


def request_rows(requests: Sequence, names: dict, viewer_id: int, csrf: str,
                 kind: str) -> str:
    """kind: 'inbox' (answer it), 'outbox' (cancel it), 'agreed'."""
    if not requests:
        return '<p class="empty">Nothing here.</p>'
    rows = []
    for r in requests:
        other = names.get(r.other(viewer_id), "?")
        when = r.when.strftime("%a %d %b, %-I:%M%p").lower().replace(":00", "")
        note = f'<div class="hint">&ldquo;{esc(r.message)}&rdquo;</div>' if r.message else ""
        if kind == "inbox":
            actions = f"""<form method="post" action="/request/respond" class="row"
   style="gap:6px;margin:0">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="request_id" value="{r.id}">
  <button class="small" name="action" value="accept">Accept</button>
  <button class="small ghost" name="action" value="decline">Decline</button>
</form>"""
        elif kind == "agreed":
            # Once it's arranged, the next thing anyone wants is to record the
            # result -- with the opponent and format already filled in.
            submit_url = (f"/submit?division={esc(r.division)}"
                          f"&format={esc(r.match_format)}"
                          f"&a1={viewer_id}&b1={r.other(viewer_id)}")
            actions = f"""<div class="row" style="gap:6px;margin:0">
  <a class="btn small" href="{submit_url}">Submit result</a>
  <form method="post" action="/request/respond" style="margin:0">
    <input type="hidden" name="csrf" value="{esc(csrf)}">
    <input type="hidden" name="request_id" value="{r.id}">
    <button class="small ghost" name="action" value="cancel">Cancel</button>
  </form>
</div>"""
        else:
            actions = f"""<form method="post" action="/request/respond" style="margin:0">
  <input type="hidden" name="csrf" value="{esc(csrf)}">
  <input type="hidden" name="request_id" value="{r.id}">
  <button class="small ghost" name="action" value="cancel">Cancel</button>
</form>"""
        rows.append(f"""<tr>
  <td style="white-space:nowrap">{esc(when)}</td>
  <td><a href="/player/{r.other(viewer_id)}">{esc(other)}</a>
      <span class="pill">{esc(div.get(r.division).short)}</span>{note}</td>
  <td style="white-space:nowrap;color:var(--ink-2)">{r.minutes} min</td>
  <td>{actions}</td>
</tr>""")
    return ('<div class="scroll"><table><tbody>' + "".join(rows)
            + "</tbody></table></div>")


# -------------------------------------------------------------- tournaments
def bracket_view(rounds: Sequence, matches: Sequence, names: dict, *,
                 viewer_id: Optional[int] = None, division: str = "",
                 match_format: str = "") -> str:
    """A knockout draw drawn as a real bracket.

    Each match is a fixed-height box and the rounds are laid out so a pair in
    one round sits either side of its parent in the next. Connector lines are
    drawn as one SVG behind the boxes, which is what makes it read as a bracket
    rather than three lists side by side.
    """
    by_round: dict = {}
    for m in matches:
        by_round.setdefault(m.round_no, []).append(m)
    if not by_round:
        return '<p class="empty">No draw yet.</p>'

    BOX_H, BOX_W, GAP_X = 56, 190, 46
    first_round = min(by_round)
    base_count = len(by_round[first_round])
    # Pitch leaves room for a box that has grown a "Find a time" line, so a
    # taller box never collides with its neighbour.
    PITCH = BOX_H + 32
    height = max(base_count * PITCH, PITCH) + 44
    width = len(rounds) * (BOX_W + GAP_X)

    # Centre of a box: round one is evenly spaced, and every later box sits
    # midway between the two it is fed by.
    def centre(round_index: int, slot: int) -> float:
        span = PITCH * (2 ** round_index)
        return 34 + span * slot + span / 2

    lines, boxes, headers = [], [], []
    for index, rnd in enumerate(rounds):
        left = index * (BOX_W + GAP_X)
        overdue = (' <tspan fill="var(--critical)">overdue</tspan>'
                   if rnd.is_overdue else "")
        headers.append(
            f'<text x="{left}" y="16" font-size="12" font-weight="600" '
            f'fill="var(--ink-2)">{esc(rnd.name)}{overdue}</text>'
            + (f'<text x="{left}" y="30" font-size="11" fill="var(--muted)">'
               f'by {esc(rnd.deadline)}</text>' if rnd.deadline else ""))

        for m in sorted(by_round.get(rnd.round_no, []), key=lambda x: x.slot):
            boxes.append(_bracket_box(m, names, left, centre(index, m.slot),
                                      BOX_W, BOX_H, viewer_id, division,
                                      match_format))
            # Line from this box across to its parent's edge.
            if index + 1 < len(rounds):
                y = centre(index, m.slot)
                parent_y = centre(index + 1, m.slot // 2)
                mid = left + BOX_W + GAP_X / 2
                lines.append(
                    f'<path d="M{left + BOX_W} {y} H{mid} V{parent_y} '
                    f'H{left + BOX_W + GAP_X}" fill="none" '
                    f'stroke="var(--line)" stroke-width="1.5"/>')

    return f"""<div class="scroll"><div style="position:relative;
     width:{width}px;height:{height}px;min-width:{width}px">
<svg width="{width}" height="{height}" style="position:absolute;inset:0"
     aria-hidden="true">{''.join(lines)}{''.join(headers)}</svg>
{''.join(boxes)}</div></div>"""


def _bracket_box(match, names: dict, left: float, middle: float, w: int, h: int,
                 viewer_id, division: str, match_format: str) -> str:
    is_bye = match.status == "bye"
    won = match.winner_id

    def side(pid) -> str:
        if pid is None:
            return ('<span class="empty">bye</span>' if is_bye
                    else '<span class="empty">&mdash;</span>')
        name = esc(names.get(pid, "?"))
        cls = "bwin" if pid == won else ""
        return f'<a class="{cls}" href="/player/{pid}">{name}</a>'

    # Your own live match gets a direct way to arrange it, so you don't have to
    # go and find the other player's profile.
    action = ""
    if (viewer_id and match.is_ready and viewer_id in match.players):
        opponent = [p for p in match.players if p != viewer_id][0]
        action = (f'<a class="bfind" href="/find/{opponent}?division='
                  f'{esc(division)}&format={esc(match_format)}">Find a time</a>')

    state = "done" if won else ("live" if match.is_ready else "waiting")
    # Positioned by its centre and translated up half its own height, so a box
    # that grows a "Find a time" line stays lined up with its connector instead
    # of spilling out of a fixed height.
    return (f'<div class="bbox {state}" style="left:{left}px;top:{middle}px;'
            f'width:{w}px;min-height:{h}px">'
            f'<div class="brow">{side(match.player_a)}</div>'
            f'<div class="brow">{side(match.player_b)}</div>{action}</div>')


def standings_table(rows: Sequence, names: dict) -> str:
    if not rows:
        return '<p class="empty">No results yet.</p>'
    body = "".join(f"""<tr>
  <td class="rank">{i}</td>
  <td class="name"><a href="/player/{s.player_id}">{esc(names.get(s.player_id,'?'))}</a></td>
  <td class="num">{s.played}</td>
  <td class="num">{s.record}</td>
  <td class="num">{s.games_won}&ndash;{s.games_lost}</td>
  <td class="num">{s.games_diff:+d}</td>
</tr>""" for i, s in enumerate(rows, start=1))
    return (f'<div class="scroll"><table><thead><tr><th>#</th><th>Player</th>'
            f'<th class="num">P</th><th class="num">W&ndash;L</th>'
            f'<th class="num">Games</th><th class="num">Diff</th></tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


# ------------------------------------------------------------------- pickers
def player_options(players: Sequence[Player], selected: Optional[int] = None,
                   *, blank: str = "") -> str:
    out = f'<option value="">{esc(blank)}</option>' if blank else ""
    return out + "".join(
        f'<option value="{p.id}"{" selected" if p.id == selected else ""}>'
        f"{esc(p.name)}</option>"
        for p in players
    )


def division_options(enabled: Sequence[str], selected: Optional[str] = None) -> str:
    return "".join(
        f'<option value="{key}"{" selected" if key == selected else ""}>'
        f"{esc(div.get(key).label)}</option>"
        for key in div.DIVISION_ORDER if key in enabled
    )


def category_options(selected: str = div.UNSPECIFIED) -> str:
    return "".join(
        f'<option value="{key}"{" selected" if key == selected else ""}>'
        f"{esc(label)}</option>"
        for key, label in div.CATEGORY_LABELS.items()
    )


def today_iso() -> str:
    return date.today().isoformat()
