"""Doc cay UI Automation thanh van ban rut gon."""
import uiautomation as auto

def dump(ctrl, depth=0, max_depth=6, out=None, limit=400):
    out = [] if out is None else out
    if len(out) >= limit or depth > max_depth:
        return out
    out.append("%s[%s] %r" % ("  " * depth, ctrl.ControlTypeName, ctrl.Name))
    for c in ctrl.GetChildren():
        dump(c, depth + 1, max_depth, out, limit)
    return out

def foreground_tree():
    return "\n".join(dump(auto.GetForegroundControl().GetTopLevelControl()))
