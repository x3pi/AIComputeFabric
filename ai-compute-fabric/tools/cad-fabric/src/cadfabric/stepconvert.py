"""Convert a STEP file to a binary STL (for programs that cannot read STEP, e.g. Blender). Run as a separate process:
`python -I stepconvert.py IN.step OUT.stl`."""
import sys


def main(src, dst):
    import cadquery as cq
    shape = cq.importers.importStep(src)
    bb = shape.val().BoundingBox()
    tol = max(max(bb.xlen, bb.ylen, bb.zlen) / 800.0, 1e-4)
    cq.exporters.export(shape, dst, cq.exporters.ExportTypes.STL, tolerance=tol, angularTolerance=0.1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
