#!/usr/bin/env python3
"""So sanh Wagent vs UFO2 tren cung engine, cung nhiem vu, kiem chung bang trang thai that.
Dung: cmp.py <wagent|ufo> <t1,t2,..> <so_lan> <out.jsonl>"""
import json, os, re, subprocess, sys, time, urllib.request
H = "fios02@10.42.0.225"
ENGINE_LOG = os.environ.get("CMP_ENGINE_LOG") or "/mnt/2d4726e7-046b-47c7-b9a9-d2a9cc0cfc8d/Work/AIComputeFabric/ai-compute-fabric/.demo/logs/engine-pc001.log"
TASKS = {
 "t1": "Mở Notepad và gõ đúng một dòng: Xin chao the gioi",
 "t2": "Mở Calculator và tính 123 + 456, để kết quả hiển thị trên màn hình",
 "t3": "Trang VnExpress đã mở sẵn trong Chrome. Hãy đọc trang, chọn 10 tiêu đề tin mới nhất, rồi mở Notepad và gõ 10 tiêu đề đó, mỗi tiêu đề một dòng, đúng nguyên văn.",
 "t4": "Mở Calculator tính 12 * 34. Sau đó mở Notepad và gõ kết quả tính được (chỉ con số).",
}
TIMEOUT = {"t1": 300, "t2": 300, "t3": 900, "t4": 480}
CAP = int(os.environ.get("CMP_TIMEOUT_MAX", "100000"))
TIMEOUT = {k: min(v, CAP) for k, v in TIMEOUT.items()}

def ssh(cmd, t=120):
    return subprocess.run(["ssh", "-o", "ConnectTimeout=10", H, cmd], capture_output=True, text=True, timeout=t).stdout

def scp_to(local, remote):
    subprocess.run(["scp", "-q", local, H + ":" + remote], check=True, timeout=60)

def win_exists(path):
    return "Y" in ssh('if exist "%s" (echo Y) else (echo N)' % path)

def win_job(cmdline, wait_file=None, timeout=60, task="cmp_job"):
    """Chay lenh trong phien desktop (schtasks /IT) roi cho file danh dau xuat hien."""
    if wait_file: ssh('del "%s" 2>nul' % wait_file)
    ssh('schtasks /Create /TN %s /TR "%s" /SC ONCE /ST 23:59 /IT /F' % (task, cmdline.replace('"', '\\"')))
    ssh("schtasks /Run /TN %s" % task)
    t0 = time.time()
    while wait_file and time.time() - t0 < timeout:
        if win_exists(wait_file): return True
        time.sleep(2)
    return not wait_file

def engine_tokens(off):
    with open(ENGINE_LOG, "rb") as f:
        f.seek(off); seg = f.read().decode("utf-8", "replace")
    toks = [int(m) for m in re.findall(r"new prompt, n_ctx_slot = \d+, n_keep = \d+, task\.n_tokens = (\d+)", seg)]
    return {"requests": len(toks), "prompt_tokens": sum(toks), "max_prompt": max(toks or [0])}

def reset_state():
    for p in ("notepad.exe", "CalculatorApp.exe", "Calculator.exe"):
        ssh("taskkill /F /IM %s 2>nul" % p)
    ssh("del /q C:\\wagent\\sandbox\\tin_*.txt 2>nul & del /q C:\\wagent\\sandbox\\tieu_de*.txt 2>nul")   # file con sot tu lan chay truoc lam nhiem ban phep do (da gap: agent doc lai file cu)
    time.sleep(2)

def open_vne():
    ssh('wmic process where "name=\'chrome.exe\' and commandline like \'%ufo_test%chrome_profile%\'" call terminate >nul 2>&1')
    time.sleep(2)
    win_job(r'cmd /c start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir=C:\ufo_test\chrome_profile --force-renderer-accessibility --no-first-run --new-window https://vnexpress.net', None)
    time.sleep(25)

def run_wagent(task, tk):
    tok = open("/tmp/claude-1000/tok.txt").read().strip()
    def call(p, b=None):
        r = urllib.request.Request("http://127.0.0.1:18765" + p, data=None if b is None else json.dumps(b).encode(), headers={"X-Token": tok, "Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(r, timeout=30))
    call("/api/run", {"url": "http://10.42.0.1:18090/v1", "task": task, "lesson": "none"})
    t0 = time.time(); s = {}
    while time.time() - t0 < TIMEOUT[tk]:
        s = call("/api/state")
        for a in s.get("pending", []): call("/api/approve", {"id": a["id"], "allow": True})
        if s["status"] != "running": break
        time.sleep(2)
    else:
        call("/api/stop", {}); s["status"] = "timeout"
    return s["status"], len(s.get("log", [])), (s.get("summary") or "")[:200]

def run_ufo(task, rid, tk):
    os.makedirs("/tmp/claude-1000/cmp", exist_ok=True)
    rf = "/tmp/claude-1000/cmp/req_%s.txt" % rid; open(rf, "w", encoding="utf-8").write(task)
    scp_to(rf, "C:/ufo_test/run/req_%s.txt" % rid)
    ssh("del C:\\ufo_test\\run\\ufo_%s.done 2>nul" % rid)
    win_job(r"C:\ufo_test\venv\Scripts\python.exe C:\ufo_test\ufo_launch.py %s C:\ufo_test\run\req_%s.txt" % (rid, rid), None)
    t0 = time.time(); status = "timeout"
    while time.time() - t0 < TIMEOUT[tk]:
        if win_exists("C:\\ufo_test\\run\\ufo_%s.done" % rid):
            status = "exited"; break
        time.sleep(3)
    if status == "timeout":
        pid = ssh("type C:\\ufo_test\\run\\ufo_%s.pid 2>nul" % rid).strip()
        if pid.isdigit(): ssh("taskkill /F /T /PID %s" % pid)
    log = ssh("type C:\\ufo_test\\run\\ufo_%s.log 2>nul" % rid)
    return status, log.count("Step") , log[-300:]

def run_uitars(task, rid, tk):
    os.makedirs("/tmp/claude-1000/cmp", exist_ok=True)
    rf = "/tmp/claude-1000/cmp/req_%s.txt" % rid; open(rf, "w", encoding="utf-8").write(task)
    scp_to(rf, "C:/ufo_test/run/req_%s.txt" % rid)
    ssh("del C:\\ufo_test\\run\\uitars_%s.done 2>nul" % rid)
    win_job(r"C:\ufo_test\venv\Scripts\pythonw.exe C:\ufo_test\uitars_run.py %s C:\ufo_test\run\req_%s.txt %d" % (rid, rid, 25 if tk in ("t3", "t4") else 15), None)
    t0 = time.time(); status = "timeout"
    while time.time() - t0 < TIMEOUT[tk]:
        if win_exists("C:\\ufo_test\\run\\uitars_%s.done" % rid):
            status = ssh("type C:\\ufo_test\\run\\uitars_%s.done" % rid).strip() or "exited"; break
        time.sleep(3)
    log = ssh("type C:\\ufo_test\\run\\uitars_%s.jsonl 2>nul" % rid)
    return status, log.count('"step"'), log[-300:]

def run_goose(task, rid, tk):
    os.makedirs("/tmp/claude-1000/cmp", exist_ok=True)
    rf = "/tmp/claude-1000/cmp/req_%s.txt" % rid; open(rf, "w", encoding="utf-8").write(task)
    scp_to(rf, "C:/ufo_test/run/req_%s.txt" % rid)
    ssh("del C:\\ufo_test\\run\\goose_%s.done 2>nul" % rid)
    win_job(r"C:\ufo_test\venv\Scripts\pythonw.exe C:\ufo_test\goose_launch.py %s C:\ufo_test\run\req_%s.txt 25" % (rid, rid), None)
    t0 = time.time(); status = "timeout"
    while time.time() - t0 < TIMEOUT[tk]:
        if win_exists("C:\\ufo_test\\run\\goose_%s.done" % rid):
            status = ssh("type C:\\ufo_test\\run\\goose_%s.done" % rid).strip() or "exited"; break
        time.sleep(3)
    if status == "timeout": ssh("taskkill /F /T /IM goose.exe 2>nul")
    log = ssh("type C:\\ufo_test\\run\\goose_%s.log 2>nul" % rid)
    return status, log.count("tool call") + log.count("─── "), log[-400:]

def verify(tk):
    out = "C:\\ufo_test\\run\\v.json"
    if not win_job(r"C:\wagent\venv\Scripts\python.exe C:\ufo_test\verify.py %s %s" % (tk, out), out, 90, "cmp_verify"): return {"ok": False, "err": "verify timeout"}
    err = ""
    for _ in range(8):                       # file xuat hien luc dang ghi -> doc lai neu chua parse duoc
        try: return json.loads(ssh("type %s" % out))
        except Exception as e: err = str(e); time.sleep(2)
    return {"ok": False, "err": err}

def main():
    system, tks, n, outp = sys.argv[1], sys.argv[2].split(","), int(sys.argv[3]), sys.argv[4]
    for tk in tks:
        for i in range(1, n + 1):
            rid = "%s_%s_%d_%d" % (system, tk, i, int(time.time()) % 100000)
            reset_state()
            if tk == "t3": open_vne()
            off = os.path.getsize(ENGINE_LOG); t0 = time.time()
            try:
                st, steps, info = run_wagent(TASKS[tk], tk) if system == "wagent" else (run_uitars(TASKS[tk], rid, tk) if system == "uitars" else (run_goose(TASKS[tk], rid, tk) if system == "goose" else run_ufo(TASKS[tk], rid, tk)))
            except Exception as e:
                st, steps, info = "driver_error", 0, repr(e)[:200]
            dur = time.time() - t0; tok = engine_tokens(off)
            v = verify(tk)
            rec = {"system": system, "task": tk, "rep": i, "status": st, "steps_or_marks": steps, "seconds": round(dur, 1), **tok, "verified": v, "ok": bool(v.get("ok")), "info": info}
            open(outp, "a", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(system, tk, i, "OK" if rec["ok"] else "FAIL", st, round(dur), "s", tok["prompt_tokens"], "ptok", flush=True)
main()
