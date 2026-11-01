"""Guard: whitelist tool + phan loai rui ro + duyet. Ket qua: ('allow'|'approve'|'deny', ly do)."""
import re

LOW = {"observe", "read_text", "read_file", "list_dir", "done", "open_app"}
MEDIUM = {"set_text", "write_file", "close_window"}      # cho phep, ghi audit
CLICK = {"click"}
# click vao phan tu co ten khop => can nguoi duyet (xoa, gui, thanh toan, cai dat, mat khau...)
RISKY_NAME = re.compile(r"delete|remove|erase|format|uninstall|install|send|submit|pay|purchase|buy|"
                        r"password|sign ?in|log ?in|run as|admin|xo[aá]|g[uử]i|thanh to[aá]n|mua|c[aà]i [dđ][aặ]t|m[aậ]t kh[aẩ]u|"
                        r"overwrite|replace|don.t save|empty", re.I)
RISKY_TEXT = re.compile(r"password|passwd|m[aậ]t kh[aẩ]u|api[_ -]?key|token|secret", re.I)

def classify(action, describe=None, exists=None):
    t, a = action["tool"], action.get("args", {})
    if t in LOW:
        return "allow", ""
    if t == "write_file":
        if exists and exists(a.get("path", "")):
            return "approve", "ghi de file da ton tai: %s" % a.get("path")
        return "allow", ""
    if t in MEDIUM:
        if t == "set_text" and RISKY_TEXT.search(str(a.get("text", ""))):
            return "approve", "van ban co ve chua bi mat/mat khau"
        return "allow", ""
    if t in CLICK:
        name = describe(a.get("id")) if describe else ""
        if RISKY_NAME.search(name or ""):
            return "approve", "bam vao phan tu rui ro: %r" % name
        return "allow", ""
    return "deny", "tool khong co trong whitelist: " + t

# giu API cu cho test GD1
def check(action):
    d, why = classify(action, describe=lambda i: "")
    return (d != "deny"), why
