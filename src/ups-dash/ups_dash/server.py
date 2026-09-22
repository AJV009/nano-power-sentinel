"""HTTP server: static bundle + JSON API + SSE.

All paths are relative so Tailscale Serve can front this at any prefix with
HTTPS and a hostname (DASHBOARD.md D2).  The tailnet is the auth boundary;
there is no login screen, which is only acceptable while this stays off the
public internet.
"""

import json
import mimetypes
import os
import queue
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import actions
from . import config as cfgmod
from . import upsops

HEARTBEAT = 15.0
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web")


def make_handler(collector, store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ups-dash/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def handle_error(self, *args):
            # A phone closing an SSE stream, locking its screen or changing
            # network produces ConnectionResetError/BrokenPipe every time.
            # The default handler printed a full traceback for each, burying
            # real errors in noise during exactly the events we care about.
            pass

        # -- plumbing --------------------------------------------------

        def _json(self, code, payload):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _static(self, relpath):
            if not relpath or relpath.endswith("/"):
                relpath = "index.html"
            full = os.path.normpath(os.path.join(WEB_DIR, relpath))
            if not full.startswith(os.path.abspath(WEB_DIR)):
                return self._json(403, {"error": "forbidden"})
            if not os.path.isfile(full):
                return self._json(404, {"error": "not found", "path": relpath})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            try:
                with open(full, "rb") as fh:
                    body = fh.read()
            except Exception as exc:
                return self._json(500, {"error": str(exc)})
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # The bundle changes whenever it is rsynced; never let a phone
            # cache a stale dashboard during an outage.
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = queue.Queue(maxsize=10)
            collector.subscribe(q)
            last = time.time()
            try:
                snap = collector.snapshot()
                if snap:
                    self._emit(snap)
                while True:
                    try:
                        snap = q.get(timeout=HEARTBEAT)
                        self._emit(snap)
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    last = time.time()
            except Exception:
                pass
            finally:
                collector.unsubscribe(q)

        def _emit(self, snap):
            payload = json.dumps(snap, default=str)
            self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
            self.wfile.flush()

        # -- routes ----------------------------------------------------

        def do_GET(self):
            parts = self.path.split("?", 1)
            route = parts[0]
            qs = {}
            if len(parts) > 1:
                from urllib.parse import parse_qs
                qs = {k: v[0] for k, v in parse_qs(parts[1]).items()}

            if route == "/api/now":
                snap = collector.snapshot()
                return self._json(200 if snap else 503,
                                  snap or {"error": "no snapshot yet"})

            if route == "/api/stream":
                return self._stream()

            if route == "/api/recent":
                since = float(qs.get("since") or (time.time() - 900))
                return self._json(200, {"samples": collector.ring_since(since)})

            if route == "/api/episodes":
                limit = min(int(qs.get("limit") or 30), 200)
                before = float(qs["before"]) if qs.get("before") else None
                return self._json(200, {
                    "episodes": store.episodes(limit, before),
                    "events": store.recent_events(120),
                })

            if route.startswith("/api/episode/"):
                try:
                    ep_id = int(route.rsplit("/", 1)[1])
                except Exception:
                    return self._json(400, {"error": "bad episode id"})
                ep = store.episode(ep_id)
                return self._json(200 if ep else 404,
                                  ep or {"error": "no such episode"})

            if route == "/api/health":
                snap = collector.snapshot() or {}
                since = time.time() - 30 * 86400
                return self._json(200, {
                    "stats": store.stats(),
                    "services": collector.services,
                    "nano": snap.get("nano"),
                    "ups": snap.get("ups"),
                    "mains": store.samples_since(since, 3600),
                    "idle": store.samples_since(time.time() - 7 * 86400, 30),
                })

            if route == "/api/config":
                # `tunables` is what the units are ACTUALLY running, learned
                # from their own startup logs.  `files` is what has been
                # written to disk -- they differ only while a unit has not
                # yet reloaded, which is itself worth being able to see.
                return self._json(200, {
                    "tunables": collector.tunables,
                    "editable": True,
                    "bounds": dict(cfgmod.LOCAL_SPEC, **cfgmod.BOX_SPEC),
                    "machines": dict(
                        {k: "jetson" for k in cfgmod.LOCAL_SPEC},
                        **{k: "box" for k in cfgmod.BOX_SPEC}),
                    "wake_reserve_margin": cfgmod.WAKE_RESERVE_MARGIN,
                    "baseline": cfgmod.BASELINE,
                    "files": {"jetson": cfgmod.read_local(),
                              "box": cfgmod.read_box(collector.box.base)},
                    "ups_settings": upsops.describe(
                        (collector.snapshot() or {}).get("ups")),
                    "ups_baseline": upsops.UPS_BASELINE,
                })

            if route.startswith("/api/"):
                return self._json(404, {"error": "no such endpoint",
                                        "path": route})
            return self._static(route.lstrip("/"))

        def do_POST(self):
            route = self.path.split("?", 1)[0].rstrip("/")
            if not route.startswith("/api/control/"):
                return self._json(404, {"error": "no such endpoint"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                body = {}
            action = route.rsplit("/", 1)[1]
            # The dispatch itself (interlock, each action's behaviour) lives
            # in actions.py -- moved there so this file stays under the
            # 300-line cap. See actions.py for the full action list.
            payload, code = actions.handle(action, body, collector, store)
            return self._json(code, payload)

        def do_PUT(self):
            if self.path.rstrip("/") != "/api/config":
                return self._json(404, {"error": "no such endpoint"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 65536:
                    raise ValueError("bad content length")
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception as exc:
                return self._json(400, {"error": "bad request: %s" % exc})
            try:
                result, code = cfgmod.apply(body, collector.box.base,
                                            collector.tunables)
            except Exception as exc:
                return self._json(500, {"error": "%s: %s"
                                        % (type(exc).__name__, exc)})
            if code == 200 and result.get("applied"):
                store.add_event(time.time(), "config_change",
                                collector.episodes.id, result["applied"])
            return self._json(code, result)

    return Handler


def serve(collector, store, bind="0.0.0.0", port=8088):
    handler = make_handler(collector, store)
    srv = ThreadingHTTPServer((bind, port), handler)
    srv.daemon_threads = True
    print("[ups-dash] serving on %s:%d (web=%s)" % (bind, port, WEB_DIR),
          flush=True)
    srv.serve_forever()
