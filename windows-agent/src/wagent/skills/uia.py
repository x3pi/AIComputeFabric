"""Skill UI Automation (chi chay duoc tren Windows). Uu tien pattern, khong dung SendKeys."""
import subprocess, time

# whitelist app cho phep mo
APPS = {"notepad": "notepad.exe", "calc": "calc.exe", "mspaint": "mspaint.exe"}

class UIA:
    def __init__(self):
        import uiautomation as auto
        self.auto, self.ids, self.win = auto, {}, None

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
        if win is None or win.ControlTypeName == "PaneControl" and not win.Name:
            wins = [w for w in a.GetRootControl().GetChildren() if w.ControlTypeName == "WindowControl" and w.Name]
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
        try:
            c.GetInvokePattern().Invoke()
        except Exception:
            c.GetLegacyIAccessiblePattern().DoDefaultAction()
        return "da bam"

    def close_window(self, **_):
        self.win.GetWindowPattern().Close()
        return "da dong cua so"
