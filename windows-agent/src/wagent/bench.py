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

# ---- bo HELD-OUT: nhiem vu moi, KHONG dung de tinh chinh agent ----
def h1_setup(sb): pass
def h1_ver(sb): return [_r(sb, "notes/%s.txt" % n) for n in ("mot", "hai", "ba")] == ["mot", "hai", "ba"] and _ls(sb, "notes") == ["ba.txt", "hai.txt", "mot.txt"]
def h2_setup(sb):
    for n in ["bao_cao_q1.txt", "bao_cao_q2.txt", "ghi_chu.txt", "du_lieu.csv"]: _w(sb, "data/" + n, n)
def h2_ver(sb): return _ls(sb, "data/reports") == ["bao_cao_q1.txt", "bao_cao_q2.txt"] and _ls(sb, "data") == ["du_lieu.csv", "ghi_chu.txt", "reports"]
def h3_setup(sb):
    for n in ["a.txt", "b.txt", "c.log"]: _w(sb, "docs/" + n, n)
def h3_ver(sb): return _ls(sb, "docs") == ["a.md", "b.md", "c.log"]
def h4_setup(sb): _w(sb, "log.txt", "\n".join("dong %d" % i for i in range(1, 8)) + "\n")
def h4_ver(sb): return (_r(sb, "count.txt") or "").strip() == "7"
def h5_setup(sb): _w(sb, "people.csv", "name,age\nAn,25\nBinh,41\nChi,30\nDung,35\nEm,19\n")
def h5_ver(sb): return [x.strip() for x in (_r(sb, "out.txt") or "").strip().splitlines()] == ["Binh", "Dung"]
def h6_setup(sb): pass
def h6_ver(sb): return [_r(sb, "2026/0%d/note.txt" % n) for n in (1, 2, 3)] == ["thang 1", "thang 2", "thang 3"]

HELDOUT = {
    "h_ghi_ba_file": (h1_setup, "Tao 3 file trong thu muc notes/: mot.txt noi dung mot, hai.txt noi dung hai, ba.txt noi dung ba (noi dung chi la chu do, khong them gi). Xong thi done", h1_ver),
    "h_gom_bao_cao": (h2_setup, "Trong data/, chuyen tat ca file co ten bat dau bang bao_cao_ vao data/reports/. Cac file khac giu nguyen. Xong thi done", h2_ver),
    "h_doi_duoi": (h3_setup, "Trong docs/, doi duoi .txt thanh .md cho cac file .txt (file duoi khac giu nguyen). Xong thi done", h3_ver),
    "h_dem_dong": (h4_setup, "Dem so dong cua log.txt, ghi chi con so do vao count.txt. Xong thi done", h4_ver),
    "h_loc_tuoi": (h5_setup, "Doc people.csv, ghi vao out.txt ten nhung nguoi co tuoi lon hon 30, moi ten mot dong, theo thu tu xuat hien. Xong thi done", h5_ver),
    "h_cay_thang": (h6_setup, "Tao 3 file: 2026/01/note.txt noi dung 'thang 1', 2026/02/note.txt noi dung 'thang 2', 2026/03/note.txt noi dung 'thang 3'. Xong thi done", h6_ver),
}

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

# ---- bo HELD-OUT 2: tao SAU khi thay ket qua held-out 1, de kiem tra khong-thien-vi ----
def g1_setup(sb): pass
def g1_ver(sb): return _r(sb, "reports/ketqua_thang_hai.txt") == "ok" and _r(sb, "reports/ketqua_thang_ba.txt") == "xong" and len(_ls(sb, "reports")) == 2
def g2_setup(sb):
    for n in ["hoa_don_01.pdf", "hoa_don_02.pdf", "thu_moi.txt"]: _w(sb, "inbox/" + n, n)
def g2_ver(sb): return _ls(sb, "archive/2026") == ["hoa_don_01.pdf", "hoa_don_02.pdf"] and _ls(sb, "inbox") == ["thu_moi.txt"]
def g3_setup(sb): _w(sb, "config.ini", "[a]\nx=1\n")
def g3_ver(sb): return _r(sb, "backup/config.ini.bak") == "[a]\nx=1\n" and _r(sb, "config.ini") == "[a]\nx=1\n"
def g4_setup(sb):
    for n, v in (("a.txt", 10), ("b.txt", 20), ("c.txt", 12)): _w(sb, "nums/" + n, "%d\n" % v)
def g4_ver(sb): return (_r(sb, "sum.txt") or "").strip() == "42"
def g5_setup(sb):
    for n in ["a.md", "b.md", "c.md", "d.txt"]: _w(sb, "docs/" + n, n)
def g5_ver(sb): return (_r(sb, "count.txt") or "").strip() == "3"
def g6_setup(sb): _w(sb, "todo.txt", "mua sua\ngiat do\ngoi dien\n")
def g6_ver(sb): return (_r(sb, "first.txt") or "").strip() == "mua sua"

HELDOUT2 = {
    "g_ten_dai": (g1_setup, "Tao file reports/ketqua_thang_hai.txt noi dung ok va reports/ketqua_thang_ba.txt noi dung xong. Xong thi done", g1_ver),
    "g_chuyen_hoa_don": (g2_setup, "Trong inbox/, chuyen cac file hoa don (ten bat dau bang hoa_don) vao archive/2026/. File khac giu nguyen. Xong thi done", g2_ver),
    "g_sao_cau_hinh": (g3_setup, "Sao luu config.ini thanh backup/config.ini.bak, giu nguyen ban goc config.ini. Xong thi done", g3_ver),
    "g_tong_so": (g4_setup, "Moi file trong nums/ chua mot so. Cong tat ca cac so lai, ghi tong (chi mot con so) vao sum.txt. Xong thi done", g4_ver),
    "g_dem_md": (g5_setup, "Dem so file .md trong docs/, ghi chi con so vao count.txt. Xong thi done", g5_ver),
    "g_dong_dau": (g6_setup, "Doc todo.txt, ghi dong dau tien cua no vao first.txt. Xong thi done", g6_ver),
}

# ---- bo HELD-OUT 3: viet SAU khi chot cai tien, chi chay mot lan cho so lieu khach quan ----
def k1_setup(sb): pass
def k1_ver(sb): return [_r(sb, "sao_luu/%s.txt" % n) for n in ("ban_ghi_hai", "ban_ghi_ba", "ban_ghi_bon")] == ["a", "b", "c"] and len(_ls(sb, "sao_luu")) == 3
def k2_setup(sb):
    for n in ["x.log", "y.log", "main.py"]: _w(sb, "app/" + n, n)
def k2_ver(sb): return _ls(sb, "app/logs") == ["x.log", "y.log"] and _ls(sb, "app") == ["logs", "main.py"]
def k3_setup(sb):
    for n in ["img1.png", "img2.png"]: _w(sb, "pics/" + n, n)
def k3_ver(sb): return _ls(sb, "pics") == ["anh_img1.png", "anh_img2.png"]
def k4_setup(sb): _w(sb, "menu.txt", "pho\ncom tam\nbun cha\nche\n")
def k4_ver(sb): return (_r(sb, "third.txt") or "").strip() == "bun cha"
def k5_setup(sb):
    _w(sb, "a.txt", "xin\n"); _w(sb, "b.txt", "chao\n")
def k5_ver(sb): return [x.strip() for x in (_r(sb, "all.txt") or "").strip().splitlines()] == ["xin", "chao"]
def k6_setup(sb):
    _w(sb, "cfg/a.ini", "A"); _w(sb, "cfg/b.ini", "B"); _w(sb, "cfg/readme.txt", "R")
def k6_ver(sb): return _ls(sb, "cfg/backup") == ["a.ini", "b.ini"] and _ls(sb, "cfg") == ["a.ini", "b.ini", "backup", "readme.txt"]

# ---- bo HELD-OUT 4 (viet SAU khi sua v4; chua tung dung de chinh) ----
def m1_setup(sb):
    for n in ["Hoa Don Thang Ba.txt", "Bien Ban Hop.txt", "Thu Moi.txt"]: _w(sb, "van_ban/" + n, n)
def m1_ver(sb): return _ls(sb, "van_ban") == ["bien_ban_hop.txt", "hoa_don_thang_ba.txt", "thu_moi.txt"]
def m2_setup(sb): pass
def m2_ver(sb): return (_r(sb, "kq.txt") or "").strip() == "112"
def m3_setup(sb): pass
def m3_ver(sb): return (_r(sb, "kq.txt") or "").strip() == "72"
HELDOUT4 = {
    "m_doi_ten_van_ban": (m1_setup, "Trong thu muc van_ban/, doi ten moi file thanh chu thuong va thay dau cach bang gach duoi (tao ban moi bang move_file den ten moi). Xong thi done", m1_ver),
    "m_tinh_cong": (m2_setup, "Mo calc, dung may tinh bam 4 5 + 6 7 = (observe de lay id cac nut), doc ket qua tren man hinh, ghi con so ket qua vao kq.txt. Xong thi done", m2_ver),
    "m_tinh_nhan": (m3_setup, "Mo calc, dung may tinh bam 9 x 8 = (nut nhan la 'Multiply by'; observe de lay id cac nut), doc ket qua tren man hinh, ghi con so ket qua vao kq.txt. Xong thi done", m3_ver),
}

HELDOUT3 = {
    "k_ten_ban_ghi": (k1_setup, "Tao 3 file trong sao_luu/: ban_ghi_hai.txt noi dung a, ban_ghi_ba.txt noi dung b, ban_ghi_bon.txt noi dung c. Xong thi done", k1_ver),
    "k_gom_log": (k2_setup, "Chuyen tat ca file .log trong app/ vao app/logs/. File khac giu nguyen. Xong thi done", k2_ver),
    "k_them_tien_to": (k3_setup, "Doi ten moi file trong pics/ bang cach them tien to anh_ truoc ten (vd img1.png thanh anh_img1.png). Xong thi done", k3_ver),
    "k_dong_thu_ba": (k4_setup, "Lay dong thu ba cua menu.txt ghi vao third.txt. Xong thi done", k4_ver),
    "k_gop_file": (k5_setup, "Gop noi dung a.txt roi b.txt thanh all.txt, moi file mot dong. Xong thi done", k5_ver),
    "k_sao_ini": (k6_setup, "Sao chep cac file .ini trong cfg/ vao cfg/backup/ giu nguyen ten. Khong dong vao file khac. Xong thi done", k6_ver),
}

ALL_SETS = lambda: {"dev": SCENARIOS, "held": HELDOUT}

def main():
    from .llm import LLM
    from .audit import Audit
    from .loop import Agent, Improve
    from .approver import DenyAll
    from .skills.uia import UIA
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True); p.add_argument("--model", default=""); p.add_argument("--key", default="")
    p.add_argument("--n", type=int, default=3); p.add_argument("--only", default="")
    p.add_argument("--out", default=r"C:\wagent\bench.jsonl"); p.add_argument("--work", default=r"C:\wagent\bench_sb")
    p.add_argument("--max-steps", type=int, default=20); p.add_argument("--no-think", action="store_true")
    p.add_argument("--set", default="dev", choices=["dev", "held", "held2", "held3", "held4", "all"]); p.add_argument("--baseline", action="store_true", help="tat moi cai tien cua agent")
    p.add_argument("--temp", type=float, default=0.0); p.add_argument("--tag", default="")
    p.add_argument("--lock", default=r"C:\wagent\bench.lock")
    a = p.parse_args()
    release = acquire_lock(a.lock)
    import atexit; atexit.register(release)
    pool = {**SCENARIOS, **HELDOUT, **HELDOUT2, **HELDOUT3, **HELDOUT4} if a.set == "all" else {"held": HELDOUT, "held2": HELDOUT2, "held3": HELDOUT3, "held4": HELDOUT4}.get(a.set, SCENARIOS)
    names = [n for n in pool if not a.only or n in a.only.split(",")]
    improve = Improve.none() if a.baseline else Improve.all()
    uia = UIA()
    for name in names:
        setup, task, ver = pool[name]
        for i in range(a.n):
            cleanup_apps()
            sb = os.path.join(a.work, name); shutil.rmtree(sb, ignore_errors=True); os.makedirs(sb)
            setup(sb)
            llm = LLM(a.url, a.model, a.key, no_think=a.no_think, temperature=a.temp, seed=i + 1)
            audit = Audit(os.path.join(a.work, "audit-%s-%d.jsonl" % (name, i)))
            t0 = time.time(); steps = []
            try:
                st, sm = Agent(llm, uia, audit, sb, a.max_steps, log=lambda m: steps.append(m), approver=DenyAll(), max_seconds=300, improve=improve).run(task)
            except Exception as e:
                st, sm = "crash", "%s: %s" % (type(e).__name__, e)
            ok = bool(ver(sb))
            rec = dict(scenario=name, run=i, set=a.set, baseline=a.baseline, temp=a.temp, tag=a.tag, agent_status=st, verified=ok, steps=len(steps),
                       tokens=llm.tokens, seconds=round(time.time() - t0, 1), summary=sm[:200])
            open(a.out, "a", encoding="utf-8").write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(json.dumps(rec, ensure_ascii=True), flush=True)   # console Windows (cp1252) khong in duoc moi ky tu
            try: uia.close_window()
            except Exception: pass

if __name__ == "__main__":
    main()
