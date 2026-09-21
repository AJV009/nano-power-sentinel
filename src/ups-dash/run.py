#!/usr/bin/env python3
"""ups-dash entry point.  Stdlib only -- the jetson has no pip environment."""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ups_dash.collector import Collector          # noqa: E402
from ups_dash.server import serve                 # noqa: E402
from ups_dash.store import Store                  # noqa: E402

from ups_dash import settings                   # noqa: E402

DB = settings.DB_PATH
BOX = os.environ.get("UPS_DASH_BOX") or settings.BOX_URL
PORT = settings.DASH_PORT
BIND = settings.DASH_BIND


def main():
    # Print the resolved configuration at startup: a deployment that never
    # got a site config is then obvious in the first log line, rather than
    # discovered during an outage when WoL quietly targets nothing.
    print("[ups-dash] config: %s" % settings.summary(), flush=True)
    if settings.looks_unconfigured():
        print("[ups-dash] WARNING: still using placeholder defaults -- "
              "set /etc/ups-dash/site.env (see .env.example)", flush=True)
    store = Store(DB)
    collector = Collector(store, box_url=BOX)
    threading.Thread(target=collector.run, daemon=True).start()
    serve(collector, store, BIND, PORT)


if __name__ == "__main__":
    main()
