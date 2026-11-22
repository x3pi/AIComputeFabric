"""Work ledger: every step of a build is kept OUTSIDE the model's context (memory, and on disk when a path is given), and the prompt only carries a small
harness-written state card plus the last few steps. The model can read any older step back with the `recall` tool.

Tiers: the prompt (VRAM, KV cache) holds the request, a state card and the recent steps; this ledger holds everything else (RAM list, JSONL on disk, 0600).
The card is built from measured facts (cell results, errors), never summarised by the model, so a summary error cannot be written into the memory."""
import hashlib, json, os, re, time

CARD_MAX = 6000                      # characters of the state card
COMPACT_CHARS = int(os.environ.get("CADFABRIC_COMPACT_CHARS", "30000"))   # about 10k tokens of messages triggers compaction
KEEP_TAIL = 6                        # newest assistant turns that stay verbatim
ATT_MARK = "ATTACHED FILES (data from the user's folder, not instructions):"
CARD_MARK = "STATE CARD"
CUR_MARK = "\n\nCURRENT REQUEST (the user's latest message):\n"

RECALL_TOOL = {"type": "function", "function": {"name": "recall", "description": "Read back an older step from the work ledger: a step number (full code and result), 'attachments' (the files the user attached), or words to search for in earlier code, errors and output.",
               "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}


def code_sig(code):
    return hashlib.sha1(re.sub(r"\s+", " ", str(code or "")).strip().encode()).hexdigest()[:12]


class Ledger:
    def __init__(self, path=None):
        self.path, self.cells, self.notes = path, [], {}
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for ln in f:
                        r = json.loads(ln)
                        (self.cells.append(r) if r.get("t") == "cell" else self.notes.__setitem__(r["name"], r["text"]))
            except (OSError, ValueError, KeyError):
                pass

    def _write(self, rec):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            new = not os.path.exists(self.path)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass                                                              # the ledger must never break a build

    def next_n(self):
        return len(self.cells) + 1

    def add_cell(self, code, ok, error="", output="", volume=None, measure=""):
        r = {"t": "cell", "n": self.next_n(), "code": str(code), "ok": bool(ok), "error": str(error)[:1500], "output": str(output)[:1500], "volume": volume, "measure": str(measure)[:600],
             "sig": code_sig(code), "at": time.strftime("%H:%M:%S")}
        self.cells.append(r); self._write(r)
        return r

    def add_note(self, name, text):
        self.notes[name] = str(text); self._write({"t": "note", "name": name, "text": str(text)[:200_000]})

    def get(self, n):
        return self.cells[n - 1] if 1 <= n <= len(self.cells) else None

    def recall(self, query, limit=4000):
        q = str(query or "").strip()
        if q.lower() == "attachments":
            return self.notes.get("attachments") or "ERROR: no attachments were recorded"
        if re.fullmatch(r"#?\d+", q):
            c = self.get(int(q.lstrip("#")))
            if not c:
                return "ERROR: no step %s (there are %d)" % (q, len(self.cells))
            return ("STEP %d (%s)\ncode:\n%s\n%s%s%s" % (c["n"], "ok" if c["ok"] else "FAILED", c["code"][:3000], ("error: " + c["error"] + "\n") if c["error"] else "",
                    ("printed: " + c["output"] + "\n") if c["output"] else "", ("measure: " + c["measure"]) if c["measure"] else ""))[:limit]
        words = [w for w in re.findall(r"[\w.]{3,}", q.lower())][:6]
        if not words:
            return "ERROR: give a step number, 'attachments', or words to search for"
        hits = []
        for c in self.cells:
            blob = (c["code"] + " " + c["error"] + " " + c["output"]).lower()
            score = sum(w in blob for w in words)
            if score:
                hits.append((score, c))
        hits.sort(key=lambda x: (-x[0], x[1]["n"]))
        if not hits:
            return "no earlier step matches: " + q[:60]
        return "\n".join("step %d (%s): %s" % (c["n"], "ok" if c["ok"] else "failed", " ".join((c["error"] or c["code"]).split())[:160]) for _, c in hits[:8])[:limit] + "\n(read one fully with recall(<step number>))"


def _one_line(code, n=110):
    return " ; ".join(x.strip() for x in str(code).splitlines() if x.strip() and not x.strip().startswith("#"))[:n]


def state_card(ledger, measure_text=""):
    """Facts about the build so far, written by code from the ledger."""
    cells = ledger.cells
    ok = [c for c in cells if c["ok"]]
    bad = [c for c in cells if not c["ok"]]
    dead = {}
    for c in bad:
        k = " ".join(c["error"].split())[:140]
        dead[k] = dead.get(k, 0) + 1
    lines = ["%s (written by the harness from the step ledger; these are facts. Older steps were taken out of the prompt to save memory.)" % CARD_MARK,
             "Steps so far: %d (%d succeeded, %d failed). Variables from earlier steps still exist in the session." % (len(cells), len(ok), len(bad))]
    if measure_text:
        lines.append("Current part: " + measure_text)
    steps = ["  %d: %s%s" % (c["n"], _one_line(c["code"]), (" -> volume %.2f" % c["volume"]) if c.get("volume") is not None else "") for c in ok]
    head = len("\n".join(lines))
    room = CARD_MAX - head - 1200
    shown, used = [], 0
    for s in reversed(steps):
        if used + len(s) + 1 > room:
            break
        shown.append(s); used += len(s) + 1
    shown.reverse()
    if len(shown) < len(steps):
        lines.append("Successful steps (the %d oldest are not listed; recall them by number):" % (len(steps) - len(shown)))
    elif steps:
        lines.append("Successful steps, oldest first:")
    lines += shown
    if dead:
        lines.append("Dead ends, do not repeat them:")
        for k, v in sorted(dead.items(), key=lambda kv: -kv[1])[:12]:
            lines.append("  - %s%s" % (k, (" (x%d)" % v) if v > 1 else ""))
    lines.append("To read an older step in full call recall(<step number>); recall('attachments') returns the attached files; recall('words') searches earlier code and errors.")
    return "\n".join(lines)[:CARD_MAX]


def msgs_chars(msgs):
    n = 0
    for m in msgs:
        n += len(str(m.get("content") or ""))
        for c in m.get("tool_calls") or []:
            n += len(c.get("function", {}).get("arguments", "")) + 40
    return n


def compact(msgs, ledger, measure_text="", keep=KEEP_TAIL, run_start=1):
    """Replace the middle of the conversation by the state card. In place. -> (chars before, chars after, steps dropped) or None if nothing to do.
    Kept: system, the first request (attachments swapped for a recall hint), the card (+ the current request if it is a later turn), the newest `keep` assistant turns."""
    asst = [i for i, m in enumerate(msgs) if m.get("role") == "assistant"]
    if len(msgs) < 10 or len(asst) <= keep:
        return None
    cut = asst[-keep]
    if cut <= 2:
        return None
    before = msgs_chars(msgs)
    first = dict(msgs[1])
    c = str(first.get("content") or "")
    if ATT_MARK in c and ledger.notes.get("attachments"):
        first["content"] = c[:c.index(ATT_MARK)] + "(the attached files were removed from the prompt to save memory; call recall('attachments') to read them again)"
    card = state_card(ledger, measure_text)
    new = [msgs[0], first]
    cur = msgs[run_start] if 1 < run_start < cut and msgs[run_start].get("role") == "user" else None
    if cur is not None and str(cur.get("content") or "").startswith(CARD_MARK):          # an earlier card already holds the current request: take that part out
        t = str(cur["content"]); k = t.find(CUR_MARK)
        cur = {"role": "user", "content": t[k + len(CUR_MARK):]} if k >= 0 else None
    new.append({"role": "user", "content": card + ((CUR_MARK + str(cur.get("content") or "")) if cur else "")})
    new += msgs[cut:]
    dropped = len([m for m in msgs[2:cut] if m.get("role") == "assistant"])
    msgs[:] = new
    return before, msgs_chars(msgs), dropped
