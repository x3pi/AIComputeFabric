"""Chay UFO2 voi yeu cau doc tu file utf-8 (tranh loi quote khi di qua ssh/schtasks). Windows, python cua venv UFO."""
import os, subprocess, sys
rid, reqfile = sys.argv[1], sys.argv[2]
req = open(reqfile, encoding="utf-8").read().strip()
os.chdir(r"C:\ufo_test\src\UFO-main")
env = dict(os.environ, PYTHONIOENCODING="utf-8")
with open(r"C:\ufo_test\run\ufo_%s.log" % rid, "wb") as log:
    p = subprocess.Popen([r"C:\ufo_test\venv\Scripts\python.exe", "-m", "ufo", "--task", "cmp_" + rid, "--mode", "normal", "--request", req],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env)
    open(r"C:\ufo_test\run\ufo_%s.pid" % rid, "w").write(str(p.pid))
    rc = p.wait()
open(r"C:\ufo_test\run\ufo_%s.done" % rid, "w").write(str(rc))
