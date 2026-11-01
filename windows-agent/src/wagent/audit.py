"""Audit JSONL hash-chain: moi dong chua prev_hash va hash cua chinh no."""
import hashlib, json, time

class Audit:
    def __init__(self, path):
        self.path, self.prev = path, "0" * 64

    def write(self, **ev):
        ev["ts"], ev["prev"] = time.time(), self.prev
        body = json.dumps(ev, sort_keys=True, ensure_ascii=False)
        self.prev = hashlib.sha256(body.encode()).hexdigest()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**ev, "hash": self.prev}, sort_keys=True, ensure_ascii=False) + "\n")

def verify(path):
    prev = "0" * 64
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        ev = json.loads(line)
        h = ev.pop("hash")
        if ev["prev"] != prev or hashlib.sha256(json.dumps(ev, sort_keys=True, ensure_ascii=False).encode()).hexdigest() != h:
            return False, n
        prev = h
    return True, 0
