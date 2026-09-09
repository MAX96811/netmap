# netmap

A live, hand-annotatable map of a homelab. One Python file scans everything —
the workstation, the server and its containers, Home Assistant, the tailnet,
the public routes — and one static page draws it as an infinite board you can
rearrange, annotate and keep.

No database, no build step, no framework, no dependencies beyond the Python
standard library. The server is ~200 lines of stdlib http; the frontend is
vanilla JS.

## What it looks like

- Every machine, container, VM, smart-home device and tailnet peer is a card,
  grouped in titled boxes, connected by typed links (network / data / control
  / depends / stream).
- **Live state**: lights show their real brightness, containers go red when
  they stop, machines show reachable/unreachable. Polling stops entirely when
  nobody is looking at the map.
- **Hand edits survive**: positions, renames, icons, notes, extra nodes are
  kept in `overrides.json`, never touched by a re-scan. Positions are stored
  relative to their group, so re-scanning doesn't shuffle your layout.
- **Traps**: every node can carry the one sentence that will save future-you
  an evening ("the NIC name changes when you re-cable — macvlan dies with
  it"). They surface as warning boxes in the inspector.
- **Drawing layer**: real Excalidraw, mounted as a transparent overlay in the
  same coordinate space as the board (optional, `tools/build-vendor.sh`).

## The design

Two files, strictly separated:

| file | written by | contains |
|---|---|---|
| `data/graph.json` | the scan, overwritten every run | what exists |
| `data/overrides.json` | you, in the browser | what you did to it |

And two layers of knowledge:

| layer | lives in | example |
|---|---|---|
| scanned | `collect.py` | "this container publishes :8096" |
| authored | `profile/curated.py` | "that's Jellyfin, it's for movies, and it breaks when..." |

The scan can see wires; only you know meaning. The map shows a warning when a
running container is missing from your authored layer, so the two never
silently drift apart.

## Quick start

```sh
git clone https://github.com/you/netmap && cd netmap
cp -r profile.example profile
$EDITOR profile/config.toml     # your hosts — see the comments
$EDITOR profile/curated.py      # what things are for

./collect.py                    # scan -> data/graph.json
python3 server.py               # http://localhost:8180
```

`profile/` and `data/` are gitignored: nothing about your network can end up
in a repo.

Python ≥ 3.11 (tomllib). The scan uses whatever it finds on the machine —
`lsblk`, `virsh`, `tailscale`, `nvidia-smi`, `ss` — and silently skips what
is missing.

## Running it on the server

The intended shape: scan from your workstation (where your credentials
already live), serve from the machine that is always on.

```sh
./collect.py --deploy    # scp data/graph.json to [nas].deploy_path
```

On the server, any way of running `python3 server.py` works. As a container:

```yaml
services:
  netmap:
    image: python:3.12-alpine
    command: python3 /srv/server.py
    ports: ["8180:8180"]
    volumes: ["/tank/netmap:/srv"]
    restart: unless-stopped
```

Nothing is baked into the image — deploying a change is a copy, not a build.

### Live state

`server.py` starts a poller (from `live.py`) that feeds the map:

| source | rate | how |
|---|---|---|
| Home Assistant | 3 s | one `GET /api/states`; url rides along in graph.json, token in `.ha-token` next to server.py |
| Docker | 15 s | reads `data/docker.json`, written by `nas-docker-state.sh` from cron on the host |
| TCP probes | 30 s | threaded connects to the `[[probe]]` targets |

The poller sleeps when no browser has asked in 90 s, so an unwatched map
costs nothing.

**Do not mount `/var/run/docker.sock` into the map's container.** It hands
root-equivalent control of the host to a container with no authentication in
front of it. The cron-written file gives the same information at one-minute
latency, and containers do not flap second-to-second. (`live.py` still has a
socket fallback for when the server runs somewhere that is actually safe.)

Probe addresses are resolved from wherever `server.py` runs — never use
loopback. `@self` in a probe becomes the workstation's LAN address at scan
time for exactly this reason.

## Security posture

- The server accepts exactly one write: `PUT /api/overrides` (the map's own
  hand edits, size-capped, JSON-validated, last 50 versions kept). It never
  executes anything and only serves `web/` and `data/`.
- The map shows your whole topology with no auth by design — keep it on the
  LAN / tailnet. Don't publish it through your tunnel.
- Credentials never live in config: `config.toml` points at token *files*
  (or env vars `NETMAP_NAS_PASS` / `NETMAP_HA_TOKEN`). SSH key auth +
  passwordless sudo is the good path; `sshpass` + password file is the
  fallback for appliances.

## The drawing layer

```sh
tools/build-vendor.sh   # needs node; produces web/vendor (~22 MB, gitignored)
```

Excalidraw is vendored so the page never touches a CDN. The board and the
overlay share one coordinate space (`zoom = view.z`, `scroll = view/view.z`);
saved drawings load with the map, and the overlay is `pointer-events: none`
unless draw mode is on.

## Extending the scan

`collect.py` is deliberately one readable file. Each `collect_*` function is
independent, guarded by its config section, and only does three things: `add`
nodes, `link` edges, and register `watch` entries for the live poller. Copy
one, point it at your Proxmox/Unraid/whatever, and send a PR.
