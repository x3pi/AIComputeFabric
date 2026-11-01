"""Nguoi duyet. Mac dinh TU CHOI neu khong ai tra loi (an toan)."""
import json, os, time

class DenyAll:
    def __call__(self, action, reason): return False

class AllowAll:   # chi de test/kich ban da kiem soat
    def __call__(self, action, reason): return True

class FileApprover:
    """Ghi <dir>/pending-N.json; nguoi duyet tao <dir>/pending-N.allow hoac .deny. Het gio => deny."""
    def __init__(self, d, timeout=120):
        self.d, self.timeout, self.n = d, timeout, 0
        os.makedirs(d, exist_ok=True)

    def __call__(self, action, reason):
        self.n += 1
        base = os.path.join(self.d, "pending-%d-%d" % (os.getpid(), self.n))
        json.dump({"action": action, "reason": reason}, open(base + ".json", "w", encoding="utf-8"), ensure_ascii=False)
        end = time.time() + self.timeout
        while time.time() < end:
            if os.path.exists(base + ".allow"): return True
            if os.path.exists(base + ".deny"): return False
            time.sleep(0.5)
        return False
