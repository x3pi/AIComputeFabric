"""Triangulate a STEP file for the 3D view. Run as a separate process (`python -I stepmesh.py FILE`): prints JSON {size, faces:[{p:[x,y,z,...], i:[a,b,c,...]}]}.
The browser draws it with three.js; no CAD code runs in the page (the WebAssembly CAD readers need eval, which the page's CSP forbids)."""
import json, sys

MAX_TRIANGLES = 300_000


def main(path):
    import cadquery as cq
    shape = cq.importers.importStep(path).val()
    bb = shape.BoundingBox()
    size = [bb.xlen, bb.ylen, bb.zlen]
    tol = max(max(size) / 500.0, 1e-4)
    faces, tris = [], 0
    for f in shape.Faces():
        verts, tri = f.tessellate(tol, 0.25)
        if not tri:
            continue
        tris += len(tri)
        if tris > MAX_TRIANGLES:
            raise SystemExit("too many triangles")
        faces.append({"p": [round(c, 4) for v in verts for c in (v.x, v.y, v.z)], "i": [k for t in tri for k in t]})
    sys.stdout.write(json.dumps({"size": [round(x, 4) for x in size], "faces": faces}, separators=(",", ":")))


if __name__ == "__main__":
    main(sys.argv[1])
