"""Error runbook ("sổ tay lỗi"): general lessons learned from failed runs, keyed by the TOOL ERROR TEXT, never by the part.

Life cycle of an entry:   candidate (written by the advisor after a run) -> pending -> approved | rejected (by the person)
Only APPROVED entries are ever shown to the agent: when a tool error matches an entry's pattern, the lesson is appended to that
tool result. Nothing is learned silently.

Safeguards against hard-coding one case (checked in `validate`):
  * the pattern must be a valid regex, must MATCH an error text actually seen in the failed run (so it is keyed to a real signature),
    and must not be so broad that it matches an ordinary success message;
  * the lesson must be short and must not contain dimensions (a number followed by a unit) or quoted feature/part names;
  * duplicates (same tool + same pattern) are dropped.
Python standard library only. The file is plain JSON so it can be read and edited by hand."""
import json, os, re, threading, time

_LOCK = threading.Lock()
MAX_LESSON = 600
MAX_ENTRIES = 200
_UNIT = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:mm|cm|m|deg|°|kg|mm3|mm2)\b", re.I)
_QUOTED = re.compile(r"""['"`][A-Za-z][A-Za-z0-9_ .-]{2,}['"`]""")
_OK_SAMPLES = ['{"ok": true}', '{"ok": true, "feature": "X", "rebuild_ok": true}']


def path(data_dir=None):
    return os.environ.get("AICF_RUNBOOK") or os.path.join(data_dir or ".", "runbook.json")


def load(p):
    try:
        with open(p, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(p, entries):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def validate(c, seen_errors):
    """-> (clean entry or None, reason). `c` = {"tool": str|None, "pattern": regex, "lesson": str}."""
    if not isinstance(c, dict):
        return None, "not an object"
    pat, lesson, tool = c.get("pattern"), c.get("lesson"), c.get("tool") or None
    if not isinstance(pat, str) or not isinstance(lesson, str) or not lesson.strip():
        return None, "missing pattern or lesson"
    lesson = lesson.strip()
    if len(lesson) > MAX_LESSON:
        return None, "lesson too long"
    if _UNIT.search(lesson):
        return None, "lesson contains a dimension"
    if _QUOTED.search(lesson):
        return None, "lesson names a specific feature/part"
    if len(pat) < 6 or len(pat) > 200:
        return None, "pattern length"
    try:
        rx = re.compile(pat, re.I)
    except re.error:
        return None, "invalid regex"
    if any(rx.search(s) for s in _OK_SAMPLES):
        return None, "pattern matches ordinary success output"
    relevant = [t for (tl, t) in seen_errors if tool is None or tl == tool]
    if not any(rx.search(t) for t in relevant):
        return None, "pattern does not match any error seen in the run"
    return {"tool": tool, "pattern": pat, "lesson": lesson}, ""


def add_candidates(p, cands, seen_errors, source=""):
    """Validate and store as `pending`. Returns (added, [(candidate, reason) rejected])."""
    added, rejected = [], []
    with _LOCK:
        entries = load(p)
        for c in cands[:5]:
            e, why = validate(c, seen_errors)
            if not e:
                rejected.append((c, why))
                continue
            if any(x.get("tool") == e["tool"] and x.get("pattern") == e["pattern"] and x.get("status") != "rejected" for x in entries):
                rejected.append((c, "duplicate"))
                continue
            if len(entries) >= MAX_ENTRIES:
                rejected.append((c, "runbook full"))
                continue
            e.update(id=(max([x.get("id", 0) for x in entries] or [0]) + 1), status="pending", created=time.strftime("%Y-%m-%d %H:%M"),
                     source=source[:80], hits=0)
            entries.append(e)
            added.append(e)
        if added:
            _save(p, entries)
    return added, rejected


def set_status(p, eid, status):
    if status not in ("approved", "rejected", "pending", "deleted"):
        return False
    with _LOCK:
        entries = load(p)
        for x in entries:
            if x.get("id") == eid:
                if status == "deleted":
                    entries.remove(x)
                else:
                    x["status"] = status
                _save(p, entries)
                return True
    return False


def matches(p, tool, text, limit=2):
    """Approved lessons whose pattern matches this tool error. Counts a hit on each."""
    with _LOCK:
        entries = load(p)
        out = []
        for x in entries:
            if x.get("status") != "approved" or (x.get("tool") and x["tool"] != tool):
                continue
            try:
                if re.search(x["pattern"], text, re.I):
                    out.append(x)
            except re.error:
                continue
            if len(out) >= limit:
                break
        if out:
            for x in out:
                x["hits"] = x.get("hits", 0) + 1
            _save(p, entries)
        return [dict(x) for x in out]


def note_for(p, tool, text):
    """Text to append to a failing tool result (or '')."""
    ms = matches(p, tool, text)
    if not ms:
        return ""
    return "\nRUNBOOK (lessons approved by the user from earlier failures; general, check them against this situation):\n" + \
        "\n".join("- %s" % m["lesson"] for m in ms)


DISTILL_PROMPT = (
    "A CAD agent (a smaller model) struggled with a task. Below are the user's request and the sequence of its tool calls with the tool replies. "
    "Write 1-3 GENERAL lessons that would help any future run recognise and escape the same kind of trap.\n"
    "Reply with ONE JSON list and nothing else: [{\"tool\": \"<tool name or null>\", \"pattern\": \"<regex that matches the TOOL ERROR TEXT>\", "
    "\"lesson\": \"<what the error usually means, what to check with a read-only query, and what to do>\"}]\n"
    "Hard rules: key every lesson to the ERROR MESSAGE of the tool (copy a stable fragment of it into the regex, no numbers or names in it). "
    "Never mention this particular part, its dimensions, or feature/sketch names; no numbers with units. Write it so it would still be true for a "
    "completely different part. Max 3 sentences per lesson. If the run teaches nothing general, reply [].")
