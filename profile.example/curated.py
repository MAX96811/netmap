"""Hand-maintained knowledge that cannot be auto-discovered.

Everything in here is authored, not scanned: what a container is *for*, which
services actually talk to each other, and the traps worth remembering. The
collector merges this on top of the live scan.

Edit this file when the *meaning* of your infrastructure changes; edit
nothing when only its contents change — the scan handles that.

Node ids you can refer to (all created by the scan):
    host:pc / host:nas      the machines ([pc].node_id / [nas].node_id)
    ct:<docker name>        a container
    vm:<libvirt name>       a VM on the workstation
    vm:nas:<name>           a VM on a TrueNAS server
    hw:cpu hw:gpu hw:ram    workstation hardware
    app:<key>               an entry of PC_APPS below
    ha:core / area:<id> / dev:<id>    Home Assistant
    ts:<hostname>           a tailnet peer
    cf:<hostname>           a public route
"""

# ---------------------------------------------------------------- containers
# docker name -> (group, icon, what it is for, url to open on double-click)
# A running container that is missing here still appears — with a warning —
# so the map tells you when this file has fallen behind reality.
CONTAINERS = {
    "homeassistant": ("home",  "🏠", "Home Assistant core", "http://192.168.1.20:8123"),
    "jellyfin":      ("media", "🎬", "Movies and shows", "http://192.168.1.10:8096"),
    "postgres":      ("dev",   "🐘", "Shared database", None),
    "netmap":        ("dev",   "🗺", "This map", None),
}

# group key -> (title of the box, icon)
CONTAINER_GROUPS = {
    "home":  ("Home Automation", "🏠"),
    "media": ("Media",           "🎵"),
    "dev":   ("Dev & Tools",     "🛠"),
}

# ------------------------------------------------------------ local PC apps
# Only software that actually reaches another machine. Purely local programs
# would be nodes that connect to nothing — leave them off the map.
# links: (target node id, kind, label) — drawn once both ends exist.
PC_APPS = [
    dict(key="moonlight", label="Moonlight", icon="🎮", url=None,
         what="Game streaming client for the living-room TV.",
         links=[("host:nas", "stream", "Sunshine")]),
]

# ------------------------------------------------------------ public routes
# (hostname, scheme, origin port, what it serves) — needs [public] enabled.
CLOUDFLARE_ROUTES = [
    # ("jellyfin.example.com", "http", 8096, "media library"),
]

# A TrueNAS server reports no address for its VMs; the known ones go here.
VM_IPS = {}

# ----------------------------------------------------------- semantic edges
# (from, to, kind, label) — kind: net | data | control | depends | stream.
# The scan only sees wires; this is where you draw the *meaning*.
EDGES = [
    ("ct:homeassistant", "ct:postgres", "depends", "recorder"),
]

# --------------------------------------------------------- extra host nodes
# Machines and devices no scan from this box can see. Same fields as any
# node; parent decides which box it lands in.
EXTRA_NODES = [
    dict(id="host:router", type="network", label="Router", sub="192.168.1.1",
         icon="🛡", layer="network", parent="group:net", meta={}),
    dict(id="internet", type="cloud", label="Internet", sub="",
         icon="🌍", layer="network", parent="group:net", meta={}),
]

# ------------------------------------------------------------------- traps
# node id -> the sentence that will save future-you an evening. Shown as a
# warning box in the inspector.
TRAPS = {
    # "host:nas": "The NIC name changes when you re-cable — macvlan dies with it.",
}

# ------------------------------------------------------------------- notes
# node id -> one line the inspector shows with the scanned facts. For what a
# scan can never know: which driver branch you are stuck on, and why.
NOTES = {
    # "hw:gpu": "Stay on 550 until the suspend bug is fixed upstream.",
}

# ------------------------------------------------------------ device icons
# For Home Assistant devices: first keyword found anywhere in
# "manufacturer model name" wins, so put the specific before the generic.
DEVICE_ICONS = [
    ("sonos", "🔈"), ("hue", "💡"), ("camera", "📹"), ("printer", "🖨"),
]
