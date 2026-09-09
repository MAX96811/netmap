#!/usr/bin/env python3
"""Static host for the homelab map, plus a place to keep hand edits.

Deliberately tiny and stdlib-only so it runs on a bare python:alpine image.
It is reachable from the LAN and the tailnet with no auth, so the only write
it accepts is the map's own overrides file, and it never executes anything.

    /                  -> web/index.html
    /data/graph.json   -> the scan output (read-only here; collect.py writes it)
    /api/overrides     -> GET the hand edits, PUT to replace them
    /api/state         -> what is on / running / reachable right now
"""
import json, os, shutil, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(ROOT, "web")
DATA = os.path.join(ROOT, "data")
OVERRIDES = os.path.join(DATA, "overrides.json")
BACKUPS = os.path.join(DATA, "backups")
PORT = int(os.environ.get("PORT", "8180"))
MAX_BODY = 8 * 1024 * 1024
HA_TOKEN = os.path.join(ROOT, ".ha-token")   # outside web/ and data/, so never served

# The map still works without live state; it just stops moving.
try:
    from live import Live
    LIVE = Live(DATA, HA_TOKEN)
except Exception as exc:                                    # pragma: no cover
    sys.stderr.write("live state unavailable: %r\n" % (exc,))
    LIVE = None

TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".json": "application/json",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
         ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
         ".map": "application/json", ".txt": "text/plain; charset=utf-8"}


class Handler(BaseHTTPRequestHandler):
    server_version = "netmap"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not os.environ.get("QUIET"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------- helpers
    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", cache=False):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def safe_path(self, base, rel):
        """Resolve rel under base, refusing anything that escapes it."""
        p = os.path.normpath(os.path.join(base, rel.lstrip("/")))
        return p if p == base or p.startswith(base + os.sep) else None

    def serve_file(self, path):
        if not path or not os.path.isfile(path):
            return self.send(404, "not found")
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            self.send(200, f.read(), TYPES.get(ext, "application/octet-stream"))

    # ----------------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/overrides":
            if os.path.exists(OVERRIDES):
                return self.serve_file(OVERRIDES)
            return self.send(200, "{}", "application/json")
        if path == "/api/state":
            if not LIVE:
                return self.send(200, json.dumps({"rev": 0, "n": {}, "sources": {}}), "application/json")
            LIVE.touch()
            return self.send(200, json.dumps(LIVE.snapshot()), "application/json")
        if path == "/api/health":
            g = os.path.join(DATA, "graph.json")
            return self.send(200, json.dumps({
                "ok": True,
                "graph_mtime": os.path.getmtime(g) if os.path.exists(g) else None,
                "overrides_bytes": os.path.getsize(OVERRIDES) if os.path.exists(OVERRIDES) else 0,
                "live": LIVE.snapshot()["sources"] if LIVE else None,
            }), "application/json")
        if path.startswith("/data/"):
            return self.serve_file(self.safe_path(DATA, path[len("/data/"):]))
        if path == "/":
            path = "/index.html"
        return self.serve_file(self.safe_path(WEB, path))

    do_HEAD = do_GET

    # ----------------------------------------------------------------- PUT
    def do_PUT(self):
        if self.path.split("?")[0] != "/api/overrides":
            return self.send(404, "not found")
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self.send(400, "bad length")
        if n <= 0 or n > MAX_BODY:
            return self.send(413, "body too large")
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
            assert isinstance(data, dict)
        except Exception:
            return self.send(400, "expected a JSON object")

        os.makedirs(BACKUPS, exist_ok=True)
        # Keep the last 50 saves. This file is hand-made work with no other
        # copy anywhere, so a bad write must always be recoverable.
        #
        # Only rotate when something other than the viewport moved. Panning and
        # zooming save constantly, and rotating on those burned the whole
        # history in seconds — which is exactly how a real edit became
        # untraceable. The camera position is not content.
        if os.path.exists(OVERRIDES):
            try:
                with open(OVERRIDES) as fh:
                    prev = json.load(fh)
            except Exception:
                prev = None
            def content(d):
                return {k: v for k, v in d.items() if k != "view"} if isinstance(d, dict) else d
            if prev is None or content(prev) != content(data):
                shutil.copy2(OVERRIDES, os.path.join(BACKUPS, "overrides-%s.json" % time.strftime("%Y%m%d-%H%M%S")))
                old = sorted(os.listdir(BACKUPS))
                for f in old[:-50]:
                    os.remove(os.path.join(BACKUPS, f))
        tmp = OVERRIDES + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, OVERRIDES)
        self.send(200, json.dumps({"ok": True}), "application/json")


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    print("netmap serving %s on :%d" % (ROOT, PORT), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
