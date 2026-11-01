"""Guard GD1 (toi thieu): tool phai nam trong whitelist; GD2 se them phan loai rui ro + duyet."""
SAFE = {"observe", "open_app", "set_text", "read_text", "click", "read_file", "write_file", "list_dir", "close_window", "done"}

def check(action):
    t = action["tool"]
    if t not in SAFE:
        return False, "tool khong co trong whitelist: " + t
    return True, ""
