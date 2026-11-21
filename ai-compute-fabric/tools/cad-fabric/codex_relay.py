"""Ask Codex on the Windows PC (where it is logged in and works) through ssh. Credentials only from the environment:
CODEX_RELAY_HOST (user@host), SSHPASS (read by sshpass -e) or a working key. Nothing is stored."""
import os, subprocess, tempfile, uuid

HOST = os.environ.get("CODEX_RELAY_HOST", "FiosSV@192.168.1.33")
PY = os.environ.get("CODEX_RELAY_PY", r"C:\Users\FiosSV\AppData\Local\Python\bin\python.exe")
APP = os.environ.get("CODEX_RELAY_APP", r"C:\Users\FiosSV\AppData\Local\AICF-CAD\app")
TMP = os.environ.get("CODEX_RELAY_TMP", r"C:\Users\FiosSV\pkgtest")
_SSH = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15"]


def _pre():
    return ["sshpass", "-e"] if os.environ.get("SSHPASS") else []


def ask(prompt, timeout=300):
    """-> (text, err)"""
    name = "q-%s.txt" % uuid.uuid4().hex[:8]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as f:
        f.write(prompt); local = f.name
    try:
        r = subprocess.run(_pre() + ["scp", *_SSH, local, "%s:/%s/%s" % (HOST, TMP.replace("\\", "/"), name)], capture_output=True, text=True, timeout=60)
        if r.returncode:
            return "", "relay copy failed: " + (r.stderr.strip().splitlines() or [""])[-1][:120]
        remote = TMP + "\\" + name
        r = subprocess.run(_pre() + ["ssh", *_SSH, HOST, '"%s" "%s\\codex_advisor.py" @%s' % (PY, APP, remote) + ' & del "%s"' % remote],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", "codex relay timed out after %ds" % timeout
    finally:
        os.unlink(local)
    out = (r.stdout or "").strip()
    if r.returncode or not out or out.startswith("ERROR:"):
        return "", out[:200] if out.startswith("ERROR:") else "relay failed (exit %d: %s)" % (r.returncode, ((r.stderr or "").strip().splitlines() or [""])[-1][:120])
    return out, ""
