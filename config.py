"""Load the site profile: what this particular homelab is made of.

Nothing about any one house lives in the engine. It all lives in a profile
directory that git never sees:

    profile/config.toml   hosts, credentials, what to scan
    profile/curated.py    what each thing is *for* — the authored layer

`profile.example/` is the shipped copy of both, with a small fictional lab in
it. Copy it to `profile/` and edit.

    from config import CFG, CURATED
"""
import os, sys, tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.environ.get("NETMAP_PROFILE") or os.path.join(HERE, "profile")
EXAMPLE = os.path.join(HERE, "profile.example")


class ConfigError(Exception):
    pass


def _die(msg):
    raise ConfigError(msg)


# --------------------------------------------------------------- defaults
# Every key the engine reads has a default here, so a short config.toml is a
# valid one and a new key never breaks an existing profile.
DEFAULTS = {
    "site": {"name": "Homelab", "lan_cidr": "", "layers": {}},
    "pc": {"enabled": True, "label": "This PC", "node_id": "host:pc", "sub": "", "host_label": "",
           "services": [], "skip_listeners": ["?", "systemd-resolve"]},
    "nas": {"enabled": False, "label": "Server", "node_id": "host:nas", "sub": "",
            "host": "", "user": "", "password_file": "", "password_env": "NETMAP_NAS_PASS",
            "ssh_opts": "-o StrictHostKeyChecking=no -o ConnectTimeout=10",
            "sudo": True, "deploy_path": "", "flavour": "truenas",
            "default_container_group": "dev"},
    "homeassistant": {"enabled": False, "url": "", "token_file": "", "container": ""},
    "tailscale": {"enabled": False},
    "public": {"enabled": False, "label": "Public", "sub": "", "tunnel": "",
               "origin_host": "", "strip_suffix": ""},
    "probe": [],
}

# Shown in the layer bar. Overridable per site under [site.layers].
LAYER_NAMES = {"pc": "PC", "nas": "Server", "ha": "Home Assistant",
               "tailnet": "Tailnet", "network": "Network", "public": "Public"}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _expand(p):
    return os.path.expanduser(os.path.expandvars(p)) if p else ""


class Section(dict):
    """A config table reachable as cfg.nas.host as well as cfg["nas"]["host"]."""
    def __getattr__(self, k):
        try:
            v = self[k]
        except KeyError:
            raise AttributeError(k)
        return Section(v) if isinstance(v, dict) else v


def load_config(path=None):
    path = path or os.path.join(PROFILE, "config.toml")
    if not os.path.exists(path):
        _die("no profile found at %s\n"
             "    cp -r %s %s   then edit profile/config.toml"
             % (path, os.path.relpath(EXAMPLE, os.getcwd()), os.path.relpath(PROFILE, os.getcwd())))
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cfg = Section(_merge(DEFAULTS, raw))
    cfg["site"]["layers"] = _merge(LAYER_NAMES, cfg["site"].get("layers"))
    cfg["_profile_dir"] = PROFILE

    # Credentials never live in the config file itself. It only says where to
    # find them, so a profile is safe to copy around or back up in the clear.
    cfg["nas"]["password"] = _read_secret(cfg["nas"]["password_file"], cfg["nas"]["password_env"])
    cfg["homeassistant"]["token"] = _read_secret(cfg["homeassistant"]["token_file"], "NETMAP_HA_TOKEN")

    if cfg["nas"]["enabled"] and not cfg["nas"]["host"]:
        _die("[nas] enabled but no host set in %s" % path)
    if cfg["homeassistant"]["enabled"] and not cfg["homeassistant"]["url"]:
        _die("[homeassistant] enabled but no url set in %s" % path)
    return cfg


def _read_secret(file_path, env_name):
    """Environment first — it beats a file so a one-off run can override."""
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    p = _expand(file_path)
    if p and os.path.exists(p):
        return open(p).read().strip()
    return ""


def load_curated(cfg=None):
    """Import profile/curated.py, filling in any table the profile leaves out."""
    import importlib.util
    path = os.path.join(PROFILE, "curated.py")
    if not os.path.exists(path):
        _die("no profile/curated.py — copy it from %s" % os.path.relpath(EXAMPLE, os.getcwd()))
    spec = importlib.util.spec_from_file_location("netmap_curated", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, default in (("CONTAINERS", {}), ("CONTAINER_GROUPS", {}), ("PC_APPS", []),
                          ("CLOUDFLARE_ROUTES", []), ("VM_IPS", {}), ("EDGES", []),
                          ("EXTRA_NODES", []), ("TRAPS", {}), ("NOTES", {}),
                          ("DEVICE_ICONS", []), ("PC_GROUPS", [])):
        if not hasattr(mod, name):
            setattr(mod, name, default)
    return mod


try:
    CFG = load_config()
    CURATED = load_curated(CFG)
except ConfigError as exc:                                  # pragma: no cover
    sys.stderr.write("netmap: %s\n" % exc)
    raise SystemExit(2)
