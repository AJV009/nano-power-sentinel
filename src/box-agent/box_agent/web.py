"""HTTP surface: GET /vitals and GET /health.  Nothing else exists yet."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import config, power, vitals, __version__

PORT = 9009
BIND = "0.0.0.0"


class Handler(BaseHTTPRequestHandler):
    server_version = "box-agent/" + __version__

    def log_message(self, fmt, *args):
        pass  # do not spam the journal with one line per 5 s poll

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        bits = self.path.split("?", 1)
        route = bits[0].rstrip("/") or "/"
        query = {}
        if len(bits) > 1:
            query = {k: v[0] for k, v in parse_qs(bits[1]).items()}

        if route == "/health":
            return self._send(200, {"ok": True, "version": __version__})
        if route == "/can-hibernate":
            return self._send(200, {"authorised": power.can_hibernate()})
        if route == "/config":
            return self._send(200, {"tunables": config.read(),
                                    "spec": config.SPEC})
        if route == "/vitals":
            try:
                return self._send(200, vitals.build(query.get("cursor")))
            except Exception as exc:
                # Never a bare 500 -- a silent failure here looks identical to
                # the box being asleep, which is a completely different fact.
                return self._send(500, {"error": "%s: %s"
                                        % (type(exc).__name__, exc)})
        return self._send(404, {"error": "no such endpoint", "path": route})

    def do_POST(self):
        if self.path.rstrip("/") != "/hibernate":
            return self._send(404, {"error": "no such endpoint"})
        ok, detail = power.hibernate()
        return self._send(200 if ok else 500, {"ok": ok, "detail": detail})

    def do_PUT(self):
        if self.path.rstrip("/") != "/config":
            return self._send(404, {"error": "no such endpoint"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 65536:
                raise ValueError("bad content length")
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as exc:
            return self._send(400, {"error": "bad request: %s" % exc})
        try:
            applied, rejected = config.apply(body)
        except Exception as exc:
            return self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
        return self._send(200 if applied or not rejected else 422,
                          {"applied": applied, "rejected": rejected})


def serve():
    vitals.start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print("[box-agent] %s listening on %s:%d  gpu=%s"
          % (__version__, BIND, PORT, vitals.gpu_pci()), flush=True)
    srv.serve_forever()
