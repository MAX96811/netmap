"""Live state for the map: what is on, what is running, what is reachable.

The scan (collect.py) says what *exists*; this says what it is *doing right
now*. It reads the `watch` map the scan emits and polls three sources at
different rates, because they change at different speeds:

    Home Assistant   3s   lights, switches, media, sensors
    Docker          15s   container up/down, read from data/docker.json
    TCP probes      30s   is that machine actually answering

Docker state arrives as a file written by a cron job on the NAS host rather
than through a mounted docker.sock. The socket would hand root-equivalent
control of the NAS to a container that has no authentication in front of it,
and containers do not change state fast enough to be worth that.

It stops polling entirely when no browser has asked for state in 90 seconds,
so an unwatched map costs Home Assistant and the NAS nothing.
"""
import http.client, json, os, socket, threading, time, urllib.error, urllib.request

# The Home Assistant address is not configured on the server at all: it rides
# along in graph.json, which the scan already deploys on every run. One less
# file to keep in sync, and nothing here has to know which house it is in.
HA_URL_ENV = os.environ.get("NETMAP_HA_URL", "")
DOCKER_SOCK = os.environ.get("NETMAP_DOCKER_SOCK", "/var/run/docker.sock")
IDLE_AFTER = 90          # seconds without a client before the poller sleeps
RATES = {"ha": 3, "docker": 15, "tcp": 30}

# Which domain speaks for a device when it has several. A light that is on is
# more worth showing than the sensor next to it.
PRIORITY = ["light", "media_player", "switch", "climate", "cover", "fan", "lock",
            "vacuum", "binary_sensor", "sensor", "device_tracker", "update"]


class Live:
    def __init__(self, data_dir, token_path):
        self.data_dir = data_dir
        self.token_path = token_path
        self.lock = threading.Lock()
        self.watch, self.parents, self.graph_mtime = {}, {}, 0
        self.ha_url = HA_URL_ENV
        self.raw = {"ha": {}, "docker": {}, "tcp": {}}
        self.sources = {k: {"ok": False, "at": 0, "error": "not polled yet"} for k in RATES}
        self.nodes, self.rev = {}, 0
        self.last_client = 0
        threading.Thread(target=self._loop, daemon=True).start()

    # ------------------------------------------------------------- plumbing
    def touch(self):
        """A browser asked for state — keep polling."""
        self.last_client = time.time()

    def snapshot(self):
        with self.lock:
            return {"rev": self.rev, "at": int(time.time()), "n": self.nodes,
                    "sources": {k: {"ok": v["ok"], "age": round(time.time() - v["at"], 1) if v["at"] else None,
                                    "error": v.get("error")} for k, v in self.sources.items()}}

    def _token(self):
        try:
            return open(self.token_path).read().strip()
        except OSError:
            return ""

    def _reload_watch(self):
        g = os.path.join(self.data_dir, "graph.json")
        try:
            m = os.path.getmtime(g)
        except OSError:
            return
        if m == self.graph_mtime:
            return
        try:
            data = json.load(open(g))
        except Exception:
            return
        self.graph_mtime = m
        self.watch = data.get("watch", {})
        self.ha_url = HA_URL_ENV or data.get("ha_url", "")
        self.parents = {n["id"]: n.get("parent") for n in data.get("nodes", [])}

    def _loop(self):
        nxt = {k: 0.0 for k in RATES}
        while True:
            if time.time() - self.last_client > IDLE_AFTER:
                time.sleep(2)
                continue
            self._reload_watch()
            now = time.time()
            for key, fn in (("ha", self._poll_ha), ("docker", self._poll_docker), ("tcp", self._poll_tcp)):
                if now >= nxt[key]:
                    nxt[key] = now + RATES[key]
                    try:
                        fn()
                        self.sources[key] = {"ok": True, "at": time.time(), "error": None}
                    except Exception as e:
                        self.sources[key] = {"ok": False, "at": self.sources[key].get("at", 0),
                                             "error": "%s: %s" % (type(e).__name__, e)}
            self._recompute()
            time.sleep(0.5)

    # -------------------------------------------------------------- sources
    def _poll_ha(self):
        if not self.ha_url:
            raise RuntimeError("no Home Assistant url — set NETMAP_HA_URL or rescan")
        token = self._token()
        if not token:
            raise RuntimeError("no Home Assistant token")
        req = urllib.request.Request(self.ha_url + "/api/states", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=8) as r:
            states = json.load(r)
        self.raw["ha"] = {s["entity_id"]: s for s in states}

    def _poll_docker(self):
        f = os.path.join(self.data_dir, "docker.json")
        if os.path.exists(f):
            age = time.time() - os.path.getmtime(f)
            if age > 600:
                raise RuntimeError("docker.json is %d minutes stale" % (age / 60))
            with open(f) as fh:
                self.raw["docker"] = json.load(fh)
            return
        # Fallback for running the server somewhere the socket is safe to use.
        if not os.path.exists(DOCKER_SOCK):
            raise RuntimeError("no docker.json and no docker socket")
        c = _UnixHTTP(DOCKER_SOCK)
        try:
            c.request("GET", "/v1.43/containers/json?all=1")
            r = c.getresponse()
            body = r.read()
            if r.status != 200:
                raise RuntimeError("docker %s" % r.status)
            self.raw["docker"] = {n.lstrip("/"): ct.get("State", "")
                                  for ct in json.loads(body) for n in ct.get("Names", [])}
        finally:
            c.close()

    def _poll_tcp(self):
        out = {}
        targets = [(nid, w["host"], w["port"]) for nid, w in self.watch.items() if w.get("kind") == "tcp"]
        threads, results = [], {}

        def probe(nid, host, port):
            try:
                with socket.create_connection((host, port), timeout=2):
                    results[nid] = True
            except Exception:
                results[nid] = False

        for nid, host, port in targets:
            t = threading.Thread(target=probe, args=(nid, host, port), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(3)
        out.update(results)
        self.raw["tcp"] = out

    # ------------------------------------------------------------- derive
    def _recompute(self):
        ha, dk, tcp = self.raw["ha"], self.raw["docker"], self.raw["tcp"]
        out = {}
        for nid, w in self.watch.items():
            kind = w.get("kind")
            if kind == "ha" and ha:
                st = _device_state(w.get("entities", []), ha)
                if st:
                    out[nid] = st
            elif kind == "docker" and dk:
                s = dk.get(w["name"])
                if s:
                    out[nid] = {"h": "up" if s == "running" else "down", "b": s}
            elif kind == "tcp" and nid in tcp:
                out[nid] = {"h": "up" if tcp[nid] else "down", "b": "" if tcp[nid] else "no answer"}
        # areas summarise the lights and switches of the devices inside them
        if ha:
            for nid, w in self.watch.items():
                if w.get("kind") != "ha_area":
                    continue
                ents = []
                for cid, cw in self.watch.items():
                    if cw.get("kind") == "ha" and self.parents.get(cid) == nid:
                        ents += cw.get("entities", [])
                on = [e for e in ents if e.split(".")[0] in ("light", "switch")
                      and ha.get(e, {}).get("state") == "on"]
                total = [e for e in ents if e.split(".")[0] in ("light", "switch")]
                if total:
                    out[nid] = {"a": 1 if on else 0, "b": "%d/%d on" % (len(on), len(total))}
        with self.lock:
            if out != self.nodes:
                self.nodes = out
                self.rev += 1


def _device_state(entities, ha):
    """One line of truth for a device that may own a dozen entities."""
    by_domain = {}
    for e in entities:
        by_domain.setdefault(e.split(".")[0], []).append(e)
    for dom in PRIORITY:
        ents = by_domain.get(dom)
        if not ents:
            continue
        states = [ha[e] for e in ents if e in ha]
        if not states:
            continue
        vals = [s["state"] for s in states]
        if all(v == "unavailable" for v in vals):
            return {"h": "down", "b": "unavailable"}

        if dom in ("light", "switch", "fan", "lock"):
            on = [s for s in states if s["state"] == "on"]
            if dom == "light" and on:
                br = [s["attributes"].get("brightness") for s in on if s["attributes"].get("brightness")]
                pct = round(sum(br) / len(br) / 2.55) if br else None
                return {"a": 1, "b": "%d%%" % pct if pct else "on",
                        "d": "%d of %d on" % (len(on), len(states)) if len(states) > 1 else None}
            return {"a": 1 if on else 0,
                    "b": ("%d/%d on" % (len(on), len(states))) if len(states) > 1 and on else ("on" if on else "off")}

        if dom == "media_player":
            s = next((x for x in states if x["state"] in ("playing", "paused")), states[0])
            title = s["attributes"].get("media_title") or ""
            return {"a": 1 if s["state"] == "playing" else 0, "b": s["state"],
                    "d": (title[:48] or None)}

        if dom == "climate":
            s = states[0]
            t = s["attributes"].get("current_temperature")
            return {"a": 1 if s["state"] not in ("off", "unavailable") else 0,
                    "b": ("%s°" % t) if t is not None else s["state"]}

        if dom == "cover":
            open_ = [s for s in states if s["state"] == "open"]
            return {"a": 1 if open_ else 0, "b": "open" if open_ else "closed"}

        if dom == "binary_sensor":
            on = [s for s in states if s["state"] == "on"]
            return {"a": 1 if on else 0, "b": "detected" if on else "clear"}

        if dom == "sensor":
            s = next((x for x in states if _num(x["state"]) is not None), None)
            if not s:
                continue
            unit = s["attributes"].get("unit_of_measurement", "")
            return {"a": 0, "b": ("%s %s" % (s["state"], unit)).strip()}

        if dom == "device_tracker":
            home = [s for s in states if s["state"] == "home"]
            return {"a": 1 if home else 0, "b": "home" if home else "away"}

        if dom == "update":
            pending = [s for s in states if s["state"] == "on"]
            if pending:
                return {"a": 0, "b": "update", "h": "warn"}
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class _UnixHTTP(http.client.HTTPConnection):
    """http.client over a unix socket — enough to read the Docker API."""
    def __init__(self, path):
        super().__init__("localhost", timeout=6)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.unix_path)
        self.sock = s
