"""Skill UI Automation (chi chay duoc tren Windows). Uu tien pattern, khong dung SendKeys."""
import os, subprocess, time

# Cua so agent KHONG duoc thay/dong: console/terminal chay chinh no (agent tu dong cua so me => tu giet minh).
PROTECTED_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "PseudoConsoleWindow"}

def is_protected(class_name, pid, own_pids):
    return class_name in PROTECTED_CLASSES or pid in own_pids

def own_pids():
    pids = {os.getpid()}
    if hasattr(os, "getppid"):
        pids.add(os.getppid())
    return pids

# whitelist app cho phep mo
APPS = {"notepad": "notepad.exe", "calc": "calc.exe", "mspaint": "mspaint.exe"}

def click_hint(kinds, id):
    """kinds: {id: (ControlTypeName, Name)}. Phan tu Text khong bam duoc; thong thuong nut that dung ngay truoc nhan Text cua no."""
    msg = "id %d la [Text] %r, khong bam duoc." % (id, kinds[id][1])
    for j in range(id - 1, max(id - 3, 0), -1):
        if j in kinds and kinds[j][0] == "ButtonControl":
            return msg + " Co le ban muon nut id %d [Button] %r (nut that thuong dung ngay truoc nhan Text cua no)." % (j, kinds[j][1])
    return msg + " Hay chon id cua phan tu [Button]."

def find_by_name(kinds, name):
    """Tim id cua phan tu bam duoc (khong phai Text) co ten khop chinh xac (khong phan biet hoa/thuong). Loi ro neu khong co/mo ho."""
    if not kinds:
        raise ValueError("chua co danh sach phan tu: goi observe truoc")
    want = str(name).strip().lower()
    hit = [i for i, (t, n) in sorted(kinds.items()) if t != "TextControl" and str(n).strip().lower() == want]
    if len(hit) == 1:
        return hit[0]
    if len(hit) > 1:
        raise ValueError("ten %r khop nhieu phan tu (id %s): dung id" % (name, hit))
    near = [n for t, n in kinds.values() if t != "TextControl" and want and want in str(n).lower()][:5]
    raise ValueError("khong co phan tu bam duoc ten %r%s" % (name, ("; gan giong: " + ", ".join(map(repr, near))) if near else ""))

class UIA:
    def __init__(self):
        import uiautomation as auto
        self.auto, self.ids, self.win = auto, {}, None
        self.friendly = False   # True: bao loi ro khi bam nham phan tu Text (goi y nut ngay truoc)

    def open_app(self, name, **_):
        exe = APPS.get(str(name).lower())
        if not exe:
            raise ValueError("app khong nam trong whitelist: %s (cho phep: %s)" % (name, ", ".join(APPS)))
        subprocess.Popen([exe])
        time.sleep(2)
        return "da mo " + exe

    def observe(self, max_items=150):
        """Danh sach phan tu cua cua so tren cung, danh so id."""
        a = self.auto
        self.ids, lines = {}, []
        win = a.GetForegroundControl().GetTopLevelControl() if a.GetForegroundControl() else None
        if win is None or (win.ControlTypeName == "PaneControl" and not win.Name) or win.Name == "Program Manager" or self._protected(win):
            wins = [w for w in a.GetRootControl().GetChildren()
                    if w.ControlTypeName == "WindowControl" and w.Name and w.Name != "Program Manager" and not self._protected(w)]
            win = wins[0] if wins else None
        if win is None:
            return "(khong co cua so)"
        self.win = win
        lines.append("WINDOW %r" % win.Name)
        def walk(c, d):
            if len(self.ids) >= max_items or d > 6:
                return
            if c.ControlTypeName in ("EditControl", "ButtonControl", "MenuItemControl", "TextControl",
                                     "CheckBoxControl", "ComboBoxControl", "ListItemControl", "TabItemControl"):
                i = len(self.ids) + 1
                self.ids[i] = c
                lines.append("%d [%s] %r" % (i, c.ControlTypeName.replace("Control", ""), c.Name))
            for k in c.GetChildren():
                walk(k, d + 1)
        walk(win, 0)
        return "\n".join(lines)

    def describe(self, id):
        try:
            c = self._el(id)
            return "%s %s" % (c.Name, c.AutomationId or "")
        except Exception:
            return ""

    def find_by_name(self, name):
        return find_by_name({i: (e.ControlTypeName, e.Name) for i, e in self.ids.items()}, name)

    def _el(self, id):
        if int(id) not in self.ids:
            raise ValueError("id %s khong ton tai (goi observe lai)" % id)
        return self.ids[int(id)]

    def set_text(self, id, text, **_):
        self._el(id).GetValuePattern().SetValue(text)
        return "da dat van ban"

    def read_text(self, id, **_):
        c = self._el(id)
        try:
            return c.GetValuePattern().Value
        except Exception:
            return c.Name

    def click(self, id, **_):
        c = self._el(id)
        if self.friendly and c.ControlTypeName == "TextControl":
            raise ValueError(click_hint({i: (e.ControlTypeName, e.Name) for i, e in self.ids.items()}, int(id)))
        try:
            c.GetInvokePattern().Invoke()
        except Exception:
            c.GetLegacyIAccessiblePattern().DoDefaultAction()
        return "da bam"

    def _protected(self, w):
        try:
            return is_protected(w.ClassName, w.ProcessId, own_pids())
        except Exception:
            return True   # khong xac dinh duoc => coi nhu duoc bao ve

    def close_window(self, **_):
        if self.win is None or self._protected(self.win):
            raise ValueError("khong duoc dong cua so nay (cua so cua chinh agent/terminal)")
        self.win.GetWindowPattern().Close()
        return "da dong cua so"
