"""Open the finished part in a desktop CAD program: the part is exported to a STEP file and handed to
  auto        SolidWorks if it is installed, otherwise the operating system's default program for .step
  solidworks  SLDWORKS.exe found under Program Files (a running SolidWorks takes the file into its existing window)
  default     os.startfile / open / xdg-open (needs a program associated with .step; a fresh Windows PC usually has none)
  blender     Blender: the STEP is converted to STL (Blender has no STEP reader) and imported into a fresh scene
  custom      your own command (no shell; `{file}` is replaced by the path, otherwise the path is appended)
Local use only: the web layer refuses this when the app is reachable from other computers."""
import glob, os, re, shlex, shutil, subprocess, sys, urllib.parse

KINDS = ("auto", "solidworks", "default", "blender", "custom")
LABELS = {"auto": "SolidWorks / CAD", "solidworks": "SolidWorks", "default": "chương trình mặc định", "blender": "Blender", "custom": "chương trình tự đặt"}


def solidworks_path():
    if os.name != "nt":
        return None
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            hits = sorted(glob.glob(os.path.join(base, "SOLIDWORKS Corp", "SOLIDWORKS*", "SLDWORKS.exe")))
            if hits:
                return hits[-1]
    return None


def blender_path():
    """Blender executable: $BLENDER, PATH, then the usual install places. None when not found."""
    env = os.environ.get("BLENDER")
    if env and os.path.isfile(env):
        return env
    hit = shutil.which("blender")
    if hit:
        return hit
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                hits = sorted(glob.glob(os.path.join(base, "Blender Foundation", "Blender*", "blender.exe")), key=lambda p: [int(x) for x in re.findall(r"\d+", os.path.basename(os.path.dirname(p)))] or [0])
                if hits:
                    return hits[-1]
    elif sys.platform == "darwin":
        p = "/Applications/Blender.app/Contents/MacOS/Blender"
        return p if os.path.isfile(p) else None
    else:
        for p in ("/snap/bin/blender", "/usr/local/bin/blender", "/opt/blender/blender"):
            if os.path.isfile(p):
                return p
    return None


def open_blender(path):
    """Open a STEP/STL/OBJ file in Blender (STEP is converted to STL first). -> {ok, detail, viewer}. Never raises."""
    exe = blender_path()
    label = LABELS["blender"]
    if not exe:
        return {"ok": False, "viewer": label, "detail": "Blender not found (install it, or set the BLENDER environment variable to its program)"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".step", ".stp", ".stl", ".obj"):
        return {"ok": False, "viewer": label, "detail": "Blender opens STEP, STL and OBJ files from here"}
    here = os.path.dirname(os.path.abspath(__file__))
    target = path
    if ext in (".step", ".stp"):
        import tempfile, hashlib
        d = os.path.join(tempfile.gettempdir(), "cadfabric-blender-%s" % (os.environ.get("USERNAME") or os.environ.get("USER") or "u"))
        os.makedirs(d, exist_ok=True)
        target = os.path.join(d, "%s-%s.stl" % (re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(path))[0])[:40], hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:8]))
        try:
            from .cq_backend import worker_env, NOWIN
            r = subprocess.run([sys.executable, "-I", os.path.join(here, "stepconvert.py"), path, target], capture_output=True, timeout=120, env=worker_env(), **NOWIN)
        except subprocess.TimeoutExpired:
            return {"ok": False, "viewer": label, "detail": "converting the STEP file took too long"}
        if r.returncode != 0 or not os.path.isfile(target):
            return {"ok": False, "viewer": label, "detail": "this STEP file could not be converted for Blender"}
    try:
        _spawn([exe, "--python", os.path.join(here, "blender_import.py"), "--", target])
    except OSError as e:
        return {"ok": False, "viewer": label, "detail": "Blender could not be started: %s" % str(e)[:100]}
    return {"ok": True, "viewer": label, "detail": "sent to Blender"}


def detected():
    return {"solidworks": solidworks_path(), "blender": blender_path()}


def validate(v):
    """-> (clean viewer dict, error)."""
    v = v if isinstance(v, dict) else {}
    kind = v.get("kind") or "auto"
    if kind not in KINDS:
        return None, "unknown viewer kind"
    cmd = str(v.get("command", "") or "").strip()
    if kind == "custom":
        if not cmd or len(cmd) > 500 or re.search(r"[\x00\r\n]", cmd):
            return None, "viewer command is empty or contains control characters"
        try:
            parts = shlex.split(cmd, posix=(os.name != "nt"))
        except ValueError as e:
            return None, "viewer command cannot be parsed (%s)" % e
        if not parts or parts[0].startswith("-"):
            return None, "viewer command must start with a program"
    return {"kind": kind, "command": cmd if kind == "custom" else ""}, ""


def resolve(cfg):
    """-> (kind actually used, label)."""
    kind = (cfg.get("viewer") or {}).get("kind") or "auto"
    if kind == "auto":
        kind = "solidworks" if solidworks_path() else "default"
    return kind, LABELS[kind]


def open_file(path, cfg):
    """Hand `path` to the chosen program. -> {ok, detail, viewer}. Never raises."""
    kind, label = resolve(cfg)
    try:
        if kind == "solidworks":
            exe = solidworks_path()
            if not exe:
                return {"ok": False, "viewer": label, "detail": "SolidWorks not found on this computer (set another viewer in Settings)"}
            _spawn([exe, path])
        elif kind == "blender":
            return open_blender(path)
        elif kind == "custom":
            parts = shlex.split((cfg.get("viewer") or {}).get("command", ""), posix=(os.name != "nt"))
            argv = [x.replace("{file}", path) for x in parts]
            if not any("{file}" in x for x in parts):
                argv.append(path)
            _spawn(argv)
        elif os.name == "nt":
            os.startfile(path)                                                    # noqa: S606 - the file is one we just wrote
        else:
            _spawn(["open" if sys.platform == "darwin" else "xdg-open", path])
    except FileNotFoundError:
        return {"ok": False, "viewer": label, "detail": "the viewer program was not found"}
    except OSError as e:
        msg = "no program is associated with .step files on this computer (choose SolidWorks or a custom command in Settings)" if getattr(e, "winerror", 0) == 1155 else str(e)[:160]
        return {"ok": False, "viewer": label, "detail": msg}
    return {"ok": True, "viewer": label, "detail": "sent to " + label}


def _spawn(argv):
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}      # detached: the viewer outlives this app
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


SAFE_OPEN = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".toml", ".log", ".dxf", ".step", ".stp", ".iges", ".igs", ".png", ".jpg", ".jpeg",
             ".gif", ".webp", ".bmp", ".pdf", ".stl", ".obj"}          # types that cannot run code when the default program opens them (no .py, .js, .html, .svg, .bat ...)


def reveal(path):
    """Show a file or folder in the system file manager (file selected where the OS supports it). -> {ok, detail}. Never raises."""
    try:
        if os.name == "nt":
            _spawn(["explorer", "/select," + os.path.normpath(path)] if os.path.isfile(path) else ["explorer", os.path.normpath(path)])
        elif sys.platform == "darwin":
            _spawn(["open", "-R", path] if os.path.isfile(path) else ["open", path])
        else:
            if os.path.isfile(path) and shutil.which("dbus-send"):
                r = subprocess.run(["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                                    "org.freedesktop.FileManager1.ShowItems", "array:string:file://" + urllib.parse.quote(path), "string:"],
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
                if r.returncode == 0:
                    return {"ok": True, "detail": "shown in the file manager"}
            _spawn(["xdg-open", os.path.dirname(path) if os.path.isfile(path) else path])
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "detail": "no file manager could be started: %s" % str(e)[:100]}
    return {"ok": True, "detail": "shown in the file manager"}


def open_default(path):
    """Open a file with the program the OS associates with it. Only types in SAFE_OPEN. -> {ok, detail}."""
    if os.path.splitext(path)[1].lower() not in SAFE_OPEN:
        return {"ok": False, "detail": "this type of file is not opened from here (use Show in folder)"}
    try:
        if os.name == "nt":
            os.startfile(path)                                                    # noqa: S606 - extension checked above
        else:
            _spawn(["open" if sys.platform == "darwin" else "xdg-open", path])
    except OSError as e:
        return {"ok": False, "detail": "no program is associated with this file: %s" % str(e)[:100]}
    return {"ok": True, "detail": "opened"}
