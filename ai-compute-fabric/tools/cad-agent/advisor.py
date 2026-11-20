#!/usr/bin/env python3
"""ask_advisor: the ONLY door through which anything may leave the CAD machine toward a big external model.

Flow:  structured request from the local model -> sanitize() -> (human approval if mode=ask) -> backend -> reply
       -> every payload and reply is appended to a hash-chained audit log. The reply is untrusted text; code in it must pass
       check_code() before anything runs it.

Design rules
  * Fail closed: any doubt (a denylist term survives, a blob is found, the local reviewer says "leak", the reviewer is down
    while it is required) -> Blocked, nothing is sent.
  * Schema, not free chat: kind (enum) + api_names (identifiers) + error_class (short) + question (<= 1200 chars).
  * Numbers are masked (dimensions are confidential); file paths/URLs/emails masked; known names -> PART_A, PART_B...;
    the denylist (project/customer/part-number terms) is matched on a NORMALIZED form (case, accents, width, spaces,
    punctuation, zero-width characters, %-escapes, base64/hex tokens) so obfuscated spellings are still caught.
  * Backends: "dryrun" (default; writes the would-be payload, sends nothing), "api" (HTTPS chat call, no tools exist),
    "codex_cli" (REFUSED unless a contained Windows user is configured: codex's read-only sandbox blocks writes, NOT reads).
Honest limit: filters reduce risk, they do not prove absence of leakage (a new encoding or paraphrase of a secret that is not
in the denylist can pass). Measure with canary_test.py and keep L2 data out of prompts by design.
"""
import base64, binascii, hashlib, json, os, re, subprocess, tempfile, time, unicodedata, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
import aicfconf
AUDIT = os.environ.get("ADVISOR_AUDIT") or os.path.join(aicfconf.data_dir(), "advisor_audit.jsonl")
KINDS = {"api_usage", "error_help", "strategy", "review_abstract"}
MAX_Q = 1200


class Blocked(Exception):
    pass


ALL_RULES = ("schema", "pseudonyms", "mask_numbers", "mask_paths", "mask_urls", "blobs", "denylist", "denylist_variants", "length")


def norm(s):
    s = urllib.parse.unquote(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("đ", "d")
    return re.sub(r"[^a-z0-9]", "", s)


_B64 = re.compile(r"[A-Za-z0-9+/_-]{12,}={0,2}")
_HEX = re.compile(r"\b(?:[0-9a-fA-F]{2}){8,}\b")


def _decoded_variants(text):
    """texts hidden inside base64 / hex tokens (and the plain text itself)."""
    out = [text]
    for m in _B64.finditer(text):
        t = m.group(0)
        for fn in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                out.append(fn(t + "=" * (-len(t) % 4)).decode("utf-8", "ignore"))
            except Exception:
                pass
    for m in _HEX.finditer(text):
        try:
            out.append(bytes.fromhex(m.group(0)).decode("utf-8", "ignore"))
        except Exception:
            pass
    return out


_PROTECT = [re.compile(p) for p in (
    r"\b(?:ISO|DIN|EN|ASME|ANSI|JIS)\s?[- ]?\d{2,6}\b",       # standard numbers carry API meaning, not design data
    r"\bsw[A-Z][A-Za-z0-9_]*",                                    # SolidWorks API enums: swFeatureType_e ...
    r"\bM\d{1,2}\b",                                              # thread designation
    r"\bSOLIDWORKS\s?20\d\d\b", r"\bPython\s?3(?:\.\d+)?\b", r"\bbpy\b",
)]
_NUM = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")
_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s'\"]*|\\\\[^\s'\"]+|(?:/(?:home|mnt|Users|var|opt|tmp|etc)/[^\s'\"]*)|[\w\-. ]+\.(?:sldprt|sldasm|slddrw|step|stp|iges|igs|stl|obj|blend|fbx|3mf|glb|dwg|dxf|xlsx|docx|pdf|csv)\b)", re.I)
_URL = re.compile(r"(?:https?|ftp)://\S+|\b[\w.+-]+@[\w-]+\.[\w.-]+\b", re.I)
_DATAURI = re.compile(r"data:[\w/+.-]+;base64,", re.I)


def sanitize(req, cfg=None, disabled=()):
    """req: dict(kind, api_names[], error_class, question). Returns (clean_text, report). Raises Blocked."""
    cfg = cfg or {}
    on = lambda r: r not in disabled
    rep = {"masked_numbers": 0, "masked_paths": 0, "masked_urls": 0, "pseudonyms": 0}
    if on("schema"):
        if not isinstance(req, dict) or req.get("kind") not in KINDS:
            raise Blocked("kind must be one of %s" % sorted(KINDS))
        names = req.get("api_names") or []
        if not isinstance(names, list) or len(names) > 12 or not all(isinstance(n, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,60}", n) for n in names):
            raise Blocked("api_names must be <=12 plain identifiers")
        if not isinstance(req.get("question", ""), str) or not isinstance(req.get("error_class", ""), str) or len(req.get("error_class", "")) > 120:
            raise Blocked("bad question/error_class")
    text = "KIND: %s\nAPI: %s\nERROR CLASS: %s\nQUESTION: %s" % (
        req.get("kind"), ", ".join(req.get("api_names") or []), req.get("error_class", ""), req.get("question", ""))
    if on("length") and len(text) > MAX_Q + 400:
        raise Blocked("request too long (%d)" % len(text))
    if on("blobs"):
        if _DATAURI.search(text):
            raise Blocked("data URI / embedded file")
        for tok in _B64.findall(text):
            if len(tok) >= 40 and re.search(r"[A-Z]", tok) and re.search(r"[a-z]", tok) and re.search(r"\d", tok):
                raise Blocked("base64-like blob")
        if _HEX.search(text) and len(_HEX.search(text).group(0)) >= 32:
            raise Blocked("hex blob")
    if on("denylist"):
        # check the ORIGINAL text first: masking numbers would otherwise turn "ZX-CRN-7741" into "ZX-CRN-<N>" and the
        # remaining prefix would no longer match the full denylist term while still identifying the part
        raw_terms = [norm(t) for t in cfg.get("denylist", []) if len(norm(t)) >= 3]
        # also match the prefix before the first digit run of a term (e.g. "ZX-CRN" of "ZX-CRN-7741")
        for t in list(cfg.get("denylist", [])):
            m = re.match(r"^\D*[A-Za-z]\D*?(?=\d)", t)
            if m and len(norm(m.group(0))) >= 4:
                raw_terms.append(norm(m.group(0)))
        for v in (_decoded_variants(text) if on("denylist_variants") else [text]):
            nv = norm(v) if on("denylist_variants") else v.lower()
            if any(t in nv for t in raw_terms):
                raise Blocked("denylisted term in request")
    if on("pseudonyms"):
        for i, term in enumerate(cfg.get("pseudonyms", [])):
            rx = re.compile(r"[\W_]*".join(re.escape(c) for c in term if c.strip()), re.I)
            text, n = rx.subn("PART_%s" % chr(65 + i % 26), text)
            rep["pseudonyms"] += n
    if on("mask_urls"):
        text, n = _URL.subn("<URL>", text)
        rep["masked_urls"] = n
    if on("mask_paths"):
        text, n = _PATH.subn("<PATH>", text)
        rep["masked_paths"] = n
    if on("mask_numbers"):
        holders = []
        enc = lambda i: "".join(chr(65 + int(d)) for d in str(i))       # digit-free placeholder so the number mask cannot touch it
        for rx in _PROTECT:
            text = rx.sub(lambda m: holders.append(m.group(0)) or "\x01" + enc(len(holders) - 1) + "\x02", text)
        text, n = _NUM.subn("<N>", text)
        rep["masked_numbers"] = n
        dec = lambda s: int("".join(str(ord(c) - 65) for c in s))
        text = re.sub(r"\x01([A-J]+)\x02", lambda m: holders[dec(m.group(1))], text)
    if on("denylist"):
        terms = [norm(t) for t in cfg.get("denylist", []) + cfg.get("pseudonyms", []) if len(norm(t)) >= 3]
        variants = _decoded_variants(text) if on("denylist_variants") else [text]
        for v in variants:
            nv = norm(v) if on("denylist_variants") else v.lower()
            for t in terms:
                if t in nv:
                    raise Blocked("denylisted term survives sanitising")
    return text, rep


# ---- reviewer (local Qwen), fail closed --------------------------------------------------------------------------
def review_local(text, cfg):
    import aicfconf
    key = aicfconf.read_key()
    body = {"model": cfg.get("model") or aicfconf.model(), "max_tokens": 60, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": "You are a data-leak reviewer. The text will be sent to an external service. Answer ONLY JSON {\"leak\": true|false}. leak=true if it reveals a specific dimension, quantity, customer/project/product name, part number, file name, location, or any identifying detail of a particular design. Generic API questions are leak=false."},
                         {"role": "user", "content": text}]}
    req = urllib.request.Request(cfg.get("url") or aicfconf.url(), json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + key})
    out = json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["message"]["content"]
    m = re.search(r"\{.*?\}", out, re.S)
    return bool(json.loads(m.group(0)).get("leak"))


# ---- audit (hash chained) --------------------------------------------------------------------------------------
def audit(kind, **kw):
    prev = "0" * 64
    if os.path.exists(AUDIT):
        with open(AUDIT, "rb") as f:
            lines = f.read().splitlines()
        if lines:
            prev = json.loads(lines[-1])["hash"]
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **kw, "prev": prev}
    rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def verify_audit(path=None):
    prev = "0" * 64
    for line in open(path or AUDIT, encoding="utf-8"):
        r = json.loads(line)
        h = r.pop("hash")
        if r["prev"] != prev or hashlib.sha256(json.dumps(r, sort_keys=True, ensure_ascii=False).encode()).hexdigest() != h:
            return False
        prev = h
    return True


# ---- reply safety ---------------------------------------------------------------------------------------------
_BAD_NAMES = {"os", "sys", "subprocess", "socket", "ctypes", "shutil", "open", "exec", "eval", "compile", "__import__", "input",
              "urllib", "requests", "http", "pathlib", "importlib", "builtins", "getattr", "setattr", "globals", "locals", "pickle"}
_OK_IMPORTS = {"math", "json", "bpy", "bmesh", "mathutils", "typing", "re"}
import ast


def check_code(code):
    """True, '' if the advisor's Python snippet only uses allowlisted modules and no dangerous names."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, "syntax error: %s" % e
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name.split(".")[0] not in _OK_IMPORTS for a in n.names):
            return False, "import not allowed"
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] not in _OK_IMPORTS:
            return False, "import not allowed"
        if isinstance(n, ast.Name) and n.id in _BAD_NAMES:
            return False, "name not allowed: " + n.id
        if isinstance(n, ast.Attribute) and (n.attr.startswith("__") or n.attr in ("system", "popen", "remove", "rmtree", "unlink", "environ")):
            return False, "attribute not allowed: " + n.attr
    return True, ""


# ---- backends -------------------------------------------------------------------------------------------------
ADVISOR_SYSTEM = ("You advise on CAD scripting. Reply with concise guidance and, if useful, a short Python snippet. "
                  "You have no tools and no access to any files.")


def _advisor_key(cfg):
    """Key from key_file (preferred: a file only the user can read) or from the env var named by key_env. Never logged."""
    if cfg.get("key_file"):
        with open(os.path.expandvars(os.path.expanduser(cfg["key_file"])), encoding="utf-8-sig") as f:
            return f.read().strip()
    return os.environ[cfg.get("key_env", "ADVISOR_API_KEY")].strip()


def _ask_anthropic(cfg, text):
    """Anthropic Messages API (also served by Claude-compatible gateways). Sends only the sanitised text, no tools. Returns (reply, error)."""
    try:
        key = _advisor_key(cfg)
    except (OSError, KeyError) as e:
        return "", "advisor key not available (%s)" % type(e).__name__
    url = cfg["url"].rstrip("/")
    if not url.endswith("/messages"):
        url += "/v1/messages"
    body = {"model": cfg["model"], "max_tokens": int(cfg.get("max_tokens", 4000)), "system": ADVISOR_SYSTEM,
            "messages": [{"role": "user", "content": text}]}
    # An explicit User-Agent is required: some gateways sit behind Cloudflare, which rejects urllib's default one (error 1010, HTTP 403).
    head = {"Content-Type": "application/json", "anthropic-version": "2023-06-01", "User-Agent": cfg.get("user_agent", "aicf-cad-advisor/1.0")}
    head["x-api-key" if cfg.get("auth") == "x-api-key" else "Authorization"] = key if cfg.get("auth") == "x-api-key" else "Bearer " + key
    try:
        r = urllib.request.Request(url, json.dumps(body).encode(), head)
        d = json.loads(urllib.request.urlopen(r, timeout=int(cfg.get("timeout", 180))).read())
    except urllib.error.HTTPError as e:
        return "", "advisor HTTP %d" % e.code
    except Exception as e:                                   # network, timeout, bad JSON: never leak details that may contain the key
        return "", "advisor request failed (%s)" % type(e).__name__
    parts = [b.get("text", "") for b in d.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
    if not parts or not "".join(parts).strip():
        if d.get("stop_reason") == "max_tokens":              # the model thinks first; a small max_tokens is eaten before any text appears
            return "", "advisor ran out of tokens before writing an answer (raise max_tokens; the model thinks first)"
        return "", "advisor reply had no text"
    return "".join(parts), ""


def ask(req, cfg=None):
    """cfg: mode dryrun|api|anthropic|codex_cli, approve(text)->bool for human approval, denylist[], pseudonyms[], reviewer off|local."""
    cfg = cfg or {}
    mode = cfg.get("mode", "dryrun")
    try:
        text, rep = sanitize(req, cfg)
        if cfg.get("reviewer", "off") == "local":
            try:
                if review_local(text, cfg):
                    raise Blocked("local reviewer flagged a possible leak")
            except Blocked:
                raise
            except Exception as e:
                raise Blocked("reviewer required but unavailable: %s" % e)
        if cfg.get("approve") and not cfg["approve"](text):
            raise Blocked("human declined")
    except Blocked as b:
        audit("blocked", reason=str(b), mode=mode)
        return {"ok": False, "blocked": True, "reason": str(b)}
    audit("outbound", mode=mode, payload=text, report=rep)
    if mode == "dryrun":
        return {"ok": True, "dryrun": True, "reply": "[dry-run: nothing was sent]", "payload": text}
    if mode == "api":
        body = {"model": cfg["model"], "messages": [
            {"role": "system", "content": "You advise on CAD scripting. Reply with concise guidance and, if useful, a short Python snippet. You have no tools and no access to any files."},
            {"role": "user", "content": text}]}
        r = urllib.request.Request(cfg["url"], json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ[cfg.get("key_env", "ADVISOR_API_KEY")]})
        reply = json.loads(urllib.request.urlopen(r, timeout=300).read())["choices"][0]["message"]["content"]
    elif mode == "anthropic":
        reply, err = _ask_anthropic(cfg, text)
        if err:
            audit("error", mode=mode, reason=err)
            return {"ok": False, "reason": err}
    elif mode == "codex_cli":
        if not cfg.get("contained_user"):
            audit("blocked", reason="codex_cli needs contained_user (its read-only sandbox does not stop reads)", mode=mode)
            return {"ok": False, "blocked": True, "reason": "codex_cli refused: no contained Windows user configured"}
        empty = tempfile.mkdtemp(prefix="advisor_")
        out = os.path.join(empty, "reply.txt")
        argv = [cfg["codex_exe"], "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
                "-s", "read-only", "-C", empty, "-o", out, "Answer using only the text below. Do not run any command or read any file.\n\n" + text]
        subprocess.run(argv, timeout=900, capture_output=True)   # to be run AS the contained user by the caller's wrapper
        reply = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
    else:
        return {"ok": False, "reason": "unknown mode"}
    audit("inbound", reply=reply[:4000])
    return {"ok": True, "reply": reply}
