"""GD0: mo Notepad, doc cay UIA, ghi ket qua ra file. Phai chay trong phien desktop."""
import subprocess, time, sys, os
import uiautomation as auto
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from wagent.observe import dump
out = r"C:\wagent\phase0_result.txt"
p = subprocess.Popen(["notepad.exe"])
time.sleep(2)
w = auto.WindowControl(searchDepth=1, ClassName="Notepad")
res = ["exists=%s" % w.Exists(5)]
if w.Exists(0):
    res += dump(w)
    e = w.EditControl()
    try:
        e.GetValuePattern().SetValue("hello from wagent")
        res.append("setvalue=ok")
    except Exception as ex:
        res.append("setvalue=FAIL %r" % ex)
    time.sleep(0.5)
    res.append("edit_text_after=" + repr(w.EditControl().GetValuePattern().Value if w.EditControl().Exists(0) else None))
    p.terminate()
open(out, "w", encoding="utf-8").write("\n".join(res))
