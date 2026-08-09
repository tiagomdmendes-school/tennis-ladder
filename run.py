#!/usr/bin/env python3
"""Start the tennis ladder.

    python3 run.py                 # http://localhost:8000
    python3 run.py --port 9000
    python3 run.py --demo          # load a demo club and start
    python3 run.py --data-dir /data     # for Docker / Fly volumes

Nothing to install: standard library only, Python 3.10+.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the tennis ladder server.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="interface to bind (default: all, so phones on the "
                             "club wifi can reach it)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir",
                        help="directory holding ladder.db and config.json; "
                             "point this at a mounted volume when deploying")
    parser.add_argument("--db", help="database file (overrides --data-dir)")
    parser.add_argument("--demo", action="store_true",
                        help="seed a demo club first (only if the database is empty)")
    args = parser.parse_args()

    # The data directory has to be settled before anything imports the config,
    # because importing it loads (and creates) config.json immediately. Setting
    # the environment variable first means the very first import already points
    # at the right place. Importing the module and *then* redirecting it would
    # leave a stray config.json behind in the project directory -- which is
    # worse than untidy: it's a second file that looks like the real one, so
    # editing the wrong copy silently does nothing.
    if args.data_dir:
        os.environ["LADDER_DATA_DIR"] = os.path.abspath(args.data_dir)

    from ladder.config import CONFIG, DB_PATH
    from ladder.web import serve

    db_path = args.db or DB_PATH
    if args.demo:
        from tools.seed_demo import seed
        seed(db_path)

    serve(host=args.host, port=args.port, db_path=db_path, config=CONFIG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
