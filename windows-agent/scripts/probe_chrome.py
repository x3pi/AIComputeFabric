import sys, time, subprocess, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\wagent\src")
import uiautomation as auto
from wagent.skills.uia import top_windows
with auto.UIAutomationInitializerInThread():
    before = {h for h, _, _ in top_windows()}
    subprocess.Popen([r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--force-renderer-accessibility", "--new-window", "https://vnexpress.net"])
    new = []
    for _ in range(40):
        time.sleep(0.5)
        new = [w for w in top_windows() if w[0] not in before]
        if new: break
    print("cua so moi:", new)
    time.sleep(8)                                   # cho trang tai
    main = [w for w in new if w[2].endswith("Google Chrome")][0]
    win = auto.ControlFromHandle(main[0])
    print("window:", win.Name, win.ClassName)
    kinds = collections.Counter(); links = []; docs = []
    def walk(c, d):
        if d > 40 or len(links) > 400: return
        t = c.ControlTypeName
        kinds[t] += 1
        if t == "DocumentControl": docs.append(c.Name)
        if t == "HyperlinkControl" and len((c.Name or "").strip()) > 25: links.append(c.Name.strip())
        for k in c.GetChildren(): walk(k, d + 1)
    walk(win, 0)
    print("loai:", dict(kinds.most_common(8)))
    print("document:", docs[:3])
    print("so link dai (>25 ky tu):", len(links))
    for l in links[:12]: print(" -", l[:110])
    for w in new:                        # dong cac cua so do script mo
        try: auto.ControlFromHandle(w[0]).GetWindowPattern().Close()
        except Exception: pass
