"""Draw a STEP file as SVG. Run as a separate process (`python -I stepview.py FILE`) so a broken or huge file can never take the app down."""
import os, re, sys, tempfile


def main(path):
    import cadquery as cq
    shape = cq.importers.importStep(path)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "v.svg")
        cq.exporters.export(shape, p, cq.exporters.ExportTypes.SVG,
                            opt={"width": 640, "height": 440, "marginLeft": 24, "marginTop": 24, "showAxes": False, "projectionDir": (1, -1, 0.8),
                                 "strokeWidth": 0.5, "strokeColor": (30, 30, 30), "hiddenColor": (170, 170, 170), "showHidden": True})
        svg = open(p, encoding="utf-8").read()
    if "viewBox" not in svg[:400]:
        svg = re.sub(r"<svg\b", '<svg viewBox="0 0 640 440"', svg, count=1)
    sys.stdout.write(svg)


if __name__ == "__main__":
    main(sys.argv[1])
