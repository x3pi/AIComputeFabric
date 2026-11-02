"""Skill file: chi doc/ghi ben trong thu muc sandbox."""
import os

class PathDenied(Exception):
    pass

def resolve(sandbox, rel):
    root = os.path.realpath(sandbox)
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        raise PathDenied("ngoai sandbox: %s" % rel)
    return p

def read_file(sandbox, path, **_):
    with open(resolve(sandbox, path), encoding="utf-8") as f:
        return f.read(4000)

def write_file(sandbox, path, content, **_):
    p = resolve(sandbox, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return "da ghi %d ky tu vao %s" % (len(content), path)

def list_dir(sandbox, path=".", **_):
    p = resolve(sandbox, path)
    if not os.path.exists(p):
        return "(thu muc '%s' chua ton tai - khong sao: no se duoc tu tao khi ban move_file/copy_file/write_file vao do)" % path
    if not os.path.isdir(p):
        raise NotADirectoryError("%s la file, khong phai thu muc" % path)
    return "\n".join(sorted(os.listdir(p))) or "(thu muc rong)"

def move_file(sandbox, src, dst, **_):
    import shutil
    a, b = resolve(sandbox, src), resolve(sandbox, dst)
    os.makedirs(os.path.dirname(b), exist_ok=True)
    shutil.move(a, b)
    return "da chuyen %s -> %s" % (src, dst)

def copy_file(sandbox, src, dst, **_):
    import shutil
    a, b = resolve(sandbox, src), resolve(sandbox, dst)
    os.makedirs(os.path.dirname(b), exist_ok=True)
    shutil.copy2(a, b)
    return "da sao chep %s -> %s" % (src, dst)

def tree(sandbox, limit=200):
    """Danh sach (duong_dan_tuong_doi, kich_thuoc) cua moi file trong sandbox - dung de doi chieu trang thai that."""
    root = os.path.realpath(sandbox); out = []
    for d, _, fs in os.walk(root):
        for f in fs:
            full = os.path.join(d, f)
            out.append((os.path.relpath(full, root).replace(os.sep, "/"), os.path.getsize(full)))
    return sorted(out)[:limit]
