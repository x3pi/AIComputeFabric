"""Lessons learned: general practices rolled out of finished conversations, kept only after the user approves them.

A lesson is advice about METHOD (how to model, which CadQuery call to prefer, how to verify, how to read an ambiguous request), never an answer to one task.
Three guards keep it that way:
  1. the reflection prompt asks for placeholders instead of concrete values and for evidence from the conversation;
  2. `check()` refuses candidates that carry concrete dimensions, units, part-specific numbers, code assignments, paths or URLs;
  3. nothing reaches the model until the user has approved it (pending and rejected lessons are never used); a rejected idea is remembered so it is not proposed again.
Stored in <data dir>/lessons.json (0600), per project or global."""
import json, os, re, secrets, time, unicodedata

MAX_LESSONS = 300
MAX_USED = 10                       # approved lessons given to the model per new conversation
MAX_BLOCK = 2800                    # characters
SCOPES = ("project", "global")
STATUSES = ("pending", "approved", "rejected")

REFLECT_PROMPT = """You review ONE finished CAD-modelling conversation (a request, the plan notes, the CadQuery cells that were run with their errors, the harness checks, the answer) and extract LESSONS for FUTURE, DIFFERENT requests.

Rules:
- A lesson is a GENERAL practice: how to model a kind of feature, which CadQuery usage works or fails, how to verify a result, how to read an ambiguous request. It must be useful for other parts, not only this one.
- NEVER put concrete values from this task in a lesson: no dimensions, no counts, no units, no part names, no file names, no code assignments. Use placeholders such as "the hole diameter", "the tube height", "a pattern of holes".
- Do not describe the solution of this one task. Do not invent: each lesson must be supported by something that actually happened (an error, a failed check, a correction by the user, an advisor pitfall).
- Do not quote identifiers that failed because they do not exist (invented function or class names): say what to do instead.
- Write the lessons in the language the user wrote their request in.
- Prefer few, good lessons. Returning none is fine.

Answer with JSON only:
{"lessons":[{"title":"<=60 chars","rule":"imperative advice, <=240 chars","when":"when it applies, <=120 chars","evidence":"what happened, in general terms, <=160 chars"}]}"""

_UNITS = re.compile(r"\d+([.,]\d+)?\s*(mm|cm|m|km|in|inch|inches|deg|degrees|độ|°|mm2|mm3|mm²|mm³|kg|g|%)(?![a-z])", re.I)
_BAD = [
    (_UNITS, "a concrete measurement"),
    (re.compile(r"[Ø⌀∅]\s*\d|\bM\d{1,2}\b"), "a concrete size"),
    (re.compile(r"\b\d{3,}\b"), "a specific number"),
    (re.compile(r"\b\d+\s*[x×]\s*\d+", re.I), "concrete dimensions"),
    (re.compile(r"\b(result|part|wp|w)\s*=\s*\S"), "code assignment from the task"),
    (re.compile(r"[A-Za-z]:\\|/home/|/tmp/|https?://|\.(step|stp|stl|py|json)\b", re.I), "a path, URL or file name"),
]


def _norm(t):
    t = unicodedata.normalize("NFKD", str(t).lower().replace("đ", "d")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", t)


def tokens(t):
    return {w for w in _norm(t).split() if len(w) > 2}


def similar(a, b):
    x, y = tokens(a), tokens(b)
    return bool(x and y) and len(x & y) / len(x | y) >= 0.55


def check(item):
    """-> (clean lesson dict, '') or (None, reason). Enforces shape and the 'general, not hardcoded' rule."""
    if not isinstance(item, dict):
        return None, "not an object"
    out = {}
    for key, cap in (("title", 70), ("rule", 280), ("when", 140), ("evidence", 200)):
        v = " ".join(str(item.get(key, "") or "").split())
        if key in ("title", "rule") and len(v) < 8:
            return None, "missing " + key
        if len(v) > cap:
            v = v[:cap].rstrip()
        out[key] = v
    blob = " ".join(out.values())
    for rx, why in _BAD:
        if rx.search(blob):
            return None, "too specific: " + why
    return out, ""


def _file(data_dir):
    return os.path.join(data_dir, "lessons.json")


def _load(data_dir):
    try:
        with open(_file(data_dir), encoding="utf-8") as f:
            d = json.load(f)
        return [x for x in d.get("lessons", []) if isinstance(x, dict) and x.get("id")]
    except (OSError, ValueError):
        return []


def _save(data_dir, items):
    os.makedirs(data_dir, exist_ok=True)
    p = _file(data_dir)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"v": 1, "lessons": items[-MAX_LESSONS:]}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def proj_key(project):
    return os.path.realpath(project) if project else ""


def visible(items, project):
    """Lessons that apply to this window: its own project's plus the global ones."""
    key = proj_key(project)
    return [x for x in items if x.get("scope") == "global" or (x.get("scope") == "project" and x.get("project") == key)]


def list_for(data_dir, project):
    return sorted(visible(_load(data_dir), project), key=lambda x: (x.get("status") != "pending", x.get("created", "")), reverse=False)


def add(data_dir, project, cands, source="", scope=None, status="pending"):
    """Store validated candidates, skipping near-duplicates of ANY existing lesson (rejected ones too: a refused idea is not proposed again).
    -> (added list, skipped [(title, reason)])."""
    items = _load(data_dir)
    key = proj_key(project)
    scope = scope if scope in SCOPES else ("project" if key else "global")
    added, skipped = [], []
    for c in cands or []:
        ok, why = check(c)
        if not ok:
            skipped.append((str((c or {}).get("title", "") if isinstance(c, dict) else c)[:60], why)); continue
        text = ok["title"] + " " + ok["rule"]
        if any(similar(text, x.get("title", "") + " " + x.get("rule", "")) for x in visible(items, project) + [i for i in items if i.get("project") == key]):
            skipped.append((ok["title"], "already known")); continue
        item = dict(ok, id=time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(2), scope=scope, project=key if scope == "project" else "", status=status,
                    source=source, created=time.strftime("%Y-%m-%d %H:%M:%S"), uses=0)
        items.append(item); added.append(item)
    if added:
        _save(data_dir, items)
    return added, skipped


def _find(items, project, lid):
    for x in visible(items, project):
        if x["id"] == lid:
            return x
    return None


def update(data_dir, project, lid, **kw):
    """status / scope / title / rule / when edits. Edited text goes through check() again. -> (lesson, error)."""
    items = _load(data_dir)
    x = _find(items, project, lid)
    if not x:
        return None, "no such lesson"
    if "status" in kw:
        if kw["status"] not in STATUSES:
            return None, "bad status"
        x["status"] = kw["status"]
    if "scope" in kw:
        if kw["scope"] not in SCOPES or (kw["scope"] == "project" and not project):
            return None, "bad scope"
        x["scope"], x["project"] = kw["scope"], (proj_key(project) if kw["scope"] == "project" else "")
    if any(k in kw for k in ("title", "rule", "when")):
        ok, why = check({"title": kw.get("title", x["title"]), "rule": kw.get("rule", x["rule"]), "when": kw.get("when", x.get("when", "")), "evidence": x.get("evidence", "")})
        if not ok:
            return None, why
        x.update(title=ok["title"], rule=ok["rule"], when=ok["when"])
        if x["status"] == "approved":
            x["status"] = "pending"                                  # changed text must be approved again
    x["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(data_dir, items)
    return x, ""


def delete(data_dir, project, lid):
    items = _load(data_dir)
    x = _find(items, project, lid)
    if not x:
        return False
    _save(data_dir, [i for i in items if i["id"] != lid])
    return True


def approved_block(data_dir, project, request=""):
    """-> (text for the system prompt or '', [ids used]). Only approved lessons; with many, the ones that overlap the request most."""
    ap = [x for x in visible(_load(data_dir), project) if x.get("status") == "approved"]
    if not ap:
        return "", []
    want = tokens(request)
    if len(ap) > MAX_USED:
        ap.sort(key=lambda x: (len(want & tokens(x["title"] + " " + x["rule"] + " " + x.get("when", ""))), x.get("created", "")), reverse=True)
        ap = ap[:MAX_USED]
    lines, used, n = [], [], 0
    for x in ap:
        ln = "- %s: %s%s" % (x["title"], x["rule"], (" (when: %s)" % x["when"]) if x.get("when") else "")
        if n + len(ln) > MAX_BLOCK:
            break
        lines.append(ln); used.append(x["id"]); n += len(ln)
    return ("\n\nLESSONS LEARNED (general practices the user approved from earlier work; apply them only when they fit the request, they are advice, not facts about this task):\n" + "\n".join(lines)), used


def mark_used(data_dir, ids):
    if not ids:
        return
    items = _load(data_dir)
    for x in items:
        if x["id"] in ids:
            x["uses"] = int(x.get("uses", 0)) + 1
    _save(data_dir, items)


def signals(events):
    """Did the conversation contain something worth learning from? (failed cell, harness gate, advisor pitfall, an answer the plan checks rejected)."""
    for e in events:
        if e.get("kind") == "cell" and not e.get("ok", True):
            return True
        if e.get("kind") == "gate":
            return True
        if e.get("kind") == "plan" and e.get("pitfalls"):
            return True
        if e.get("kind") == "final" and (e.get("warnings") or 0):
            return True
    return False


def digest(events, limit=7000):
    """The conversation as compact text for the reflection call."""
    out = []
    for e in events:
        k = e.get("kind")
        if k == "user":
            out.append("USER: " + str(e.get("text", ""))[:600])
        elif k == "plan":
            out.append("PLAN: %s%s" % (str(e.get("describe", ""))[:500], ("\nADVISOR PITFALLS: " + "; ".join(e["pitfalls"])) if e.get("pitfalls") else ""))
        elif k == "cell":
            out.append("CELL %s %s:\n%s%s" % (e.get("n"), "OK" if e.get("ok") else "FAILED", str(e.get("code", ""))[:700], ("\nERROR: " + str(e.get("error", ""))[:300]) if e.get("error") else ""))
        elif k == "gate":
            out.append("HARNESS CHECK: " + str(e.get("text", ""))[:300])
        elif k == "final":
            out.append("ANSWER: " + str(e.get("text", ""))[:500])
        elif k == "error":
            out.append("ERROR: " + str(e.get("text", ""))[:300])
    text = "\n\n".join(out)
    return text if len(text) <= limit else text[:limit // 2] + "\n...\n" + text[-limit // 2:]


def reflect(llm, events):
    """Ask the model for candidate lessons. -> (candidates list, raw text). Never raises on a bad answer."""
    from . import planners
    body = digest(events)
    if not body:
        return [], ""
    m = llm.chat([{"role": "system", "content": REFLECT_PROMPT}, {"role": "user", "content": body}], think=False, max_tokens=1400, temperature=0.2)
    raw = m.get("content") or ""
    j = planners.extract_json(raw) or {}
    c = j.get("lessons") if isinstance(j, dict) else None
    return [x for x in (c if isinstance(c, list) else [])][:6], raw
