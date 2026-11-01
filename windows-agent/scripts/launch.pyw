"""Launcher cho shortcut desktop: bat server web (an) neu chua chay, roi mo trinh duyet.
Chay bang pythonw (khong hien cua so console). Tat ca duong dan lay theo vi tri file nay."""
import os, socket, subprocess, sys, time, webbrowser

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PORT = 8765
TOKEN_FILE = os.path.join(ROOT, "web.token")

def port_open(port, host="127.0.0.1", timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False

def read_token(path, wait=10.0):
    end = time.time() + wait
    while time.time() < end:
        try:
            t = open(path, encoding="utf-8").read().strip()
            if len(t) >= 16:
                return t
        except OSError:
            pass
        time.sleep(0.2)
    return ""

def main():
    if not port_open(PORT):
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
        log = open(os.path.join(ROOT, "web.log"), "ab")
        subprocess.Popen(
            [sys.executable, "-u", "-m", "wagent", "--web", str(PORT), "--token-file", TOKEN_FILE,
             "--ui-dir", os.path.join(ROOT, "ui_dist")],
            cwd=ROOT, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00000008,  # CREATE_NO_WINDOW | DETACHED_PROCESS
        )
        for _ in range(75):
            if port_open(PORT):
                break
            time.sleep(0.2)
    tok = read_token(TOKEN_FILE)
    webbrowser.open("http://127.0.0.1:%d/?t=%s" % (PORT, tok))

if __name__ == "__main__":
    main()
