"""Shared configuration for the CAD agent: one place for the gateway URL, key, tool paths and the data folder, so the same
files install on any Windows PC. Nothing machine specific is hard coded in the scripts.

Lookup order for the config file (first that exists):
  1. $AICF_CAD_CONFIG
  2. <install dir>/config.json            (install dir = folder holding these scripts)
  3. %ProgramData%\\AICF-CAD\\config.json
Environment overrides (win over the file): AICF_URL, AICF_MODEL, AICF_KEY_FILE, SW_MCP_EXE, SW_EXE, AICF_DATA_DIR.

config.json keys: url, model, key_file (path; relative paths are relative to the install dir), mcp_exe, sw_exe, data_dir.
Legacy: if no key_file is configured, <install dir>/key.txt is used.
"""
import json, os, shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {"url": "http://127.0.0.1:18080/v1/chat/completions", "model": "Qwen3.8-27B-UD-Q4_K_M.gguf"}


def _config_path():
    for p in (os.environ.get("AICF_CAD_CONFIG"), os.path.join(ROOT, "config.json"),
              os.path.join(os.environ.get("PROGRAMDATA", ""), "AICF-CAD", "config.json") if os.environ.get("PROGRAMDATA") else None):
        if p and os.path.isfile(p):
            return p
    return None


def load():
    cfg = dict(DEFAULTS)
    path = _config_path()
    if path:
        with open(path, encoding="utf-8-sig") as f:      # utf-8-sig: PowerShell 5 writes a BOM
            cfg.update(json.load(f))
    cfg["_path"] = path
    return cfg


def _abs(p, base):
    p = os.path.expandvars(os.path.expanduser(p))
    return p if os.path.isabs(p) else os.path.join(base, p)


def key_file(cfg=None):
    cfg = cfg or load()
    p = os.environ.get("AICF_KEY_FILE") or cfg.get("key_file")
    return _abs(p, ROOT) if p else os.path.join(ROOT, "key.txt")


def read_key(cfg=None):
    p = key_file(cfg)
    try:
        with open(p, encoding="utf-8-sig") as f:
            k = f.read().strip()
    except OSError as e:
        raise SystemExit("API key file not readable (%s): %s. Run install.ps1 or set key_file in config.json." % (p, e))
    if not k:
        raise SystemExit("API key file is empty: %s" % p)
    return k


def url(cfg=None):
    return os.environ.get("AICF_URL") or (cfg or load())["url"]


def model(cfg=None):
    return os.environ.get("AICF_MODEL") or (cfg or load())["model"]


def mcp_exe(cfg=None):
    """solidworks-mcp executable: env, config, PATH, then the usual uv/pipx locations."""
    cfg = cfg or load()
    cands = [os.environ.get("SW_MCP_EXE"), cfg.get("mcp_exe"), shutil.which("solidworks-mcp"),
             os.path.expanduser(r"~\.local\bin\solidworks-mcp.exe"),
             os.path.join(os.environ.get("APPDATA", ""), "uv", "tools", "solidworks-mcp", "Scripts", "solidworks-mcp.exe")]
    for c in cands:
        if c and os.path.isfile(os.path.expandvars(c)):
            return os.path.expandvars(c)
    raise SystemExit("solidworks-mcp not found. Run install.ps1 (or set mcp_exe in config.json / SW_MCP_EXE).")


def sw_exe(cfg=None):
    """SLDWORKS.exe: env, config, registry (any installed version), then the default folder."""
    cfg = cfg or load()
    cands = [os.environ.get("SW_EXE"), cfg.get("sw_exe")]
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE,):
            for sub in (r"SOFTWARE\SolidWorks", r"SOFTWARE\WOW6432Node\SolidWorks"):
                try:
                    with winreg.OpenKey(root, sub) as k:
                        for i in range(winreg.QueryInfoKey(k)[0]):
                            name = winreg.EnumKey(k, i)
                            try:
                                with winreg.OpenKey(k, name + r"\Setup") as s:
                                    d = winreg.QueryValueEx(s, "SolidWorks Folder")[0]
                                    cands.append(os.path.join(d, "SLDWORKS.exe"))
                            except OSError:
                                pass
                except OSError:
                    pass
    except ImportError:
        pass                                              # not Windows
    cands.append(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe")
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def data_dir(cfg=None):
    """Folder for job outputs and audit logs (always created, user writable)."""
    cfg = cfg or load()
    p = os.environ.get("AICF_DATA_DIR") or cfg.get("data_dir") or os.path.join(os.environ.get("LOCALAPPDATA") or ROOT, "AICF-CAD")
    p = _abs(p, ROOT)
    os.makedirs(p, exist_ok=True)
    return p
