"""One place for settings. Nothing machine specific is hard coded and no secret lives in a file this package writes.

Config file (first that exists): $CADFABRIC_CONFIG, ./cadfabric.json, <user config dir>/cadfabric/config.json
  Linux/Mac: ~/.config/cadfabric/config.json     Windows: %APPDATA%\\cadfabric\\config.json
Keys: url, model, key_file (path to a file holding the API key), planner, relay {host, codex}, codex_bin, data_dir
Environment overrides: CADFABRIC_URL, CADFABRIC_MODEL, CADFABRIC_KEY (the key itself), CADFABRIC_KEY_FILE, CADFABRIC_PLANNER, CADFABRIC_DATA_DIR."""
import json, os, sys

DEFAULTS = {"url": "http://127.0.0.1:18080/v1/chat/completions", "model": "", "key_file": None, "planner": "nothink",
            "relay": {"host": "", "codex": "codex"}, "codex_bin": None, "data_dir": None}
PLANNERS = ("off", "nothink", "think", "codex", "codex-relay")


def config_dir():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "cadfabric")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "cadfabric")


def config_path():
    for p in (os.environ.get("CADFABRIC_CONFIG"), os.path.join(os.getcwd(), "cadfabric.json"), os.path.join(config_dir(), "config.json")):
        if p and os.path.isfile(p):
            return p
    return None


def load():
    cfg = json.loads(json.dumps(DEFAULTS))
    path = config_path()
    if path:
        with open(path, encoding="utf-8-sig") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    for env, k in (("CADFABRIC_URL", "url"), ("CADFABRIC_MODEL", "model"), ("CADFABRIC_KEY_FILE", "key_file"), ("CADFABRIC_PLANNER", "planner"),
                   ("CADFABRIC_DATA_DIR", "data_dir")):
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    cfg["_path"] = path
    if cfg["planner"] not in PLANNERS:
        raise SystemExit("config: planner must be one of %s (got %r)" % (", ".join(PLANNERS), cfg["planner"]))
    return cfg


def read_key(cfg):
    """The API key from $CADFABRIC_KEY or the key file. Empty string is allowed (a local gateway without auth)."""
    if os.environ.get("CADFABRIC_KEY"):
        return os.environ["CADFABRIC_KEY"].strip()
    p = cfg.get("key_file")
    if not p:
        return ""
    try:
        with open(os.path.expanduser(os.path.expandvars(p)), encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError as e:
        raise SystemExit("API key file not readable (%s): %s" % (p, e))


def data_dir(cfg):
    d = cfg.get("data_dir") or os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share") if sys.platform != "win32"
                                           else os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "cadfabric")
    os.makedirs(d, exist_ok=True)
    return d
