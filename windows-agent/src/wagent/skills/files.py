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
    return "\n".join(sorted(os.listdir(resolve(sandbox, path))))

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
