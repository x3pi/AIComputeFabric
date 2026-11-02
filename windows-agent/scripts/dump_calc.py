import sys, time
sys.path.insert(0, r"C:\wagent\src")
from wagent.skills.uia import UIA
u = UIA(); print(u.open_app("calc")); time.sleep(2)
out = u.observe()
open(r"C:\wagent\calc_dump.txt", "w", encoding="utf-8").write(out)
u.close_window()
