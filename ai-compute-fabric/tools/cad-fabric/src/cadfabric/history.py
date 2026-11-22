"""Saved design sessions: one JSON file per conversation in <data dir>/history, so a part can be reopened and edited later.
A record holds the chat events, the model's message history, the CAD cells (replaying them rebuilds the part) and the last measurement.
Files are private to the user (0600), written atomically, pruned to MAX_SESSIONS. Ids are validated before they touch a path."""
import json, os, re, secrets, time

ID = re.compile(r"^\d{8}-\d{6}-[a-z0-9]{4}$")
MAX_SESSIONS = 100
SKIP_EVENTS = ("phase",)                      # progress ticks are not worth keeping


def valid(sid):
    return isinstance(sid, str) and bool(ID.match(sid))


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _dir(data_dir):
    d = os.path.join(data_dir, "history")
    os.makedirs(d, exist_ok=True)
    return d


def _path(data_dir, sid):
    if not valid(sid):
        raise ValueError("bad session id")
    return os.path.join(_dir(data_dir), sid + ".json")


def save(data_dir, rec):
    """Write one session atomically, then prune the oldest ones."""
    rec = dict(rec, v=1, events=[e for e in rec.get("events", []) if e.get("kind") not in SKIP_EVENTS], updated=time.strftime("%Y-%m-%d %H:%M:%S"))
    p = _path(data_dir, rec["id"])
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, p)
    files = sorted(f for f in os.listdir(_dir(data_dir)) if f.endswith(".json") and ID.match(f[:-5]))
    for old in files[:-MAX_SESSIONS]:
        try:
            os.unlink(os.path.join(_dir(data_dir), old))
        except OSError:
            pass


def load(data_dir, sid):
    try:
        with open(_path(data_dir, sid), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("id") == sid and isinstance(rec.get("events"), list) else None


def norm(ws):
    """Project key stored in a record: the real path of the folder ('' = no project)."""
    return os.path.realpath(ws) if ws else ""


def list_sessions(data_dir, workspace=None):
    """-> newest first: [{id, title, created, updated, volume, steps, workspace}]. `workspace` given: only that project's conversations
    (older records without a project belong to '' = no project). None: all of them. Unreadable files are skipped."""
    want = None if workspace is None else norm(workspace)
    out = []
    d = _dir(data_dir)
    for f in os.listdir(d):
        if not (f.endswith(".json") and ID.match(f[:-5])):
            continue
        rec = load(data_dir, f[:-5])
        if rec and (want is None or norm(rec.get("workspace")) == want):
            m = rec.get("measure") or {}
            out.append({"id": rec["id"], "title": str(rec.get("title", ""))[:80], "created": rec.get("created", ""), "updated": rec.get("updated", ""),
                        "volume": m.get("volume"), "steps": len(rec.get("cells") or []), "workspace": rec.get("workspace") or ""})
    return sorted(out, key=lambda r: r["id"], reverse=True)


def delete(data_dir, sid):
    try:
        os.unlink(_path(data_dir, sid))
        return True
    except OSError:
        return False


def clear(data_dir, workspace=None):
    """Delete saved conversations: all, or only those of one project."""
    n = 0
    d = _dir(data_dir)
    want = None if workspace is None else norm(workspace)
    for f in os.listdir(d):
        if f.endswith((".json", ".tmp")) and (ID.match(f.split(".")[0])):
            if want is not None and f.endswith(".json") and norm((load(data_dir, f[:-5]) or {}).get("workspace")) != want:
                continue
            try:
                os.unlink(os.path.join(d, f)); n += 1
            except OSError:
                pass
    return n
