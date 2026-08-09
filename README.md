# Tennis Ladder

A club tennis ladder for a college team. Members submit their own results, an
opponent confirms, and five ladders update themselves — men's and women's
singles and doubles, plus mixed. Ratings use **Glicko-2**, so the standings
reflect *who* you beat, and in doubles, *who you had to carry*.

No dependencies, no build step, no accounts to sign up for. Python 3.10+ and one
command.

```bash
python3 run.py --demo     # seeded demo club, http://localhost:8000
python3 run.py            # your own empty ladder
```

---

## Why not just win/loss?

A win-percentage ladder rewards ducking strong opponents: beat the bottom three
twice each and you sit above someone who went 3-3 against the top four. Plain
Elo fixes that but treats everyone's rating as equally trustworthy, which breaks
down in a club where people play six matches a season.

Glicko-2 carries three numbers per player, **per division**:

| | |
|---|---|
| **Rating** | the skill estimate (everyone starts at 1500) |
| **RD** | rating deviation — how sure we are (starts ±350, shrinks with play, drifts up during inactivity) |
| **Volatility** | how erratic their results are, which sets how fast their rating may move |

What that buys you:

- Beating the #1 moves you far more than beating the #20; losing to the #1
  barely costs you.
- A newcomer converges in about three matches instead of twenty.
- **Ladder position is `rating − 1×RD`**, so you climb by *proving* a rating,
  but a short season doesn't rank everyone by attendance.

### The scoreline counts too

A win is worth 80% of the credit outright; the other 20% is split by share of
games won:

| Score | Winner's credit |
|---|---|
| 6-0 6-0 | 1.00 |
| 6-4 6-4 | 0.92 |
| 7-6 7-6 | 0.91 |

Winning is what matters, but a thrashing is recorded as a thrashing. The two
sides' credits always sum to 1, so margin never creates or destroys rating.
Retirements and walkovers ignore it entirely.

---

## Doubles: how your partner counts

Doubles is rated **per player**, but what you're measured against is the gap
between the two *teams*. Each player's match reduces to a Glicko-2 update
against a **virtual opponent**, placed so the gap they face equals the gap their
team faced:

```
d      = mean(your side) − mean(their side)
v_you  = your rating − d
rd_v   = sqrt(sum of everyone else's RD²) / n
```

**You 1600, weak partner 1200, opponents both 1500.** Your team averages 1400
against their 1500, so you're scored as though you personally faced a **1700**
player. Winning pays accordingly:

| Your partner | You gain on a win |
|---|---|
| 1200 (weak) | **+22.2** |
| 1600 (equal) | +12.7 |
| 1900 (strong) | +7.0 |

Your partner's virtual opponent is 1300 — one hundred above *their* rating — so
you both carry **exactly the same team win expectation** (36.69%), and differ
only in how far each of you moves, which Glicko-2 already scales by your own RD.
That shared-expectation property is why the model is a reduction rather than an
ad-hoc "weak partner bonus": the credit falls out of the arithmetic and stays
symmetric.

Singles is the `n = 1` case of the same formula, so both share one code path.

---

## Divisions

Five ladders, each with its own independent rating per player — you can be #2 in
men's singles and #7 in mixed:

`Men's Singles` · `Women's Singles` · `Men's Doubles` · `Women's Doubles` ·
`Mixed Doubles`

Each player has a category (men's / women's / unspecified) set by an admin.
Lineups that don't fit a division are **blocked**, since a mis-clicked entry
quietly distorts a ladder for weeks; admins have an override for genuine
exceptions.

**Cross-division seeding:** your first match in a doubles division starts from
your singles rating at that moment, held loosely (±300). A hint, not a claim —
a genuinely different doubles player corrects it within a few matches, and it
saves a short season from being all settling-in.

---

## Seasons

College rosters turn over every year, so seasons reset the competition without
losing what the club has learned.

- Admin starts a new season from `/admin`. Nothing is deleted; past seasons stay
  readable via the season picker.
- **Ratings carry over** with uncertainty widened to at least ±150 — where you
  finished seeds where you start, without freezing it.
- Graduating players get deactivated: they leave the ladders, their history
  stays.
- Recruits start fresh at 1500 ±350.

Every seed comes from a player's **own results** — last season, or their own
singles rating. Nothing encodes anyone's opinion of how good someone is.

---

## Who you play well with

The player page lists every partner with matches together, W–L, and
**performance vs expectation** — how much better the pair does than the four
players' ratings predict, per match.

This is the number raw W–L can't give you. From the demo data:

| Partner | Together | W–L | vs expected |
|---|---|---|---|
| Ben Okafor | 8 | 8-0 | **+0.16** |
| Liam Doyle | 8 | 4-4 | **+0.08** |
| Noah Fischer | 4 | 3-1 | **−0.16** |

A 3-1 record scoring *worse* than a 4-4 record is the whole point: the 4-4 came
against opposition the pair was expected to lose to. Pairs with fewer than three
matches together are flagged as thin.

---

## How a result gets onto the ladder

1. A player signs in and submits the score, written from **side A's** point of
   view. The winner is worked out from it.
2. Someone from the **other side** confirms it — one opponent is enough, and
   your own partner can't sign off a result your side entered.
3. Ratings recompute and the ladder redraws.

That two-step is the whole anti-cheat story, and it works because the people
motivated to check a result are the ones who lost it.

### Score formats accepted

Club formats vary, so enter whatever you actually played — side A's games first:

| Format | Type this |
|---|---|
| **A single set** — the usual challenge match | `6-4` |
| One set decided on a tie-break | `7-6` or `7-6(5)` |
| Two sets and a deciding match tie-break | `6-4 4-6 10-8` |
| Full best of three | `6-4 3-6 6-2` |
| Eight-game pro set | `8-6` |
| Fast4 | `4-2` |
| Someone retired | `6-3 2-1 ret.` |
| Walkover | `w/o` |

Sets may be separated by spaces or commas; `-`, `:` and `/` all work. A
deciding-set match tie-break counts as one game rather than ten, so a
super-tie-break can't outweigh the two real sets before it.

**Every match counts the same regardless of format** — a single set moves your
rating as much as a three-setter. That's deliberate: one set is the normal
format here, and down-weighting short matches would slow every rating from
settling, which is exactly what a short season can't afford. The trade-off is
that an occasional full three-setter carries no more weight than a quick single
set.

A score with the sets level and no decider (`6-4 3-6`) is refused — that's an
unfinished match, not a format.

---

## Email notifications

Off until an admin configures SMTP (stdlib `smtplib`, still zero dependencies).
Each player then picks **which** notifications they want, individually, on their
own `/me` settings page:

1. A result needs your confirmation
2. Your submitted result was confirmed or disputed
3. Weekly ladder summary
4. New season started

Nothing is sent to anyone who hasn't opted in, and every message carries a
working one-click unsubscribe link for its own type. Sending happens on a
background thread — **a broken mail server can never break a result
submission**.

---

## Finding matches outside practice

**Availability.** Each player sets their *usual week* once on a grid — "Tuesdays
3-6, Thursdays after 4" — which is what a class schedule actually gives you and
doesn't go stale. Anything that differs is a one-tap exception on a real date:
the next fortnight is listed with a **Can't make it** button per day. The whole
thing is designed around the fact that nobody maintains a detailed calendar.

**Requesting a match.** Open someone's profile, hit **Find a time**, and you get
the windows you're both actually free, soonest first — filtered to gaps long
enough for the format you picked. A 60-minute Thursday overlap is offered for a
single set but not for best-of-three. One click sends a request; nothing is
booked until they accept. Accepting cancels your other pending asks with that
player, so you don't end up double-booked.

Match length comes from `match_formats` in the config, so if your sets run long,
change the minutes and every suggestion updates.

## Tournaments

An admin creates one from `/admin`: pick a division, a format, and who's in.

- **Single elimination** — a seeded bracket. Seeds come from current ladder
  position, or a random draw if the ladder is too young to mean much. Byes go to
  the top seeds automatically when the field isn't a power of two, and the top
  two seeds can only meet in the final.
- **Round robin** — everyone plays everyone, with a standings table ranked on
  wins, then head-to-head, then games difference. Better for a small club: one
  bad afternoon doesn't end your tournament.

Each round gets a **play-by date**; players arrange actual times between
themselves using the same availability matching. Results go in the normal way —
submit a score and the draw advances itself. **Tournament matches count towards
ladder ratings** like any other match.

When a deadline passes with a match unplayed, nothing happens automatically. The
tournament page flags it to the admin, who can extend the round or send someone
through — a scheduling mix-up shouldn't silently knock out your best player.

## Pages

| | |
|---|---|
| `/` | the ladders — points, rating ±RD, record, form, movement, season picker |
| `/tournaments` | brackets and standings |
| `/schedule` | requests waiting on you, agreed matches, what you've asked |
| `/availability` | your usual week, and blocking out specific days |
| `/find/<id>` | times you and one opponent are both free |
| `/submit` | submit a result (form adapts to the division) |
| `/pending` | confirm or dispute |
| `/matches` | full history, filterable by division |
| `/player/<id>` | per-division cards, partner chemistry, rating charts, matches |
| `/me` | your email, notification toggles, PIN |
| `/admin` | players, categories, PINs, seasons, CSV import, delete |
| `/about` | how the rating works, with this club's actual settings |
| `/api/ladder.json`, `/export/*.csv` | data out |

---

## Setup for a real club

```bash
python3 run.py          # writes data/config.json on first run
```

Edit `data/config.json` and restart. **Set `admin_password` — it starts as
`changeme`** and the server warns you on every boot.

Then, as admin: add each player with their category, import any past results,
and share the URL. **There are no PINs to hand out** — each player picks their
own the first time they sign in, so it's something they'll actually remember. If
someone forgets theirs, clear it from `/admin` and they choose again; nobody,
including you, can read an existing PIN.

### Settings worth knowing

| Setting | Default | What it does |
|---|---|---|
| `conservative_k` | 1.0 | the `rating − k×RD` used for ladder order |
| `provisional_matches` | 3 | matches before the "provisional" tag comes off |
| `rating_period_days` | 7 | Glicko-2 batches results into periods |
| `margin_weight` | 0.20 | how much the scoreline counts; `0.0` = pure win/loss |
| `season_carryover_rd` | 150 | uncertainty a carried-over rating restarts at |
| `cross_division_rd` | 300 | uncertainty when seeding doubles from singles |
| `challenge_up_positions` | 3 | how far above you you may challenge (advisory) |
| `rematch_cooldown_days` | 0 | quick-rematch warning; 0 disables it |
| `enabled_divisions` | all five | trim to hide any |
| `require_confirmation` | true | set false to have results count immediately |

Challenge rules are advisory by design: the app shows them and warns, but never
blocks a result. Clubs bend their own rules constantly, and a ladder that
refuses real matches gets abandoned.

---

## Importing existing results

Columns `date,division,player_a,player_b,score`, plus `player_a2`/`player_b2`
for doubles and an optional `note`. Score from side A's point of view. Unknown
names are added automatically.

```csv
date,division,player_a,player_a2,player_b,player_b2,score,note
2026-03-01,mens_singles,Ana Silva,,Ben Okafor,,6-4 3-6 10-8,challenge
2026-03-02,mixed_doubles,Ana Silva,Ben Okafor,Chiara Rossi,Devon Park,6-4 6-2,
```

Upload or paste it at `/admin`, or use the CLI. Bad rows are reported
individually and skipped; the good ones still land.

---

## Command line

```bash
python3 -m tools.ladderctl standings              # all five ladders
python3 -m tools.ladderctl standings mens_doubles
python3 -m tools.ladderctl partners "Ana Silva"   # doubles chemistry
python3 -m tools.ladderctl add-player "Ana Silva" --category womens
python3 -m tools.ladderctl record mixed_doubles "Ana,Ben" "Cara,Dan" "6-4 6-2"
python3 -m tools.ladderctl season "Spring 2027"
python3 -m tools.ladderctl export matches > backup.csv
```

---

## Deploying

See **[DEPLOYING.md](DEPLOYING.md)**. It leads with a field-by-field
**Oracle Cloud Always Free** walkthrough (free indefinitely, no ongoing cost) —
every step of the create-instance form, the two-firewalls trap that stops most
first deployments, free HTTPS via DuckDNS + Caddy, and a troubleshooting table.
Also covers a VPS with Caddy, Fly.io, Cloudflare Tunnel, WSGI hosts, and testing
on your phone over the club wifi (including the **WSL2 mirrored-networking
fix**, without which phones can't reach a server running under WSL).

Before making it public: set `admin_password`, serve over HTTPS, and set
`base_url`. PINs are stored hashed (PBKDF2-SHA256) and sessions live in the
database, so a restart doesn't sign the club out.

---

## How it's put together

```
run.py               start the server
deploy.sh            pull + restart, on the server
ladder/
  glicko2.py         the rating maths — pure functions, no I/O
  doubles.py         the virtual-opponent reduction for team play
  divisions.py       the five ladders and who may enter each
  availability.py    interval maths: who's free, and when two people overlap
  tournaments.py     seeded brackets, round-robin rotation, standings
  scoring.py         tennis scores in, [0,1] rating scores out
  storage.py         SQLite: players, seasons, matches, availability, draws
  migrations.py      schema versioning and PIN hashing
  engine.py          replays match history into every ladder
  scheduling.py      suggested times and match requests
  service.py         the rules (who may confirm what, running a tournament)
  mailer.py          opt-in notifications, on a background queue
  web.py / views.py  router, HTML, and the rating charts
  wsgi.py            entry point for gunicorn / PythonAnywhere
tools/               seed_demo.py, ladderctl.py
tests/               352 tests
data/                ladder.db and config.json (created on first run)
```

**Ratings are never stored.** They're recomputed from the confirmed match list
every time a ladder is drawn — one chronological pass per season across all
divisions at once. Correcting a result from six weeks ago fixes every rating
downstream of it automatically. A few thousand matches replay in milliseconds.

The other consequence: `data/ladder.db` holds only what people actually entered.
Copy that one file and you have a complete backup.

---

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

352 tests, no dependencies. The ones worth knowing about:

- **`test_availability.py`** pins the interval arithmetic underneath the
  scheduler: merging, subtracting a blocked afternoon, intersecting two weeks,
  and refusing to suggest a gap too short for the format.
- **`test_tournaments.py`** checks the draw is a real bracket — top two seeds in
  opposite halves, byes to the top seeds — and that a three-way cycle in a round
  robin is left unresolved rather than given a fake order.

- **`test_glicko2.py`** checks the implementation against the worked example in
  Glickman's own paper (1464.06 / RD 151.52). If this fails, everything built on
  it is wrong.
- **`test_doubles.py`** encodes the club's actual requirement: winning with a
  weak partner must gain several times more than winning behind a strong one,
  both partners must share one team expectation, and scores must stay symmetric
  so no rating leaks.
- **`test_recovery.py`** gives simulated players a hidden strength — separately
  for singles and doubles — plays seasons from it, and checks each ladder
  rediscovers the right order, averaged over several seasons and compared
  against the displacement of a random ordering. It also checks the doubles
  ladder tracks *doubles* strength rather than copying singles.
- **`test_migrations.py`** builds a database in the old schema and verifies
  every match, winner and PIN survives the upgrade.
- **`test_web.py`** runs a real server on a loopback port and drives it over
  HTTP, so cookies, CSRF, sessions-across-restart and the submit→confirm
  workflow are exercised as a browser would.

---

## Deliberate limitations

- **One process.** Sessions persist in SQLite, but the rating cache is
  in-memory. Run a single worker — it's ample for a club.
- **PINs gate identity, not value.** They answer "did the right person confirm
  this result", and are stored hashed so nobody can read them back. Players
  choose their own on first sign-in; anyone who reaches the site before a
  teammate does could claim that name, which is a deliberate trade for not
  making the captain distribute secrets. An admin can clear a PIN to undo it.
- **Categories are a two-way split** (men's / women's / unspecified) because the
  divisions the club competes in are structured that way. `unspecified` players
  can be added and rated but must be categorised before entering a gendered
  division.
