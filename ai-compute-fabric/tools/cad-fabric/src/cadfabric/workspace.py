"""A working folder the user chooses. The model and the advisors get READ access to text files inside it, nothing else.
Every path is relative to the folder and is resolved with realpath, so `..`, absolute paths and symlinks that point outside are refused.
Secrets are never readable (keys, .env, certificates, ssh/git folders). The only thing written is a new .step file, never over an existing one."""
import fnmatch, os, re

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".toml", ".log", ".py", ".dxf", ".step", ".stp", ".iges", ".igs", ".svg", ".html",
            ".htm", ".sql", ".js", ".ts", ".c", ".h", ".cpp", ".rst", ".tex", ".nc", ".gcode", ".scad", ".obj", ".stl"}
DENY_NAMES = ("*.pem", "*.key", "*.p12", "*.pfx", "*.crt", "*.cer", "id_rsa*", "id_ed25519*", ".env*", "key*.txt", "*secret*", "*password*", "*credential*", "*.kdbx", "*.sqlite", "*.db")
DENY_DIRS = {".git", ".ssh", ".gnupg", ".aws", ".config", "node_modules", "__pycache__", ".venv", "venv"}
MAX_READ = 60_000
MAX_LIST = 400
MAX_ATTACH_TOTAL = 90_000
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,80}$")


def check_folder(path):
    """-> (clean absolute path, error). The folder itself must exist; it is stored as typed by the user (absolute)."""
    p = str(path or "").strip()
    if not p:
        return "", ""
    if len(p) > 400 or re.search(r"[\x00\r\n]", p):
        return None, "folder path is not valid"
    p = os.path.abspath(os.path.expanduser(p))
    if not os.path.isdir(p):
        return None, "folder does not exist: " + p
    if p in (os.path.abspath(os.sep), os.path.expanduser("~")) or (os.name == "nt" and re.match(r"^[A-Za-z]:\\?$", p)):
        return None, "choose a project folder, not a drive root or your whole home folder"
    return p, ""


def _denied(rel_parts):
    for part in rel_parts:
        low = part.lower()
        if low in DENY_DIRS:
            return True
        if any(fnmatch.fnmatch(low, pat) for pat in DENY_NAMES):
            return True
    return False


def resolve(root, rel):
    """-> (absolute real path, error). Never leaves `root`."""
    if not root:
        return None, "no working folder is set (choose one in Settings)"
    rel = str(rel or "").replace("\\", "/").strip("/")
    if re.search(r"[\x00\r\n]", rel) or re.match(r"^[A-Za-z]:", rel) or rel.startswith("~"):
        return None, "path must be relative to the working folder"
    parts = [x for x in rel.split("/") if x not in ("", ".")]
    if ".." in parts:
        return None, "path must stay inside the working folder"
    if _denied(parts):
        return None, "that file is protected and cannot be read"
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, *parts))
    if full != base and not full.startswith(base + os.sep):
        return None, "path must stay inside the working folder"
    return full, ""


def listing(root, rel=""):
    """-> ({path, entries:[{name, dir, size}], truncated}, error). One level; folders first."""
    full, err = resolve(root, rel)
    if err:
        return None, err
    if not os.path.isdir(full):
        return None, "not a folder: " + str(rel)
    rows = []
    try:
        names = sorted(os.listdir(full), key=lambda n: (not os.path.isdir(os.path.join(full, n)), n.lower()))
    except OSError as e:
        return None, str(e)[:120]
    base = os.path.realpath(root)
    for n in names:
        if n.startswith(".") or _denied([n]):
            continue
        p = os.path.realpath(os.path.join(full, n))
        if p != base and not p.startswith(base + os.sep):
            continue                                                            # a link that points outside is simply not shown
        d = os.path.isdir(p)
        try:
            size = None if d else os.path.getsize(p)
        except OSError:
            continue
        rows.append({"name": n, "dir": d, "size": size, "text": (not d) and os.path.splitext(n)[1].lower() in TEXT_EXT})
    return {"path": str(rel or "").replace("\\", "/").strip("/"), "entries": rows[:MAX_LIST], "truncated": len(rows) > MAX_LIST}, ""


def read_text(root, rel, limit=MAX_READ):
    """-> (text, error). Only known text types; large files are cut and say so."""
    full, err = resolve(root, rel)
    if err:
        return None, err
    if not os.path.isfile(full):
        return None, "no such file: " + str(rel)
    if os.path.splitext(full)[1].lower() not in TEXT_EXT:
        return None, "only text files can be read (%s)" % ", ".join(sorted(TEXT_EXT)[:12]) + ", ..."
    try:
        with open(full, "rb") as f:
            raw = f.read(limit + 1)
    except OSError as e:
        return None, str(e)[:120]
    if b"\x00" in raw[:4096]:
        return None, "this file is binary"
    cut = len(raw) > limit
    text = raw[:limit].decode("utf-8", errors="replace")
    return text + ("\n[... cut at %d bytes ...]" % limit if cut else ""), ""


def attachments(root, rels):
    """Turn selected files into one text block for the prompt. -> (block, files used, error)."""
    out, used, left = [], [], MAX_ATTACH_TOTAL
    for rel in rels or []:
        text, err = read_text(root, rel, limit=min(MAX_READ, left))
        if err:
            return "", [], "%s: %s" % (rel, err)
        out.append("----- FILE: %s -----\n%s" % (str(rel).replace("\\", "/"), text))
        used.append(str(rel).replace("\\", "/"))
        left -= len(text)
        if left <= 0:
            break
    return ("\n\n".join(out) if out else ""), used, ""


def search(root, needle, rel="", max_hits=30):
    """Case-insensitive substring search in text files. -> (lines, error)."""
    full, err = resolve(root, rel)
    if err:
        return None, err
    needle = str(needle or "").strip().lower()
    if len(needle) < 2:
        return None, "search text is too short"
    base, hits, scanned = os.path.realpath(root), [], 0
    for d, dirs, files in os.walk(full):
        dirs[:] = [x for x in dirs if not x.startswith(".") and not _denied([x])]
        for n in sorted(files):
            if n.startswith(".") or _denied([n]) or os.path.splitext(n)[1].lower() not in TEXT_EXT:
                continue
            p = os.path.realpath(os.path.join(d, n))
            if not p.startswith(base + os.sep):
                continue
            scanned += 1
            if scanned > 500:
                return hits, ""
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if needle in line.lower():
                            hits.append("%s:%d: %s" % (os.path.relpath(p, base).replace("\\", "/"), i, line.strip()[:200]))
                            if len(hits) >= max_hits:
                                return hits, ""
            except OSError:
                continue
    return hits, ""


def uploads(items):
    """Files dropped into the chat (name + text, no path on disk). -> (block, names, error). Same limits as attachments."""
    if not isinstance(items, list) or len(items) > 8:
        return "", [], "at most 8 dropped files"
    out, names, left = [], [], MAX_ATTACH_TOTAL
    for it in items:
        name = os.path.basename(str((it or {}).get("name", "")).replace("\\", "/")) if isinstance(it, dict) else ""
        text = it.get("text") if isinstance(it, dict) else None
        if not name or len(name) > 120 or re.search(r"[\x00\r\n]", name) or not isinstance(text, str):
            return "", [], "bad dropped file"
        if os.path.splitext(name)[1].lower() not in TEXT_EXT:
            return "", [], "%s: only text files can be dropped" % name
        if _denied([name]):
            return "", [], "%s: that file is protected" % name
        if "\x00" in text[:4096]:
            return "", [], "%s: this file is binary" % name
        text = text[:min(MAX_READ, max(left, 0))]
        out.append("----- FILE (dropped): %s -----\n%s" % (name, text)); names.append(name); left -= len(text)
    return "\n\n".join(out), names, ""


def new_step_path(root, name):
    """A fresh path for a STEP export inside the folder (never an existing file). -> (path, error)."""
    n = str(name or "part").strip()
    n = re.sub(r"\.(step|stp)$", "", n, flags=re.I)
    if not _NAME.match(n):
        return None, "file name may only use letters, digits, space, - _ ."
    base = os.path.realpath(root or "")
    if not root or not os.path.isdir(base):
        return None, "no working folder is set (choose one in Settings)"
    for i in range(0, 100):
        p = os.path.join(base, n + ("" if i == 0 else "-%d" % i) + ".step")
        if not os.path.exists(p):
            return p, ""
    return None, "too many files with that name"
