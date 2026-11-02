"""Kiem chung TRANG THAI THAT sau khi mot he agent chay xong (khong tin summary cua model).
Chay trong phien desktop (schtasks /IT):  python verify.py <t1|t2|t3|t4> <out.json>
t1: Notepad co dung 1 dong 'Xin chao the gioi' | t2: Calculator hien 579 | t3: Notepad co >=10 tieu de co that tren trang VnExpress dang mo
t4: Calculator hien 408 VA Notepad chua 408."""
import json, re, sys, time, unicodedata
import uiautomation as ua

def notepad_text():
    for w in ua.GetRootControl().GetChildren():
        if w.ClassName == "Notepad" or w.Name.endswith("Notepad"):
            for c in (w.EditControl(searchDepth=6), w.DocumentControl(searchDepth=6)):
                try:
                    if c.Exists(0, 0):
                        try: return c.GetValuePattern().Value
                        except Exception: return c.GetTextPattern().DocumentRange.GetText(-1)
                except Exception:
                    continue
    return None

def calc_display():
    for w in ua.GetRootControl().GetChildren():
        if w.Name == "Calculator":
            c = w.TextControl(AutomationId="CalculatorResults", searchDepth=12)
            if c.Exists(0, 0):
                m = re.search(r"Display is\s*([\d.,\-]+)", c.Name)
                return (m.group(1) if m else c.Name).replace(",", "")
    return None

def norm(s):
    s = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", s or "")
    return re.sub(r"\s+", " ", s).strip().lower()

def noacc(s):
    s = unicodedata.normalize("NFD", (s or "").replace("\u0111", "d").replace("\u0110", "D"))
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

def page_titles():
    out, seen = [], set()
    for w in ua.GetRootControl().GetChildren():
        if w.ClassName.startswith("Chrome_WidgetWin") and "vnexpress" in w.Name.lower():
            stack, t0 = [w], time.time()
            while stack and time.time() - t0 < 20:
                c = stack.pop()
                try:
                    if c.ControlTypeName == "HyperlinkControl":
                        n = re.sub(r"\s+", " ", c.Name or "").strip()
                        if len(n) >= 15 and n.lower() not in seen:
                            seen.add(n.lower()); out.append(n)
                    stack.extend(reversed(c.GetChildren()))
                except Exception:
                    continue
            break
    return out

def main():
    task, outp = sys.argv[1], sys.argv[2]
    r = {"task": task}
    if task == "t1":
        t = notepad_text(); r["text"] = t
        r["ok"] = t is not None and norm(t) == "xin chao the gioi" and len([x for x in t.splitlines() if x.strip()]) == 1
        r["lenient_ok"] = t is not None and noacc(norm(t)) == "xin chao the gioi" and len([x for x in t.splitlines() if x.strip()]) == 1
    elif task == "t2":
        d = calc_display(); r["display"] = d; r["ok"] = d == "579"
    elif task == "t4":
        d, t = calc_display(), notepad_text(); r["display"], r["text"] = d, t
        r["ok"] = d == "408" and t is not None and "408" in t
    elif task == "t3":
        t = notepad_text(); truth = page_titles(); tn = [norm(x) for x in truth]
        lines = [norm(x) for x in (t or "").splitlines() if x.strip()]
        matched, bad = set(), []
        for l in lines:
            hit = next((i for i, x in enumerate(tn) if l == x or (len(l) >= 20 and (l in x or x in l))), None)
            if hit is None: bad.append(l[:80])
            else: matched.add(hit)
        r.update(n_lines=len(lines), n_truth=len(truth), n_matched=len(matched), unmatched=bad[:5])
        r["ok"] = len(matched) >= 10 and len(bad) <= 2
    json.dump(r, open(outp, "w", encoding="utf-8"), ensure_ascii=False)

main()
