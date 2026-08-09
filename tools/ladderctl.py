#!/usr/bin/env python3
"""Command-line access to the same operations the web app performs.

    python3 -m tools.ladderctl standings mens_singles
    python3 -m tools.ladderctl add-player "Ana Silva" --category womens
    python3 -m tools.ladderctl record mens_singles "Ana" "Ben" "6-4 3-6 10-8"
    python3 -m tools.ladderctl record mixed_doubles "Ana,Ben" "Cara,Dan" "6-4 6-2"
    python3 -m tools.ladderctl partners "Ana Silva"
    python3 -m tools.ladderctl season "Spring 2027"
    python3 -m tools.ladderctl import results.csv
    python3 -m tools.ladderctl export matches > backup.csv
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from ladder import divisions as div
from ladder.config import CONFIG, DB_PATH
from ladder.service import LadderService, ServiceError
from ladder.storage import Database


def _service(db_path: str) -> LadderService:
    return LadderService(Database(db_path), CONFIG)


def _ids(service: LadderService, names: str) -> list:
    out = []
    for name in names.split(","):
        name = name.strip()
        if not name:
            continue
        player = service.db.find_player_by_name(name)
        if not player:
            raise SystemExit(f"No player called {name!r}. "
                             f"Add them first: add-player {name!r}")
        out.append(player.id)
    return out


def cmd_standings(service: LadderService, args) -> None:
    divisions = [args.division] if args.division else div.DIVISION_ORDER
    for key in divisions:
        ladder = service.engine.ladder(key)
        if not ladder.entries:
            continue
        print(f"\n{div.get(key).label}")
        print(f"{'#':>3}  {'Player':22} {'Pts':>5} {'Rating':>9} {'W-L':>7}  Form")
        print("-" * 64)
        for e in ladder.entries:
            flag = "*" if e.provisional else " "
            rating = f"{round(e.rating.rating)}±{round(e.rating.rd)}"
            print(f"{e.rank:>3}{flag} {e.player.name:22} {round(e.ladder_points):>5} "
                  f"{rating:>9} {e.record:>7}  {''.join(e.form)}")
    print("\n* provisional -- rating still settling")


def cmd_partners(service: LadderService, args) -> None:
    player = service.db.find_player_by_name(args.name)
    if not player:
        raise SystemExit(f"No player called {args.name!r}.")
    stats = service.engine.partners_of(player.id)
    if not stats:
        print(f"{player.name} has no doubles matches this season.")
        return
    names = {p.id: p.name for p in service.db.list_players()}
    print(f"\n{player.name} -- partners, best chemistry first")
    print(f"{'Partner':22} {'Div':>4} {'N':>3} {'W-L':>7} {'vs expected':>12}")
    print("-" * 54)
    for s in stats:
        thin = " (thin)" if s.thin else ""
        print(f"{names.get(s.partner_id,'?'):22} {div.get(s.division).short:>4} "
              f"{s.played:>3} {s.record:>7} {s.per_match:>+11.2f}{thin}")
    print("\n'vs expected' is average wins above what the four ratings predicted.")


def cmd_add_player(service: LadderService, args) -> None:
    player = service.add_player(args.name, args.email or "", "", args.category)
    print(f"Added {player.name} ({div.CATEGORY_LABELS[player.category]}). "
          "They choose their own PIN the first time they sign in.")


def cmd_clear_pin(service: LadderService, args) -> None:
    player = service.db.find_player_by_name(args.name)
    if not player:
        raise SystemExit(f"No player called {args.name!r}.")
    service.db.clear_pin(player.id)
    print(f"Cleared {player.name}'s PIN. They'll pick a new one at next sign-in.")


def cmd_record(service: LadderService, args) -> None:
    result = service.submit_result(
        division=args.division,
        side_a=_ids(service, args.side_a), side_b=_ids(service, args.side_b),
        score_text=args.score, played_on=args.date,
        auto_confirm=not args.pending, note=args.note or "",
        allow_category_override=args.override,
    )
    if result.warning:
        print(f"Warning: {result.warning}")
    print(f"Recorded match #{result.match_id} ({result.status}).")


def cmd_season(service: LadderService, args) -> None:
    season = service.start_season(args.name, args.starts_on)
    print(f"Started {season.name}. Ratings carried over with widened uncertainty.")


def cmd_seasons(service: LadderService, args) -> None:
    for s in service.db.seasons():
        mark = " (current)" if s.is_current else ""
        print(f"{s.id:>3}  {s.name:24} {s.starts_on} .. {s.ends_on or '':10}{mark}")


def cmd_pending(service: LadderService, args) -> None:
    names = {p.id: p.name for p in service.db.list_players()}
    matches = service.db.list_matches(status="pending")
    if not matches:
        print("Nothing awaiting confirmation.")
        return
    for m in matches:
        win = " & ".join(names.get(p, "?") for p in m.winners)
        lose = " & ".join(names.get(p, "?") for p in m.losers)
        print(f"#{m.id:<4} {m.played_on}  {div.get(m.division).short}  "
              f"{win} def. {lose}  {m.score}")


def cmd_confirm(service: LadderService, args) -> None:
    service.confirm(args.match_id, None, is_admin=True)
    print(f"Match #{args.match_id} confirmed.")


def cmd_delete(service: LadderService, args) -> None:
    service.delete_match(args.match_id)
    print(f"Match #{args.match_id} deleted; ratings recalculated.")


def cmd_import(service: LadderService, args) -> None:
    with open(args.path, "r", encoding="utf-8") as fh:
        imported, errors = service.import_csv(fh.read(), auto_confirm=not args.pending)
    print(f"Imported {imported} result(s).")
    for problem in errors:
        print(f"  skipped -- {problem}")


def cmd_export(service: LadderService, args) -> None:
    sys.stdout.write(
        service.export_ladder_csv(args.division or div.MENS_SINGLES)
        if args.what == "ladder" else service.export_matches_csv()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ladderctl", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_PATH)
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("standings", help="print a ladder (or all of them)")
    p.add_argument("division", nargs="?", choices=list(div.DIVISION_ORDER))
    p.set_defaults(fn=cmd_standings)

    p = subs.add_parser("partners", help="doubles chemistry for one player")
    p.add_argument("name")
    p.set_defaults(fn=cmd_partners)

    p = subs.add_parser("add-player", help="add a player")
    p.add_argument("name")
    p.add_argument("--category", choices=list(div.CATEGORIES),
                   default=div.UNSPECIFIED)
    p.add_argument("--email")
    p.set_defaults(fn=cmd_add_player)

    p = subs.add_parser("clear-pin", help="forget a player's PIN so they re-pick")
    p.add_argument("name")
    p.set_defaults(fn=cmd_clear_pin)

    p = subs.add_parser("record", help="record a result (score from side A's view)")
    p.add_argument("division", choices=list(div.DIVISION_ORDER))
    p.add_argument("side_a", help="player, or 'player,partner' for doubles")
    p.add_argument("side_b", help="player, or 'player,partner' for doubles")
    p.add_argument("score")
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--note")
    p.add_argument("--override", action="store_true",
                   help="skip the category eligibility check")
    p.add_argument("--pending", action="store_true",
                   help="leave awaiting the opponent's confirmation")
    p.set_defaults(fn=cmd_record)

    p = subs.add_parser("season", help="start a new season")
    p.add_argument("name")
    p.add_argument("--starts-on", dest="starts_on")
    p.set_defaults(fn=cmd_season)

    subs.add_parser("seasons", help="list seasons").set_defaults(fn=cmd_seasons)
    subs.add_parser("pending", help="list unconfirmed results").set_defaults(fn=cmd_pending)

    p = subs.add_parser("confirm", help="confirm a pending result")
    p.add_argument("match_id", type=int)
    p.set_defaults(fn=cmd_confirm)

    p = subs.add_parser("delete", help="delete a result and recalculate")
    p.add_argument("match_id", type=int)
    p.set_defaults(fn=cmd_delete)

    p = subs.add_parser("import", help="bulk-import a results CSV")
    p.add_argument("path")
    p.add_argument("--pending", action="store_true")
    p.set_defaults(fn=cmd_import)

    p = subs.add_parser("export", help="write a ladder or all matches as CSV")
    p.add_argument("what", choices=["ladder", "matches"], nargs="?", default="ladder")
    p.add_argument("--division", choices=list(div.DIVISION_ORDER))
    p.set_defaults(fn=cmd_export)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.fn(_service(args.db), args)
    except ServiceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
