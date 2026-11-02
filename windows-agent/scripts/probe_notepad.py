import sys, time
sys.path.insert(0, r"C:\wagent\src")
from wagent.skills.uia import UIA, find_by_name
u = UIA(); print(u.open_app("notepad")); time.sleep(1)
u.observe()
k = lambda: {i: (e.ControlTypeName, e.Name) for i, e in u.ids.items()}
u.click(find_by_name(k(), "File")); time.sleep(1)
print(u.observe()[:200]); u.observe()
u.click(find_by_name(k(), "Save As...")); time.sleep(2.5)
print("=== sau Save As: foreground & top-level windows")
a = u.auto
fg = a.GetForegroundControl()
print("FG:", fg.ControlTypeName, repr(fg.Name), fg.ProcessId, "| top:", repr(fg.GetTopLevelControl().Name))
for w in a.GetRootControl().GetChildren():
    if w.ControlTypeName == "WindowControl" and w.Name: print(" TOP", repr(w.Name), w.ProcessId, w.NativeWindowHandle)
print(u.observe())
