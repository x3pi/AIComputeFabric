"""Thu vien BAI HOC: luu lai cach agent da lam thanh cong mot nhiem vu, de lan sau nhiem vu tuong tu duoc goi y lai
(few-shot) ma khong phai sua ma. Bai hoc chi la GOI Y chen vao tin nhan nhiem vu khi KHOP; khong doi prompt he thong,
khong bypass guard/duyet/sandbox. File JSON o thu muc rieng ngoai sandbox (agent khong ghi duoc vao day)."""
import json, os, re, time, unicodedata

ALLOWED_TOOLS = {"open_app", "observe", "read_page", "set_text", "click", "read_file", "write_file", "list_dir",
                 "move_file", "copy_file", "close_window"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_STOP = set("""va roi xong thi done mo cac la cua trong tu den cho voi mot nhung nay do khi neu hay de duoc vao ra o
the a of to and in on for hoac cung nua sau truoc bang theo moi tung tat ca""".split())
MAX_STEPS, MAX_STR = 20, 200
AUTO_SHARED, AUTO_SCORE = 5, 0.8      # tu dong chen chi khi khop RAT cao (mo hinh nho nhay voi chu them vao prompt)
SECRETISH = re.compile(r"password|passwd|m[aậ]t kh[aẩ]u|api[_ -]?key|token|secret", re.I)

def tokens(text):
    s = unicodedata.normalize("NFD", str(text or "").lower().replace("đ", "d"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return [t for t in re.findall(r"[a-z0-9]+", s) if t not in _STOP and len(t) > 1]

_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_FILENAME = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]{1,5}\b")

def intent_tokens(text):
    """Tu chi Y DINH: bo URL, ten file co duoi, so (tham so thay doi giua cac nhiem vu cung loai)."""
    t = _FILENAME.sub(" ", _URL.sub(" ", str(text or "")))
    return [x for x in tokens(t) if not x.isdigit()]

def keywords(task, extra=()):
    seen, out = set(), []
    for t in intent_tokens(task) + [x for e in extra for x in intent_tokens(e)]:
        if t not in seen:
            seen.add(t); out.append(t)
    return out[:40]

def slugify(title):
    s = "-".join(tokens(title))[:48].strip("-")
    return s or "bai-hoc"

def _clean_step(act):
    """Chuan hoa 1 buoc de luu: bo du lieu nhay cam / qua dai. Tra ve None neu khong nen luu."""
    tool, args = act.get("tool"), dict(act.get("args") or {})
    if tool not in ALLOWED_TOOLS:
        return None
    out = {}
    for k, v in args.items():
        if isinstance(v, (int, float, bool)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v if len(v) <= MAX_STR else v[:MAX_STR]
        else:
            return None
    if tool == "set_text" and SECRETISH.search(str(out.get("text", ""))):
        return None                                           # khong luu van ban giong mat khau
    if tool == "write_file" and len(str(args.get("content", ""))) > 80:
        out["content"] = "<noi dung tao tu du lieu doc duoc>"   # noi dung dai thuong la du lieu rieng/khong tin cay
    if tool == "click" and "name" not in out and act.get("target"):
        out = {"name": str(act["target"]).split("\t")[0][:80]}   # id doi lan moi; ten nut on dinh hon
    if tool in ("set_text", "read_text") and act.get("target"):
        out["ten_o_nhap"] = str(act["target"])[:80]
    return {"tool": tool, "args": out}

def from_run(task, actions, title="", extra_keywords=()):
    """Tao bai hoc tu cac hanh dong THANH CONG cua mot lan chay. Raise ValueError neu khong du dieu kien."""
    steps = []
    for a in actions:
        st = _clean_step(a)
        if st and not (steps and steps[-1] == st):            # bo lap lien tiep
            steps.append(st)
    if not any(s["tool"] != "observe" for s in steps):
        raise ValueError("lan chay khong co hanh dong nao de hoc")
    if len(steps) > MAX_STEPS:
        raise ValueError("qua nhieu buoc (%d > %d): chia nho nhiem vu" % (len(steps), MAX_STEPS))
    title = (title or task).strip()[:120]
    return {"id": slugify(title), "title": title, "task": str(task)[:400], "keywords": keywords(task, [title, *extra_keywords]),
            "steps": steps, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "uses": 0}

def validate(les):
    if not isinstance(les, dict) or not _SLUG.match(str(les.get("id", ""))):
        return False
    if not isinstance(les.get("keywords"), list) or not all(isinstance(k, str) for k in les["keywords"]):
        return False
    st = les.get("steps")
    if not isinstance(st, list) or not 1 <= len(st) <= MAX_STEPS:
        return False
    return all(isinstance(s, dict) and s.get("tool") in ALLOWED_TOOLS and isinstance(s.get("args", {}), dict) for s in st)

def unique_id(dir_, les):
    """Tranh ghi de bai hoc cu: neu id da co thi them hau to -2, -3..."""
    base, n = les["id"], 1
    while os.path.exists(os.path.join(dir_, les["id"] + ".json")):
        n += 1
        les = dict(les, id="%s-%d" % (base[:60], n))
    return les

def summary(les):
    return {"id": les["id"], "title": les["title"], "task": les.get("task", ""), "steps": len(les["steps"])}

def save(dir_, les):
    if not validate(les):
        raise ValueError("bai hoc khong hop le")
    os.makedirs(dir_, exist_ok=True)
    path = os.path.join(dir_, les["id"] + ".json")
    json.dump(les, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return path

def load_all(dir_):
    out = []
    try:
        names = sorted(os.listdir(dir_))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json"):
            try:
                les = json.load(open(os.path.join(dir_, n), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if validate(les) and les["id"] + ".json" == n:     # ten file phai khop id (chong path la)
                out.append(les)
    return out

def delete(dir_, lesson_id):
    if not _SLUG.match(str(lesson_id)):
        raise ValueError("id khong hop le")
    p = os.path.join(dir_, lesson_id + ".json")
    if not os.path.isfile(p):
        raise FileNotFoundError(lesson_id)
    os.remove(p)

def score(task, les):
    """(so_tu_chung, do_phu): do_phu = ti le cac tu cua NHIEM VU MOI duoc bai hoc bao phu (khong chia cho so tu khoa cua bai hoc:
    bai hoc co nhieu tu rieng nhu ten file/dia chi se bi phat oan)."""
    t, k = set(intent_tokens(task)), set(les["keywords"])
    shared = len(t & k)
    return shared, (shared / len(t) if t else 0.0)

def best_match(task, lessons):
    """Bai hoc khop nhat neu du nguong TU DONG, nguoc lai None. Khop thap hon thi nguoi dung chon bai hoc thu cong tren giao dien."""
    best, best_key = None, (0.0, 0)
    for les in lessons:
        shared, ratio = score(task, les)
        if shared >= AUTO_SHARED and ratio >= AUTO_SCORE and (ratio, shared) > best_key:
            best, best_key = les, (ratio, shared)
    return best

def lesson_note(les):
    lines = ["", "BAI HOC THAM KHAO: nhiem vu tuong tu ('%s') da hoan thanh truoc day theo cac buoc sau. Chi de THAM KHAO: "
                 "doi ten file/dia chi theo nhiem vu moi, quan sat man hinh that, khong lam theo may moc." % les["title"]]
    for i, s in enumerate(les["steps"], 1):
        lines.append("%d. %s %s" % (i, s["tool"], json.dumps(s.get("args", {}), ensure_ascii=False)))
    note = "\n".join(lines)
    return note if len(note) <= 1800 else note[:1800] + "\n..."
