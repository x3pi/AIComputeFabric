"""Ve icon wagent (bo goc xanh + con tro chuot trang + cham tron) thanh .ico nhieu kich thuoc. Chi dung stdlib."""
import struct, sys, zlib

def draw(size):
    ss = 3  # supersample
    n = size * ss
    def inside_round(x, y, r):
        cx = min(max(x, r), n - 1 - r); cy = min(max(y, r), n - 1 - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    # con tro: da giac theo toa do 0..1
    poly = [(0.30, 0.18), (0.30, 0.78), (0.44, 0.65), (0.54, 0.86), (0.63, 0.82), (0.53, 0.61), (0.72, 0.61)]
    poly = [(x * n, y * n) for x, y in poly]
    def in_poly(px, py):
        c = False; j = len(poly) - 1
        for i in range(len(poly)):
            xi, yi = poly[i]; xj, yj = poly[j]
            if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi: c = not c
            j = i
        return c
    r = int(n * 0.22)
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    X, Y = x * ss + sx, y * ss + sy
                    if not inside_round(X, Y, r): continue
                    t = Y / n
                    col = (int(37 + (30 - 37) * t), int(99 + (64 - 99) * t), int(235 + (175 - 235) * t), 255)  # xanh -> xanh dam
                    if in_poly(X, Y): col = (255, 255, 255, 255)
                    elif (X - 0.74 * n) ** 2 + (Y - 0.30 * n) ** 2 <= (0.075 * n) ** 2: col = (125, 211, 252, 255)
                    for k in range(4): acc[k] += col[k] if k == 3 else col[k]
                    # alpha cong don
            cnt = ss * ss
            a = acc[3] // cnt
            if a == 0: row += b"\x00\x00\x00\x00"
            else:
                covered = acc[3] // 255
                row += bytes((acc[0] // covered, acc[1] // covered, acc[2] // covered, a))
        rows.append(bytes(row))
    return rows

def png(size):
    raw = b"".join(b"\x00" + r for r in draw(size))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")

def make_ico(path, sizes=(16, 32, 48, 64, 128, 256)):
    imgs = [png(s) for s in sizes]
    hdr = struct.pack("<HHH", 0, 1, len(imgs)); off = 6 + 16 * len(imgs); ents = b""
    for s, d in zip(sizes, imgs):
        ents += struct.pack("<BBBBHHII", 0 if s == 256 else s, 0 if s == 256 else s, 0, 0, 1, 32, len(d), off); off += len(d)
    open(path, "wb").write(hdr + ents + b"".join(imgs))

if __name__ == "__main__":
    make_ico(sys.argv[1] if len(sys.argv) > 1 else "wagent.ico")
