"""Same-shape check for the benchmark. A matching volume is not enough (a hole in the wrong place keeps the volume), so the final part is compared with the
reference by quantities that do not depend on where or how the model placed it: volume, surface area, the three principal moments of inertia, and the sorted
bounding-box sizes. Both shapes are measured by the CAD kernel; the model's own claims are never used."""
import cadquery as cq
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps


def fingerprint(shape):
    """-> dict(volume, area, moments=[3 sorted], dims=[3 sorted]) of a cq.Shape or a Workplane."""
    sh = shape.val() if hasattr(shape, "val") else shape
    v, a = GProp_GProps(), GProp_GProps()
    BRepGProp.VolumeProperties_s(sh.wrapped, v)
    BRepGProp.SurfaceProperties_s(sh.wrapped, a)
    p = v.PrincipalProperties()
    mom = sorted([p.Moments()[0], p.Moments()[1], p.Moments()[2]])
    bb = sh.BoundingBox()
    return {"volume": v.Mass(), "area": a.Mass(), "moments": mom, "dims": sorted([bb.xlen, bb.ylen, bb.zlen])}


def from_step(path):
    return fingerprint(cq.importers.importStep(path))


def _close(x, y, tol):
    return abs(x - y) <= tol * max(abs(y), 1e-9)


def same(got, ref, tol=0.002):
    """-> (ok, [what differs]). `tol` is relative; moments are the strictest test of placement of features."""
    bad = []
    for k in ("volume", "area"):
        if not _close(got[k], ref[k], tol):
            bad.append("%s %.4g vs %.4g" % (k, got[k], ref[k]))
    for k in ("moments", "dims"):
        for i, (x, y) in enumerate(zip(got[k], ref[k])):
            if not _close(x, y, tol):
                bad.append("%s[%d] %.4g vs %.4g" % (k, i, x, y))
    return (not bad), bad


def reference_print(backend, ref_code):
    """Fingerprint of a task's reference solution, built in the (sandboxed) worker and read back from a STEP file."""
    import os, tempfile
    backend.reset()
    r = backend.execute(ref_code)
    if not r.ok:
        raise RuntimeError("reference failed: %s" % r.error)
    return _via_step(backend)


def _via_step(backend):
    import os, tempfile
    d = tempfile.mkdtemp(prefix="cfshape-")
    p = os.path.join(d, "p.step")
    try:
        backend.export_step(p)
        return from_step(p)
    finally:
        try:
            os.unlink(p); os.rmdir(d)
        except OSError:
            pass


def current_print(backend):
    return _via_step(backend)
