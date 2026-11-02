"""Chay goose (headless, khong console) voi cau hinh co lap. usage: pythonw goose_launch.py <rid> <request_file> [max_turns] [model] [host]
Log: C:\\ufo_test\\run\\goose_<rid>.log, .done. Cau hinh goose nam trong C:\\goose_test\\appdata (khong dung cau hinh nguoi dung)."""
import os, subprocess, sys, time
rid, reqfile = sys.argv[1], sys.argv[2]
turns = sys.argv[3] if len(sys.argv) > 3 else "25"
model = sys.argv[4] if len(sys.argv) > 4 else "qwen3-coder"
host = sys.argv[5] if len(sys.argv) > 5 else "http://10.42.0.1:18090"
req = open(reqfile, encoding="utf-8").read().strip()
env = dict(os.environ, APPDATA=r"C:\goose_test\appdata", LOCALAPPDATA=r"C:\goose_test\localappdata", USERPROFILE=r"C:\goose_test\home",
           GOOSE_PROVIDER="openai", GOOSE_MODEL=model, OPENAI_HOST=host, OPENAI_BASE_PATH="v1/chat/completions", OPENAI_API_KEY="local-none",
           GOOSE_MODE="auto", GOOSE_DISABLE_KEYRING="1", GOOSE_TELEMETRY_ENABLED="false", PYTHONIOENCODING="utf-8")
for d in (r"C:\goose_test\appdata", r"C:\goose_test\localappdata", r"C:\goose_test\home", r"C:\goose_test\work"): os.makedirs(d, exist_ok=True)
cmd = [r"C:\goose_test\bin\goose-package\goose.exe", "run", "--no-session", "--with-builtin", (open(r"C:\goose_test\ext.txt").read().strip() if os.path.exists(r"C:\goose_test\ext.txt") else "developer"), "--max-turns", turns]
if os.path.exists(r"C:\goose_test\ext_stdio.txt"):
    cmd += ["--with-extension", open(r"C:\goose_test\ext_stdio.txt").read().strip()]
cmd += ["-t", req]
t0 = time.time()
with open(r"C:\ufo_test\run\goose_%s.log" % rid, "wb") as log:
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=r"C:\goose_test\work", creationflags=0x08000000)
    try: rc = p.wait(timeout=int(os.environ.get("GOOSE_TIMEOUT", "600")))
    except subprocess.TimeoutExpired:
        p.kill(); rc = "timeout"
open(r"C:\ufo_test\run\goose_%s.done" % rid, "w").write("%s %.0f" % (rc, time.time() - t0))
