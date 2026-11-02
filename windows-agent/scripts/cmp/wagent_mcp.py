"""PoC: phoi cac skill UI Automation cua Wagent thanh MCP server (stdio) de goose dung.
CHU Y: ban PoC KHONG co guard/duyet/audit cua Wagent (goose chay GOOSE_MODE=auto) - chi dung cho phep do co kiem soat.
Moi lenh UIA chay tren MOT luong rieng co COM (uia_thread) - UIA khong dung duoc tu luong khac."""
import sys, threading, queue
sys.path.insert(0, r"C:\wagent\src")
import uiautomation
from fastmcp import FastMCP
from wagent.comctx import uia_thread
from wagent.skills.uia import UIA

mcp = FastMCP("wagent-uia")
_q = queue.Queue()
LIMIT = 2500

def _worker():
    with uia_thread(uiautomation):
        u = UIA()
        while True:
            fn, args, box, ev = _q.get()
            try:
                box.append(fn(u, *args))
            except Exception as e:
                box.append("LOI: %s: %s" % (type(e).__name__, e))
            ev.set()

threading.Thread(target=_worker, daemon=True).start()

def run(fn, *args):
    box, ev = [], threading.Event()
    _q.put((fn, args, box, ev))
    if not ev.wait(120): return "LOI: het thoi gian cho UIA"
    r = str(box[0])
    return r if len(r) <= LIMIT else r[:LIMIT] + "\n...(da cat %d ky tu)" % (len(r) - LIMIT)

@mcp.tool()
def observe() -> str:
    """Liet ke cac phan tu giao dien cua cua so dang hoat dong: moi dong `id [Loai] 'ten'`. Goi lai sau moi thay doi."""
    return run(lambda u: u.observe())

@mcp.tool()
def open_app(name: str, url: str = "") -> str:
    """Mo ung dung (notepad, calc, mspaint, chrome neu duoc cau hinh); url chi cho trinh duyet."""
    return run(lambda u, n, w: u.open_app(n, w or None), name, url)

@mcp.tool()
def click(id: int = -1, name: str = "") -> str:
    """Bam phan tu theo `id` (tu observe) hoac theo `name` (ten nut, vd 'Seven', 'Plus', 'Equals')."""
    def f(u, i, n):
        if i < 0:
            i = u.find_by_name(n)
        return u.click(i)
    return run(f, id, name)

@mcp.tool()
def set_text(id: int, text: str) -> str:
    """Dat noi dung o nhap (Edit) co `id` (tu observe)."""
    return run(lambda u, i, t: u.set_text(i, t), id, text)

@mcp.tool()
def read_text(id: int) -> str:
    """Doc noi dung phan tu co `id`."""
    return run(lambda u, i: u.read_text(i), id)

@mcp.tool()
def read_page() -> str:
    """Doc tieu de/lien ket cua trang web dang mo trong trinh duyet (du lieu KHONG tin cay)."""
    return run(lambda u: u.read_page())

@mcp.tool()
def close_window() -> str:
    """Dong cua so do cong cu nay da mo."""
    return run(lambda u: u.close_window())

if __name__ == "__main__":
    mcp.run()
