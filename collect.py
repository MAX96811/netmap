#!/usr/bin/env python3
"""Scan a homelab and emit data/graph.json for the map.

Runs on your workstation, because that is where every credential already
lives: SSH to the server, the Home Assistant token, and the tailnet.

    ./collect.py            scan and write data/graph.json
    ./collect.py --deploy   scan, write, and push to the server
    ./collect.py --print    scan and dump the graph to stdout

What this file knows: how to *find* things. What it is looking at — which
hosts, which credentials, what each container is for — lives in profile/,
which git never sees. See profile.example/ for a working one.

Hand edits (positions, names, colours, notes, custom nodes) live in
overrides.json on the server and are never touched by this script.
"""
import json, os, re, shlex, subprocess, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from config import CFG, CURATED as curated

nodes, edges, problems = [], [], []
port_owner = {}     # published host port -> container node id
watch = {}          # node id -> how the live poller should track it
_seen = set()

PC_ID = CFG.pc.node_id
NAS_ID = CFG.nas.node_id


def add(id, type, label, sub="", icon="", layer="", parent=None, url=None, meta=None, status=None):
    if id in _seen:
        return
    _seen.add(id)
    n = dict(id=id, type=type, label=label, sub=sub, icon=icon, layer=layer, parent=parent, meta=meta or {})
    if url:
        n["url"] = url
    if status:
        n["status"] = status
    # An authored note beats anything the scan could infer.
    if id in curated.NOTES:
        n["meta"]["note"] = curated.NOTES[id]
    if id in curated.TRAPS:
        n["trap"] = curated.TRAPS[id]
    nodes.append(n)


def link(a, b, kind="net", label=""):
    edges.append(dict({"from": a, "to": b, "kind": kind, "label": label}))


def sh(cmd, timeout=25):
    """Run a shell command, return stdout ('' on any failure)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def nas(cmd, timeout=45, sudo=None):
    """Run a command on the server over SSH.

    Key auth is the good path: leave the password out of the profile, put your
    key on the server, and sudo has to be passwordless there. sshpass is the
    fallback for appliances that will not take a key.
    """
    c = CFG.nas
    if not c.enabled or not c.host:
        return ""
    sudo = c.sudo if sudo is None else sudo
    pw = c.get("password", "")
    if sudo:
        cmd = ("echo %s | sudo -S %s" % (shlex.quote(pw), cmd)) if pw else ("sudo -n %s" % cmd)
    q = shlex.quote(cmd)
    prefix = ("sshpass -p %s " % shlex.quote(pw)) if pw else ""
    return sh("%sssh %s %s@%s %s 2>/dev/null" % (prefix, c.ssh_opts, c.user, c.host, q), timeout=timeout)


def ha_get(path, timeout=20):
    c = CFG.homeassistant
    if not c.enabled or not c.get("token"):
        return ""
    return sh("curl -s --max-time %d -H %s %s%s" % (
        max(timeout - 5, 5), shlex.quote("Authorization: Bearer " + c["token"]), c.url, path), timeout=timeout)


def ha_template(tpl, timeout=40):
    c = CFG.homeassistant
    if not c.enabled or not c.get("token"):
        return ""
    body = json.dumps({"template": tpl})
    return sh("curl -s --max-time %d -X POST -H %s -H 'Content-Type: application/json' -d %s %s/api/template"
              % (timeout - 5, shlex.quote("Authorization: Bearer " + c["token"]), shlex.quote(body), c.url),
              timeout=timeout)


def group(id, label, icon="", layer="", parent=None, sub=""):
    add(id, "group", label, sub=sub, icon=icon, layer=layer, parent=parent)


DEFAULT_PC_GROUPS = [("hw", "Hardware", "\U0001f527"), ("storage", "Disks & Mounts", "\U0001f4bd"),
                     ("vm", "VMs & Containers", "\U0001f4e6"), ("svc", "Services", "\u2699"),
                     ("apps", "Connected apps", "\U0001f680")]


# ======================================================== the workstation
def collect_pc():
    c = CFG.pc
    if not c.enabled:
        return
    group("group:pc", c.label, "\U0001f5a5", "pc", sub=c.sub)
    for gid, lbl, ic in (curated.PC_GROUPS or DEFAULT_PC_GROUPS):
        group("group:pc." + gid, lbl, ic, "pc", parent="group:pc")

    cpu = sh("lscpu | awk -F: '/^Model name/{gsub(/^ +/,\"\",$2); print $2; exit}'")
    cores = sh("nproc")
    mem = sh("free -h | awk '/^Mem:/{print $2}'")
    gpu = sh("nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null")
    kernel = sh("uname -r")
    add(PC_ID, "host", c.host_label or sh("hostname") or "workstation", sub=cpu or "PC", icon="\U0001f5a5",
        layer="pc", parent="group:pc",
        meta={"cpu": cpu, "threads": cores, "ram": mem, "kernel": kernel,
              "os": sh("grep -oP '(?<=^PRETTY_NAME=\").*(?=\")' /etc/os-release")})
    add("hw:cpu", "hardware", cpu or "CPU", sub="%s threads" % cores, icon="\U0001f9e0",
        layer="pc", parent="group:pc.hw")
    if gpu:
        name, vram, drv = [x.strip() for x in (gpu.split(",") + ["", ""])[:3]]
        add("hw:gpu", "hardware", name, sub=vram, icon="\U0001f3ae", layer="pc",
            parent="group:pc.hw", meta={"driver": drv})
    add("hw:ram", "hardware", "RAM", sub=mem, icon="\U0001f4a0", layer="pc", parent="group:pc.hw")
    audio = sh("pactl info 2>/dev/null | awk -F': ' '/Default Sink/{print $2}'")
    if audio:
        add("hw:audio", "hardware", "Audio out", sub=audio[:44], icon="\U0001f50a",
            layer="pc", parent="group:pc.hw")

    # disks
    for line in sh("lsblk -dn -o NAME,SIZE,MODEL").splitlines():
        p = line.split(None, 2)
        if len(p) < 2 or p[0].startswith("zram"):
            continue
        name, size, model = p[0], p[1], (p[2] if len(p) > 2 else "")
        add("disk:" + name, "storage", "/dev/" + name, sub="%s  %s" % (size, model), icon="\U0001f4bd",
            layer="pc", parent="group:pc.storage", meta={"model": model, "size": size})
        link(PC_ID, "disk:" + name, "data", "")
    for line in sh("findmnt -rn -o TARGET,SOURCE,FSTYPE").splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        tgt, src, fs = p[0], p[1], p[2]
        if fs in ("cifs", "nfs", "nfs4", "ntfs3", "vfat") and not tgt.startswith("/boot"):
            add("mount:" + tgt, "storage", tgt, sub="%s · %s" % (fs, src), icon="\U0001f4c1",
                layer="pc", parent="group:pc.storage", meta={"source": src, "fs": fs})
            if CFG.nas.host and CFG.nas.host in src:
                link("mount:" + tgt, NAS_ID, "data", fs.upper())

    # libvirt VMs, with the detail that actually matters: size, passthrough, disks
    for line in sh("virsh -c qemu:///system list --all 2>/dev/null").splitlines()[2:]:
        p = line.split()
        if len(p) < 3:
            continue
        vm, state = p[1], " ".join(p[2:])
        xml = sh("virsh -c qemu:///system dumpxml %s 2>/dev/null" % shlex.quote(vm))
        vcpu = (re.search(r"<vcpu[^>]*>(\d+)</vcpu>", xml) or [None, ""])[1]
        memkb = (re.search(r"<memory[^>]*>(\d+)</memory>", xml) or [None, ""])[1]
        ram = "%d GiB" % round(int(memkb) / 1048576) if memkb else ""
        pci = len(re.findall(r"<hostdev[^>]*type=.pci", xml))
        disks = re.findall(r"<source (?:dev|file)='([^']+)'", xml)
        gfx = ", ".join(sorted(set(re.findall(r"<graphics type='(\w+)'", xml))))
        add("vm:" + vm, "vm", vm, sub="%s vCPU · %s%s" % (vcpu or "?", ram, " · GPU passthrough" if pci else ""),
            icon="\U0001f4bb", layer="pc", parent="group:pc.vm",
            status="up" if "running" in state else "down",
            meta={"state": state, "vcpus": vcpu, "memory": ram, "pci_passthrough": str(pci),
                  "graphics": gfx, "disks": ", ".join(disks)})
        link(PC_ID, "vm:" + vm, "depends", "libvirt")
        # A VM booting a real disk should say so on the map.
        for d in disks:
            if d.startswith("/dev/"):
                dev = os.path.basename(sh("readlink -f %s" % shlex.quote(d)) or d)
                if "disk:" + dev in _seen:
                    link("vm:" + vm, "disk:" + dev, "data", "boots this physical disk")
    if sh("systemctl is-active waydroid-container 2>/dev/null") == "active":
        add("app:waydroid", "app", "Waydroid", sub="Android container", icon="\U0001f4f1",
            layer="pc", parent="group:pc.vm", status="up")

    # services worth naming
    want = {}
    for entry in c.services:
        unit, lbl, ic = (list(entry) + ["", ""])[:3] if isinstance(entry, (list, tuple)) else (entry, entry, "\u2699")
        want[unit] = (lbl or unit, ic or "\u2699")
    running = set(x.split(".service")[0] for x in sh(
        "systemctl list-units --type=service --state=running --no-pager --no-legend | awk '{print $1}'").split())
    for unit, (lbl, ic) in want.items():
        if unit in running:
            add("svc:" + unit, "service", lbl, sub=unit, icon=ic, layer="pc",
                parent="group:pc.svc", status="up")
    if CFG.tailscale.enabled:
        ts_ip = sh("tailscale ip -4 2>/dev/null").strip()
        add("svc:tailscale", "service", "Tailscale", sub=ts_ip or "tailnet", icon="\U0001f517",
            layer="pc", parent="group:pc.svc", status="up")

    # listening ports -> a compact service list
    # A listener only counts if another machine could actually reach it —
    # loopback-only ports (a chat client's RPC, a dev server) are not infrastructure.
    ports = {}
    for line in sh("ss -tlnp").splitlines()[1:]:
        m = re.search(r"(\S+):(\d+)\s", line)
        pr = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        host = m.group(1)
        if host.startswith("127.") or host in ("[::1]", "::1"):
            continue
        ports.setdefault(pr.group(1) if pr else "?", set()).add(m.group(2))
    for proc, pl in sorted(ports.items()):
        if proc in c.skip_listeners:
            continue
        add("lsn:" + proc, "service", proc, sub="ports " + ",".join(sorted(pl, key=int)[:6]),
            icon="\U0001f50c", layer="pc", parent="group:pc.svc", status="up")

    for a in curated.PC_APPS:
        add("app:" + a["key"], "app", a["label"], sub=a["what"][:62], icon=a["icon"],
            layer="pc", parent="group:pc.apps", url=a.get("url"), meta={"what": a["what"]})
        link(PC_ID, "app:" + a["key"], "depends", "")


# ================================================================== server
def collect_nas():
    c = CFG.nas
    if not c.enabled:
        return
    group("group:nas", "%s — %s" % (c.label, c.host), "\U0001f5c4", "nas", sub=c.sub)
    for gid, lbl, ic in [("group:nas.pools", "ZFS pools", "\U0001f5c3"),
                         ("group:nas.vm", "Virtual machines", "\U0001f4bb"),
                         ("group:nas.apps", "Stopped apps", "\U0001f4a4")]:
        group(gid, lbl, ic, "nas", parent="group:nas")
    group("group:nas.docker", "Docker", "\U0001f433", "nas", parent="group:nas")
    for key, (lbl, ic) in curated.CONTAINER_GROUPS.items():
        group("group:docker." + key, lbl, ic, "nas", parent="group:nas.docker")

    ver = nas("cat /etc/version 2>/dev/null || true")
    nic = nas("ip -br addr | grep %s | awk '{print $1}'" % shlex.quote(c.host), sudo=False)
    up = nas("uptime -p", sudo=False)
    add(NAS_ID, "host", c.label, sub=c.host, icon="\U0001f5c4", layer="nas", parent="group:nas",
        url="http://" + c.host, status="up" if ver or nic else "unknown",
        meta={"version": ver, "lan_nic": nic, "uptime": up})
    if not (ver or nic):
        problems.append("server unreachable over SSH — its section may be stale")

    for line in nas("zpool list -H -o name,size,alloc,free,health").splitlines():
        p = line.split("\t") if "\t" in line else line.split()
        if len(p) < 5:
            continue
        add("pool:" + p[0], "storage", p[0], sub="%s of %s used" % (p[2], p[1]), icon="\U0001f5c3",
            layer="nas", parent="group:nas.pools", status="up" if p[4] == "ONLINE" else "warn",
            meta={"size": p[1], "allocated": p[2], "free": p[3], "health": p[4]})
        link(NAS_ID, "pool:" + p[0], "data", "")

    collect_containers()
    if c.flavour == "truenas":
        collect_truenas_vms()


def collect_containers():
    host = CFG.nas.host
    pub_re = re.compile(r"(?:0\.0\.0\.0|%s):(\d+)(?:-(\d+))?->" % re.escape(host))
    for line in nas('docker ps -a --format "{{.Names}}|{{.Image}}|{{.Ports}}|{{.Status}}"').splitlines():
        p = line.split("|")
        if len(p) < 4:
            continue
        name, image, ports, status = p[0], p[1], p[2], p[3]
        # docker ps -a also returns one-shot init/permission/upgrade helpers that
        # exited at deploy time. They are noise on a map, so keep only what is
        # running plus anything explicitly described in the profile.
        if not status.startswith("Up") and name not in curated.CONTAINERS:
            continue
        gkey, icon, purpose, url = curated.CONTAINERS.get(name, (None, "\U0001f4e6", "", None))
        if gkey is None:
            # Park it somewhere sane and say so, rather than dropping it.
            fallback = CFG.nas.default_container_group
            gkey = fallback if fallback in curated.CONTAINER_GROUPS else next(iter(curated.CONTAINER_GROUPS), "dev")
            problems.append("container %s is not described in profile/curated.py" % name)
        pub = sorted(set(m[0] for m in pub_re.findall(ports)), key=int)
        add("ct:" + name, "container", _ct_label(name),
            sub=(", ".join("· :" + x for x in pub[:4]) if pub else image.split(":")[0][:34]),
            icon=icon, layer="nas", parent="group:docker." + gkey, url=url,
            status="up" if status.startswith("Up") else "down",
            meta={"image": image, "status": status, "published_ports": ", ".join(pub), "purpose": purpose})
        link(NAS_ID, "ct:" + name, "depends", "")
        watch["ct:" + name] = {"kind": "docker", "name": name}
        for lo_s, hi_s in pub_re.findall(ports):
            lo = int(lo_s); hi = int(hi_s) if hi_s else lo
            for pnum in range(lo, min(hi, lo + 32) + 1):
                port_owner.setdefault(pnum, "ct:" + name)


def collect_truenas_vms():
    for v in json.loads(nas("midclt call vm.query") or "[]"):
        st = (v.get("status") or {}).get("state", "")
        vcpu = (v.get("vcpus") or 1) * (v.get("cores") or 1) * (v.get("threads") or 1)
        ram = round((v.get("memory") or 0) / 1024)
        ip = curated.VM_IPS.get(v["name"], "")
        add("vm:nas:" + v["name"], "vm", v["name"],
            sub="%s · %s vCPU · %s GiB" % (ip or st.lower(), vcpu, ram),
            icon="\U0001f4bb", layer="nas", parent="group:nas.vm",
            status={"RUNNING": "up", "ERROR": "warn"}.get(st, "down"),
            meta={"state": st, "vcpus": str(vcpu), "memory": "%s GiB" % ram, "ip": ip,
                  "autostart": str(v.get("autostart")), "bootloader": v.get("bootloader", ""),
                  "description": v.get("description", ""), "vm_id": str(v.get("id"))})
        link(NAS_ID, "vm:nas:" + v["name"], "depends", "")

    try:
        for a in json.loads(nas("midclt call app.query")):
            if a.get("state") != "RUNNING":
                add("app:nas:" + a["name"], "app", a["name"], sub=a.get("state", "").lower(),
                    icon="\U0001f4a4", layer="nas", parent="group:nas.apps", status="down",
                    meta={"state": a.get("state")})
    except Exception:
        pass


def _ct_label(name):
    """ix-home-assistant-home-assistant-1 -> home-assistant, ix-nextcloud-redis-1 -> nextcloud-redis."""
    n = re.sub(r"^ix-", "", re.sub(r"-\d+$", "", name))
    half = len(n) // 2
    if len(n) % 2 == 1 and n[half] == "-" and n[:half] == n[half + 1:]:
        return n[:half]          # the app and its main service share a name
    return n


# ========================================================= Home Assistant
def collect_ha():
    c = CFG.homeassistant
    if not c.enabled:
        return
    group("group:ha", "Home Assistant", "\U0001f3e0", "ha", sub=c.url.split("//")[-1])
    try:
        cfg = json.loads(ha_get("/api/config"))
    except Exception:
        cfg = {}
        problems.append("Home Assistant did not answer — its section may be stale")
    add("ha:core", "service", "HA Core", sub=cfg.get("version", ""), icon="\U0001f3e0",
        layer="ha", parent="group:ha", url=c.url, status="up" if cfg else "unknown",
        meta={"version": cfg.get("version", ""), "location": cfg.get("location_name", ""),
              "tz": cfg.get("time_zone", "")})
    if c.container and ("ct:" + c.container) in _seen:
        link("ct:" + c.container, "ha:core", "depends", "runs")

    areas = {}
    for line in ha_template("{% for a in areas() %}{{a}}||{{area_name(a)}}||{{area_entities(a)|length}}\n{% endfor %}").splitlines():
        p = line.split("||")
        if len(p) == 3:
            areas[p[0]] = (p[1], p[2])
            add("area:" + p[0], "area", p[1], sub="%s entities" % p[2], icon="\U0001f4cd",
                layer="ha", parent="group:ha", meta={"entities": p[2]})
            watch["area:" + p[0]] = {"kind": "ha_area", "area": p[0]}
            link("ha:core", "area:" + p[0], "control", "")

    tpl = ('{% for a in areas() %}{% for d in area_devices(a) %}{{a}}||{{d}}||'
           '{{device_attr(d,"name_by_user") or device_attr(d,"name")}}||{{device_attr(d,"manufacturer")}}||'
           '{{device_attr(d,"model")}}||{{device_entities(d)|length}}||{{device_entities(d)|join(",")}}'
           '\n{% endfor %}{% endfor %}')
    ndev = 0
    for line in ha_template(tpl, timeout=60).splitlines():
        p = line.split("||")
        if len(p) < 6:
            continue
        area, did, name, manu, model, ecount = p[0], p[1], p[2], p[3], p[4], p[5]
        ents = [e for e in (p[6].split(",") if len(p) > 6 and p[6] else []) if "." in e]
        if area not in areas:
            continue
        add("dev:" + did, "device", name or "device", sub="%s · %s ent." % (manu or "?", ecount),
            icon=_dev_icon(manu, model, name), layer="ha", parent="area:" + area,
            meta={"manufacturer": manu, "model": model, "entities": ecount, "area": areas[area][0]})
        if ents:
            watch["dev:" + did] = {"kind": "ha", "entities": ents}
        ndev += 1
    if not ndev:
        problems.append("no Home Assistant devices returned — check the token")


def _dev_icon(manu, model, name):
    s = " ".join([manu or "", model or "", name or ""]).lower()
    for kw, ic in curated.DEVICE_ICONS:
        if kw in s:
            return ic
    return "\u25c9"


# ================================================================ tailnet
def collect_tailnet():
    if not CFG.tailscale.enabled:
        return
    group("group:tailnet", "Tailnet", "\U0001f517", "tailnet", sub="Tailscale mesh")
    try:
        st = json.loads(sh("tailscale status --json", timeout=20))
    except Exception:
        problems.append("tailscale status unavailable")
        return
    for p in list(st.get("Peer", {}).values()) + ([st["Self"]] if "Self" in st else []):
        host = (p.get("HostName") or p.get("DNSName", "").split(".")[0] or "node")
        last = p.get("LastSeen", "")
        add("ts:" + host, "device", host, sub=(p.get("TailscaleIPs") or [""])[0],
            icon=_os_icon(p.get("OS", "")), layer="tailnet", parent="group:tailnet",
            status="up" if p.get("Online", False) else "down",
            meta={"os": p.get("OS", ""), "online": str(p.get("Online", False)), "last_seen": last[:10],
                  "exit_node": str(p.get("ExitNodeOption", False))})


def _os_icon(os_):
    return {"linux": "\U0001f427", "windows": "\U0001fa9f", "iOS": "\U0001f4f1",
            "android": "\U0001f916", "tvOS": "\U0001f4fa", "macOS": "\U0001f34e"}.get(os_, "\U0001f5a5")


# ==================================================== public ingress routes
def collect_public():
    """Everything reachable from the open internet, and whether it still works."""
    c = CFG.public
    if not c.enabled or not curated.CLOUDFLARE_ROUTES:
        return
    origin = c.origin_host or CFG.nas.host
    group("group:public", c.label, "\U0001f9a9", "public", sub=c.sub)
    for hostname, scheme, port, note in curated.CLOUDFLARE_ROUTES:
        nid = "cf:" + hostname
        label = hostname.replace(c.strip_suffix, "") if c.strip_suffix else hostname
        add(nid, "route", label or hostname, sub="%s → :%d" % (scheme, port), icon="\U0001f310",
            layer="public", parent="group:public",
            url=None if scheme == "tcp" else "https://" + hostname,
            meta={"hostname": hostname, "origin": "%s://%s:%d" % (scheme, origin, port),
                  "serves": note, "tunnel": c.tunnel})
        # a route is only as good as the thing behind it
        watch[nid] = {"kind": "tcp", "host": origin, "port": port}
        for ct in ("ct:cloudflared-tunnel", "ct:cloudflared"):
            if ct in _seen:
                link(ct, nid, "net", "")
                break
        owner = port_owner.get(port)
        if owner:
            link(nid, owner, "net", "origin")


# ================================================================== assemble
def self_ip():
    """This machine's own LAN address.

    Probes run from wherever the server runs, not from here, so a probe aimed
    at this box must use a routable address. An early version probed
    127.0.0.1:22 and reported the workstation down forever.
    """
    return (sh("ip -4 -br addr show scope global | grep -v virbr | awk '{print $3}' | cut -d/ -f1").split() or [""])[0]


def main():
    group("group:net", "Network", "\U0001f310", "network", sub=CFG.site.lan_cidr)
    collect_pc()
    collect_nas()
    collect_ha()
    collect_tailnet()
    collect_public()

    for n in curated.EXTRA_NODES:
        add(**n)
    for a in curated.PC_APPS:
        for tgt, kind, lbl in a.get("links", []):
            if ("app:" + a["key"]) in _seen and tgt in _seen:
                link("app:" + a["key"], tgt, kind, lbl)
    for a, b, kind, label in curated.EDGES:
        if a in _seen and b in _seen:
            link(a, b, kind, label)

    # Machines the poller should TCP-probe. Pick a port that is always
    # listening on that host, not something incidental.
    me = None
    for p in CFG.probe:
        if p["node"] not in _seen:
            continue
        host = p["host"]
        if host == "@self":
            me = self_ip() if me is None else me
            host = me or p.get("fallback", "")
        if host:
            watch[p["node"]] = {"kind": "tcp", "host": host, "port": p["port"]}

    graph = dict(
        generated=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        site=CFG.site.name,
        layers=CFG.site.layers,
        ha_url=CFG.homeassistant.url if CFG.homeassistant.enabled else "",
        host=sh("hostname"),
        counts={"nodes": len(nodes), "edges": len(edges)},
        watch=watch,
        problems=problems,
        nodes=nodes,
        edges=edges,
    )
    out = os.path.join(HERE, "data", "graph.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(graph, f, indent=1, ensure_ascii=False)
    print("wrote %s — %d nodes, %d edges" % (out, len(nodes), len(edges)))
    for p in problems:
        print("  ! " + p)
    return graph


def deploy():
    c = CFG.nas
    if not c.deploy_path:
        print("no [nas] deploy_path in the profile — nothing to deploy to")
        return 1
    pw = c.get("password", "")
    prefix = ("sshpass -p %s " % shlex.quote(pw)) if pw else ""
    r = subprocess.run("%sscp %s %s/data/graph.json %s@%s:%s/data/graph.json"
                       % (prefix, c.ssh_opts, HERE, c.user, c.host, c.deploy_path), shell=True)
    print("deployed" if r.returncode == 0 else "DEPLOY FAILED")
    return r.returncode


if __name__ == "__main__":
    g = main()
    if "--print" in sys.argv:
        json.dump(g, sys.stdout, indent=1, ensure_ascii=False)
    if "--deploy" in sys.argv:
        raise SystemExit(deploy())
