"""Benchmark kich ban: moi kich ban co setup, task, verify (kiem tra TRANG THAI THAT, khong tin summary)."""
import argparse, json, os, shutil, sys, time

def _w(sb, rel, txt):
    p = os.path.join(sb, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(txt)

def _r(sb, rel):
    try: return open(os.path.join(sb, rel), encoding="utf-8").read()
    except OSError: return None

def _ls(sb, sub=""):
    p = os.path.join(sb, sub)
    return sorted(os.listdir(p)) if os.path.isdir(p) else []

# 1. Notepad + file
def s1_setup(sb): pass
def s1_ver(sb): return (_r(sb, "ghichu.txt") or "").strip() == "Xin chao"
# 2. Phan loai file theo duoi
def s2_setup(sb):
    for n in ["a.txt", "b.txt", "x.jpg", "y.jpg", "z.pdf"]: _w(sb, "in/" + n, n)
def s2_ver(sb):
    return (_ls(sb, "out/txt") == ["a.txt", "b.txt"] and _ls(sb, "out/jpg") == ["x.jpg", "y.jpg"]
            and _ls(sb, "out/pdf") == ["z.pdf"] and _ls(sb, "in") == [])
# 3. Doi ten: khoang trang -> gach duoi, chu thuong
def s3_setup(sb):
    for n in ["Bao Cao Q1.txt", "Ke Hoach 2027.txt", "ghi chu.txt"]: _w(sb, "docs/" + n, n)
def s3_ver(sb): return _ls(sb, "docs") == ["bao_cao_q1.txt", "ghi_chu.txt", "ke_hoach_2027.txt"]
# 4. Tong cot CSV
def s4_setup(sb): _w(sb, "sales.csv", "ngay,doanh_thu\n2026-10-01,120\n2026-10-02,75\n2026-10-03,305\n2026-10-04,50\n")
def s4_ver(sb): return (_r(sb, "tong.txt") or "").strip().split()[-1:] == ["550"]
# 5. Sao luu *.txt
def s5_setup(sb):
    for n in ["n1.txt", "n2.txt", "img.png"]: _w(sb, "proj/" + n, n)
def s5_ver(sb): return _ls(sb, "backup") == ["n1.txt", "n2.txt"] and _ls(sb, "proj") == ["img.png", "n1.txt", "n2.txt"]
# 6. May tinh: GUI that
def s6_setup(sb): pass
def s6_ver(sb): return (_r(sb, "ketqua.txt") or "").strip() == "42"

SCENARIOS = {
    "notepad_ghi_file": (s1_setup, "Mo notepad, go chu 'Xin chao' vao o soan thao, sau do ghi file ghichu.txt trong sandbox voi noi dung Xin chao, roi done", s1_ver),
    "phan_loai_file": (s2_setup, "Thu muc in/ co cac file. Chuyen moi file vao out/<duoi>/ (vd a.txt vao out/txt/a.txt, x.jpg vao out/jpg/x.jpg). Dung list_dir de xem. Xong thi done", s2_ver),
    "doi_ten_hang_loat": (s3_setup, "Trong thu muc docs/, doi ten moi file thanh chu thuong va thay dau cach bang gach duoi (tao ban moi bang move_file den ten moi). Xong thi done", s3_ver),
    "tong_csv": (s4_setup, "Doc sales.csv, tinh tong cot doanh_thu, ghi ket qua (chi mot con so) vao tong.txt. Xong thi done", s4_ver),
    "sao_luu_txt": (s5_setup, "Sao chep (copy_file) cac file .txt trong proj/ sang backup/ (giu ten). Khong dong vao file khac. Xong thi done", s5_ver),
    "may_tinh_gui": (s6_setup, "Mo calc, dung may tinh bam 1 2 + 3 0 = de tinh 12+30 (observe de lay id cac nut), doc ket qua tren man hinh, ghi con so ket qua vao ketqua.txt. Xong thi done", s6_ver),
}

APP_IMAGES = ["notepad.exe", "CalculatorApp.exe", "calc.exe", "mspaint.exe"]

def cleanup_apps(run=None):
    """Dong cac app ma agent co the da mo (kich ban truoc de lai cua so/hop thoai 'Save changes' lam nhieu kich ban sau)."""
    import subprocess
    run = run or subprocess.run
    for img in APP_IMAGES:
        run(["taskkill", "/F", "/IM", img], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

class BenchLocked(Exception):
    pass

def acquire_lock(path):
    """Chi cho 1 benchmark chay mot luc (hai ban chay chung se dung sandbox/cua so cua nhau). Tra ve ham giai phong."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise BenchLocked("benchmark khac dang chay (khoa: %s)" % path)
    os.write(fd, str(os.getpid()).encode()); os.close(fd)
    return lambda: os.path.exists(path) and os.remove(path)

def main():
    from .llm import LLM
    from .audit import Audit
    from .loop import Agent
    from .approver import DenyAll
    from .skills.uia import UIA
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True); p.add_argument("--model", default=""); p.add_argument("--key", default="")
    p.add_argument("--n", type=int, default=3); p.add_argument("--only", default="")
    p.add_argument("--out", default=r"C:\wagent\bench.jsonl"); p.add_argument("--work", default=r"C:\wagent\bench_sb")
    p.add_argument("--max-steps", type=int, default=20); p.add_argument("--no-think", action="store_true")
    p.add_argument("--lock", default=r"C:\wagent\bench.lock")
    a = p.parse_args()
    release = acquire_lock(a.lock)
    import atexit; atexit.register(release)
    names = [n for n in SCENARIOS if not a.only or n in a.only.split(",")]
    uia = UIA()
    for name in names:
        setup, task, ver = SCENARIOS[name]
        for i in range(a.n):
            cleanup_apps()
            sb = os.path.join(a.work, name); shutil.rmtree(sb, ignore_errors=True); os.makedirs(sb)
            setup(sb)
            llm = LLM(a.url, a.model, a.key, no_think=a.no_think)
            audit = Audit(os.path.join(a.work, "audit-%s-%d.jsonl" % (name, i)))
            t0 = time.time(); steps = []
            try:
                st, sm = Agent(llm, uia, audit, sb, a.max_steps, log=lambda m: steps.append(m), approver=DenyAll(), max_seconds=300).run(task)
            except Exception as e:
                st, sm = "crash", "%s: %s" % (type(e).__name__, e)
            ok = bool(ver(sb))
            rec = dict(scenario=name, run=i, agent_status=st, verified=ok, steps=len(steps),
                       tokens=llm.tokens, seconds=round(time.time() - t0, 1), summary=sm[:200])
            open(a.out, "a", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(json.dumps(rec, ensure_ascii=False), flush=True)
            try: uia.close_window()
            except Exception: pass

if __name__ == "__main__":
    main()
