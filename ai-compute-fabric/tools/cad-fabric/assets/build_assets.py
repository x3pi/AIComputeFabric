"""Builds the CadFabric logo and icon files from one geometry (pure vector, no fonts needed at display time: the wordmark is converted to paths).
Run: python assets/build_assets.py   ->  assets/*.svg, assets/png/*.png, assets/cadfabric.ico
Needs: fonttools, cairosvg, Pillow, and the Inter font (SIL OFL) at FONT below."""
import math, os
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf"
ACC, ACC_DARK, ACC_MID, ACC_LIGHT = "#c96442", "#8f3b21", "#a8502f", "#d97757"
INK, INK_DARK_BG = "#1f2328", "#ece9e2"


def cube(cx, cy, r, hole=True, hole_k=0.29):
    """Isometric cube (hexagon) centred at cx,cy with circumradius r. -> (top, left, right path data, hole ellipse (cx, cy, rx, ry))."""
    p = [(cx + r * math.sin(math.radians(a)), cy - r * math.cos(math.radians(a))) for a in range(0, 360, 60)]   # top, upper-right, lower-right, bottom, lower-left, upper-left
    c = (cx, cy)
    f = lambda pts: "M" + " L".join("%.2f %.2f" % q for q in pts) + " Z"
    top, left, right = f([p[0], p[1], c, p[5]]), f([p[5], c, p[3], p[4]]), f([c, p[1], p[2], p[3]])
    ty = (p[0][1] + c[1]) / 2                                       # centre of the top face
    rx = r * hole_k * 2
    return top, left, right, (cx, ty, rx, rx * 0.5774)


def mark_group(cx, cy, r, top_fill, left_fill, right_fill, hole_fill, hole_k=0.29, stroke=None):
    top, left, right, (hx, hy, rx, ry) = cube(cx, cy, r, hole_k=hole_k)
    s = ' stroke="%s" stroke-width="%.2f" stroke-linejoin="round"' % (stroke[0], stroke[1]) if stroke else ""
    return ('<path d="%s" fill="%s"%s/><path d="%s" fill="%s"%s/><path d="%s" fill="%s"%s/>'
            '<ellipse cx="%.2f" cy="%.2f" rx="%.2f" ry="%.2f" fill="%s"/>') % (left, left_fill, s, right, right_fill, s, top, top_fill, s, hx, hy, rx, ry, hole_fill)


def svg(w, h, body, title, vb=None):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s" width="%s" height="%s" role="img" aria-label="%s"><title>%s</title>%s</svg>\n'
            % (vb or "0 0 %s %s" % (w, h), w, h, title, title, body))


def tile(size_rx, body, gid="g"):
    return ('<defs><linearGradient id="%s" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="%s"/><stop offset="1" stop-color="#b9532f"/></linearGradient></defs>'
            '<rect width="64" height="64" rx="%s" fill="url(#%s)"/>%s' % (gid, ACC_LIGHT, size_rx, gid, body))


def icon_svg(small=False):
    r, hk = (25, 0.27) if small else (23, 0.22)
    g = mark_group(32, 32, r, "#ffffff", "rgba(255,255,255,0.74)", "rgba(255,255,255,0.5)", ACC_DARK if not small else "#a24627", hole_k=hk)
    return svg(64, 64, tile(14 if not small else 12, g), "CadFabric")


def text_paths(text, size, x, y, fill, font):
    """Outline of `text` as one path (Inter SemiBold), baseline at y, left edge at x. -> (svg path element, advance width)."""
    gs, cmap, upm = font.getGlyphSet(), font.getBestCmap(), font["head"].unitsPerEm
    s = size / upm
    out, pen_x = [], 0
    for ch in text:
        name = cmap[ord(ch)]
        pen = SVGPathPen(gs)
        gs[name].draw(TransformPen(pen, (s, 0, 0, -s, x + pen_x * s, y)))
        out.append(pen.getCommands())
        pen_x += gs[name].width
    return '<path d="%s" fill="%s"/>' % (" ".join(out), fill), pen_x * s


def logo_svg(dark=False):
    font = TTFont(FONT)
    top, left, right = ("#d97757", "#b9593a", "#98452a") if dark else (ACC, ACC_MID, ACC_DARK)
    cad_fill, fab_fill = (INK_DARK_BG, "#d97757") if dark else (INK, ACC)
    mark = ""
    # the hole must be see-through: rebuild the top face with an even-odd hole
    t, l, r_, (hx, hy, rx, ry) = cube(30, 32, 26, hole_k=0.23)
    ell = "M%.2f %.2f a%.2f %.2f 0 1 0 %.2f 0 a%.2f %.2f 0 1 0 %.2f 0 Z" % (hx - rx, hy, rx, ry, 2 * rx, rx, ry, -2 * rx)
    mark = '<path d="%s" fill="%s"/><path d="%s" fill="%s"/><path d="%s %s" fill="%s" fill-rule="evenodd"/>' % (l, left, r_, right, t, ell, top)
    size, baseline, x0 = 40, 46.5, 70
    cad, w1 = text_paths("Cad", size, x0, baseline, cad_fill, font)
    fab, w2 = text_paths("Fabric", size, x0 + w1 + 1, baseline, fab_fill, font)
    width = int(x0 + w1 + w2 + 8)
    return svg(width, 64, mark + cad + fab, "CadFabric", vb="0 0 %d 64" % width)


def mark_svg():
    t, l, r_, (hx, hy, rx, ry) = cube(32, 32, 28, hole_k=0.23)
    ell = "M%.2f %.2f a%.2f %.2f 0 1 0 %.2f 0 a%.2f %.2f 0 1 0 %.2f 0 Z" % (hx - rx, hy, rx, ry, 2 * rx, rx, ry, -2 * rx)
    return svg(64, 64, '<path d="%s" fill="%s"/><path d="%s" fill="%s"/><path d="%s %s" fill="%s" fill-rule="evenodd"/>' % (l, ACC_MID, r_, ACC_DARK, t, ell, ACC), "CadFabric")


def main():
    files = {"cadfabric-icon.svg": icon_svg(), "cadfabric-icon-small.svg": icon_svg(small=True), "cadfabric-mark.svg": mark_svg(),
             "cadfabric-logo.svg": logo_svg(), "cadfabric-logo-dark.svg": logo_svg(dark=True)}
    for n, s in files.items():
        open(os.path.join(HERE, n), "w", encoding="utf-8").write(s)
    import cairosvg
    from PIL import Image
    os.makedirs(os.path.join(HERE, "png"), exist_ok=True)
    sizes = (16, 24, 32, 48, 64, 128, 256, 512)
    for z in sizes:
        src = "cadfabric-icon-small.svg" if z <= 32 else "cadfabric-icon.svg"
        cairosvg.svg2png(url=os.path.join(HERE, src), write_to=os.path.join(HERE, "png", "cadfabric-%d.png" % z), output_width=z, output_height=z)
    cairosvg.svg2png(url=os.path.join(HERE, "cadfabric-logo.svg"), write_to=os.path.join(HERE, "png", "cadfabric-logo.png"), output_height=256)
    imgs = [Image.open(os.path.join(HERE, "png", "cadfabric-%d.png" % z)).convert("RGBA") for z in (256,)]
    imgs[0].save(os.path.join(HERE, "cadfabric.ico"), format="ICO", sizes=[(z, z) for z in (16, 24, 32, 48, 64, 128, 256)],
                 append_images=[Image.open(os.path.join(HERE, "png", "cadfabric-%d.png" % z)).convert("RGBA") for z in (16, 24, 32, 48, 64, 128)])
    print("ok:", ", ".join(sorted(files)), "+ png/ + cadfabric.ico")


if __name__ == "__main__":
    main()
