#!/usr/bin/env python3
"""Installation self-check for the CAD agent. Prints one PASS/WARN/FAIL line per check and exits non-zero on any FAIL.

    python doctor.py [--chat]      --chat also sends one tiny chat request (uses a few tokens of the tenant quota)

FAIL = the agent cannot work; WARN = works, but something you should know (e.g. SolidWorks not running yet)."""
import json, os, subprocess, sys, tempfile, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import aicfconf

rows = []


def rec(level, name, detail=""):
    rows.append((level, name, detail))
    print("%-5s %-26s %s" % (level, name, detail))


def http(url, key, body=None, timeout=20):
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None,
                                 {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def main():
    try:
        cfg = aicfconf.load()
    except Exception as e:
        rec("FAIL", "config file", "unreadable: %s" % e)
        return 1
    rec("PASS" if cfg.get("_path") else "WARN", "config file", cfg.get("_path") or "none found, using defaults")

    try:
        key = aicfconf.read_key(cfg)
        rec("PASS", "api key file", aicfconf.key_file(cfg))
    except SystemExit as e:
        rec("FAIL", "api key file", str(e))
        key = ""

    chat_url = aicfconf.url(cfg)
    base = chat_url.split("/chat/completions")[0]
    if key:
        code, js = http(base + "/models", key)
        names = [m.get("id") for m in js.get("data", [])] if isinstance(js, dict) else []
        if code == 200:
            want = aicfconf.model(cfg)
            rec("PASS" if want in names else "WARN", "gateway /models", "reachable; models=%s%s" % (names[:3], "" if want in names else " (configured model %r not listed)" % want))
        elif code == 401:
            rec("FAIL", "gateway /models", "HTTP 401: the API key is wrong or revoked")
        else:
            rec("FAIL", "gateway /models", "%s unreachable (%s)" % (base, js.get("error", "HTTP %s" % code)))
        if "--chat" in sys.argv and code == 200:
            code, js = http(chat_url, key, {"model": aicfconf.model(cfg), "max_tokens": 16, "temperature": 0,
                                            "chat_template_kwargs": {"enable_thinking": False},
                                            "messages": [{"role": "user", "content": "Reply with the single word OK."}]}, timeout=120)
            ok = code == 200 and js.get("choices")
            rec("PASS" if ok else "FAIL", "gateway chat", "reply=%r" % js["choices"][0]["message"]["content"][:30] if ok else "HTTP %s" % code)

    try:
        exe = aicfconf.mcp_exe(cfg)
        rec("PASS", "solidworks-mcp", exe)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "sw_agent.py"), "--list-tools"], capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
        n = sum(1 for ln in r.stdout.splitlines() if ln.strip())
        rec("PASS" if r.returncode == 0 and n else "FAIL", "mcp tool list", "%d lines" % n if r.returncode == 0 else (r.stderr or r.stdout)[-200:])
    except SystemExit as e:
        rec("FAIL", "solidworks-mcp", str(e))

    sw = aicfconf.sw_exe(cfg)
    rec("PASS" if sw else "WARN", "SolidWorks installed", sw or "SLDWORKS.exe not found (set sw_exe in config.json if it is installed elsewhere)")
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/fi", "imagename eq SLDWORKS.exe"], capture_output=True, text=True).stdout
        rec("PASS" if "SLDWORKS.exe" in out else "WARN", "SolidWorks running", "yes" if "SLDWORKS.exe" in out else "not running: start it and wait until the window is usable before a job")

    d = aicfconf.data_dir(cfg)
    try:
        with tempfile.NamedTemporaryFile(dir=d):
            pass
        rec("PASS", "data folder writable", d)
    except OSError as e:
        rec("FAIL", "data folder writable", "%s: %s" % (d, e))

    fails = sum(1 for r in rows if r[0] == "FAIL")
    print("\n%d FAIL, %d WARN, %d PASS" % (fails, sum(1 for r in rows if r[0] == "WARN"), sum(1 for r in rows if r[0] == "PASS")))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
