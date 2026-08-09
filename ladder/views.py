"""HTML rendering.

Plain functions returning strings -- no template engine, so the app has zero
dependencies. Every value that came from a user goes through `esc()`.
"""

from __future__ import annotations

import html
from datetime import date
from typing import Iterable, List, Optional, Sequence

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
) -> str:
    def nav_link(href: str, key: str, label: str) -> str:
        cls = ' class="active"' if key == active else ""
        return f'<a href="{href}"{cls}>{esc(label)}</a>'

    links = [
        nav_link("/", "ladder", "Ladders"),
        nav_link("/submit", "submit", "Submit result"),
        nav_link("/pending", "pending", "Confirm"),
        nav_link("/matches", "matches", "Matches"),
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
