"""Vong lap UI-TARS-1.5 (thi giac) tren Windows: chup man hinh -> model -> toa do -> pyautogui. Chay trong phien desktop (schtasks /IT).
usage: python uitars_run.py <rid> <request_file> [max_steps]
Khong qua guard cua Wagent (chi dung cho phep do co kiem soat). Log: C:\\ufo_test\\run\\uitars_<rid>.jsonl, .done"""
import base64, io, json, math, re, sys, time
import pyautogui, requests
from PIL import Image

URL = "http://10.42.0.1:18093/v1/chat/completions"
rid, reqfile = sys.argv[1], sys.argv[2]
max_steps = int(sys.argv[3]) if len(sys.argv) > 3 else 15
task = open(reqfile, encoding="utf-8").read().strip()
pyautogui.FAILSAFE = False; pyautogui.PAUSE = 0.3
LOG = r"C:\ufo_test\run\uitars_%s.jsonl" % rid

ACTION_SPACE = """click(start_box='<|box_start|>(x1,y1)<|box_end|>')
left_double(start_box='<|box_start|>(x1,y1)<|box_end|>')
right_single(start_box='<|box_start|>(x1,y1)<|box_end|>')
drag(start_box='<|box_start|>(x1,y1)<|box_end|>', end_box='<|box_start|>(x3,y3)<|box_end|>')
hotkey(key='')
type(content='') #If you want to submit your input, use "\\n" at the end of `content`.
scroll(start_box='<|box_start|>(x1,y1)<|box_end|>', direction='down or up or right or left')
wait() #Sleep for 5s and take a screenshot to check for any changes.
finished()"""
PROMPT = """You are a GUI agent. You are given a task and your action history, with screenshots. You need to perform the next action to complete the task. 

## Output Format
```
Thought: ...
Action: ...
```

## Action Space
%s

## Note
- Use English in `Thought` part.
- Write a small plan and finally summarize your next action (with its target element) in one sentence in `Thought` part.

## User Instruction
%s
"""

def smart_resize(h, w, f=28, minp=100 * 28 * 28, maxp=16384 * 28 * 28):
    hb, wb = max(f, round(h / f) * f), max(f, round(w / f) * f)
    if hb * wb > maxp:
        b = math.sqrt(h * w / maxp); hb, wb = math.floor(h / b / f) * f, math.floor(w / b / f) * f
    elif hb * wb < minp:
        b = math.sqrt(minp / (h * w)); hb, wb = math.ceil(h * b / f) * f, math.ceil(w * b / f) * f
    return hb, wb

def shot():
    im = pyautogui.screenshot(); W, H = im.size
    nh, nw = smart_resize(H, W)
    buf = io.BytesIO(); im.resize((nw, nh)).save(buf, "PNG")
    return W, H, nw, nh, base64.b64encode(buf.getvalue()).decode()

def pt(s, W, H, nw, nh):
    m = re.findall(r"\(?\s*(\d+)\s*,\s*(\d+)\s*\)?", s or "")
    if not m: return None
    x, y = int(m[0][0]), int(m[0][1])
    return int(x / nw * W), int(y / nh * H)

def paste(text):
    import tkinter
    r = tkinter.Tk(); r.withdraw(); r.clipboard_clear(); r.clipboard_append(text); r.update()
    pyautogui.hotkey("ctrl", "v"); time.sleep(0.4); r.destroy()

def do(action, W, H, nw, nh):
    m = re.match(r"\s*(\w+)\((.*)\)\s*$", action, re.S)
    if not m: return "khong_parse_duoc"
    fn, args = m.group(1), m.group(2)
    sb = re.search(r"start_box='([^']*)'", args); eb = re.search(r"end_box='([^']*)'", args)
    p = pt(sb.group(1), W, H, nw, nh) if sb else None
    if fn == "click" and p: pyautogui.click(*p)
    elif fn == "left_double" and p: pyautogui.doubleClick(*p)
    elif fn == "right_single" and p: pyautogui.rightClick(*p)
    elif fn == "drag" and p and eb:
        q = pt(eb.group(1), W, H, nw, nh); pyautogui.moveTo(*p); pyautogui.dragTo(*q, duration=0.5, button="left")
    elif fn == "hotkey":
        k = re.search(r"key='([^']*)'", args); keys = (k.group(1) if k else "").lower().split()
        if keys: pyautogui.hotkey(*keys)
    elif fn == "type":
        c = re.search(r"content='(.*)'", args, re.S); txt = c.group(1) if c else ""
        txt = txt.replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"')
        enter = txt.endswith("\n"); txt = txt.rstrip("\n")
        if txt: paste(txt)
        if enter: pyautogui.press("enter")
    elif fn == "scroll" and p:
        d = re.search(r"direction='(\w+)'", args); pyautogui.moveTo(*p)
        pyautogui.scroll(-600 if (d and d.group(1) == "down") else 600)
    elif fn == "wait": time.sleep(5)
    elif fn == "finished": return "finished"
    else: return "khong_ho_tro:" + fn
    return "ok"

hist = []; status = "max_steps"
for step in range(1, max_steps + 1):
    W, H, nw, nh, b64 = shot()
    msgs = [{"role": "user", "content": PROMPT % (ACTION_SPACE, task)}]
    for t in hist[-6:]: msgs.append({"role": "assistant", "content": t})
    msgs.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]})
    t0 = time.time()
    try:
        r = requests.post(URL, json={"model": "ui-tars", "messages": msgs, "temperature": 0, "max_tokens": 400}, timeout=300)
        r.raise_for_status(); txt = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        open(LOG, "a", encoding="utf-8").write(json.dumps({"step": step, "error": repr(e)[:300]}) + "\n"); status = "llm_error"; break
    am = re.search(r"Action:\s*(.+)", txt, re.S); action = am.group(1).strip().splitlines()[0] if am else ""
    res = do(action, W, H, nw, nh) if action else "khong_co_action"
    open(LOG, "a", encoding="utf-8").write(json.dumps({"step": step, "sec": round(time.time() - t0, 1), "raw": txt[:400], "action": action, "result": res}, ensure_ascii=False) + "\n")
    hist.append(txt)
    if res == "finished": status = "finished"; break
    time.sleep(1.5)
open(r"C:\ufo_test\run\uitars_%s.done" % rid, "w").write(status)
