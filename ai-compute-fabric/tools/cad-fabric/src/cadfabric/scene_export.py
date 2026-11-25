"""Universal scene exporter for CadFabric: converts GLB/bxlib 3D scenes to binary STL (full or single part),
auto-oriented multi-color 3MF build plates (Bambu Studio / OrcaSlicer ready), and colored multi-part XCAF STEP assemblies.
Run as subprocess for STEP/3MF export: `python -I scene_export.py step|3mf <scene_dir> <glb_path> <out_path>`."""
import json
import math
import os
import struct
import sys
import zipfile
from xml.sax.saxutils import escape as _xml_esc

import numpy as np

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942

_COMP_DTYPE = {
    5120: ("i1", 1, 127.0),
    5121: ("u1", 1, 255.0),
    5122: ("<i2", 2, 32767.0),
    5123: ("<u2", 2, 65535.0),
    5125: ("<u4", 4, 4294967295.0),
    5126: ("<f4", 4, 1.0),
}
_TYPE_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _read_accessor(gltf, raw, bin_off, bin_len, idx):
    acc = (gltf.get("accessors") or [])[idx]
    dtype_str, bpe, max_val = _COMP_DTYPE[acc["componentType"]]
    n_comp = _TYPE_COUNT[acc["type"]]
    bv = (gltf.get("bufferViews") or [])[acc.get("bufferView", 0)]
    rel_off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    abs_off = bin_off + rel_off
    stride = bv.get("byteStride") or (n_comp * bpe)
    count = int(acc.get("count") or 0)
    if rel_off < 0 or (count > 0 and abs_off + (count - 1) * stride + n_comp * bpe > bin_off + bin_len):
        raise ValueError("GLB accessor out of bounds")
    if count == 0:
        return np.zeros((0, n_comp) if n_comp > 1 else (0,), dtype=np.float64)
    if stride == n_comp * bpe:
        arr = np.frombuffer(raw, dtype=dtype_str, count=count * n_comp, offset=abs_off)
    else:
        dt = np.dtype({"names": ["v"], "formats": ["(%d,)%s" % (n_comp, dtype_str)], "offsets": [0], "itemsize": stride})
        arr = np.frombuffer(raw, dtype=dt, count=count, offset=abs_off)["v"].reshape(-1)
    if acc.get("normalized") and acc["componentType"] != 5126:
        arr = np.maximum(-1.0, arr.astype(np.float64) / max_val)
    if n_comp > 1:
        arr = arr.reshape(count, n_comp)
    return arr


def _node_matrix(nd):
    if "matrix" in nd and len(nd["matrix"]) == 16:
        return np.array(nd["matrix"], dtype=np.float64).reshape(4, 4).T
    M = np.eye(4, dtype=np.float64)
    if "scale" in nd:
        sx, sy, sz = nd["scale"]
        M = M @ np.diag([sx, sy, sz, 1.0])
    if "rotation" in nd:
        x, y, z, w = nd["rotation"]
        R = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0.0],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0.0],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ], dtype=np.float64)
        M = R @ M
    if "translation" in nd:
        T = np.eye(4, dtype=np.float64)
        T[:3, 3] = nd["translation"]
        M = T @ M
    return M


def glb_extract_parts(glb_source):
    """Parse GLB (path or bytes) into CAD Z-up mm meshes: {part_id: (verts_mm Nx3, tris Mx3)}."""
    if isinstance(glb_source, (bytes, bytearray, memoryview)):
        raw = bytes(glb_source)
    else:
        with open(glb_source, "rb") as f:
            raw = f.read()
    if len(raw) < 12:
        raise ValueError("Invalid GLB (too short)")
    magic, ver, total_len = struct.unpack_from("<III", raw, 0)
    if magic != GLB_MAGIC or ver != 2:
        raise ValueError("Invalid GLB header")
    total_len = min(total_len, len(raw))
    gltf, bin_off, bin_len, off = None, 0, 0, 12
    while off + 8 <= total_len:
        c_len, c_type = struct.unpack_from("<II", raw, off)
        data_off = off + 8
        if data_off + c_len > total_len:
            break
        if c_type == CHUNK_JSON:
            gltf = json.loads(raw[data_off:data_off + c_len].decode("utf-8"))
        elif c_type == CHUNK_BIN:
            bin_off, bin_len = data_off, c_len
        off = data_off + c_len
    if not gltf:
        raise ValueError("GLB missing JSON chunk")

    nodes = gltf.get("nodes") or []
    meshes = gltf.get("meshes") or []
    parents = {}
    for i, nd in enumerate(nodes):
        for c in nd.get("children") or ():
            parents[c] = i
    world_mats = {}

    def world_mat(i):
        if i in world_mats:
            return world_mats[i]
        M = _node_matrix(nodes[i])
        if i in parents:
            M = world_mat(parents[i]) @ M
        world_mats[i] = M
        return M

    parts = {}
    for i, nd in enumerate(nodes):
        if "mesh" not in nd:
            continue
        pid = str(nd.get("name") or ("node_%d" % i))
        wm = world_mat(i)
        mdef = meshes[nd["mesh"]]
        v_list, t_list, v_off = [], [], 0
        for pr in mdef.get("primitives") or ():
            if (pr.get("mode", 4)) != 4:
                continue
            attrs = pr.get("attributes") or {}
            if "POSITION" not in attrs:
                continue
            pos = np.asarray(_read_accessor(gltf, raw, bin_off, bin_len, attrs["POSITION"]), dtype=np.float64)
            if not len(pos):
                continue
            vh = np.c_[pos, np.ones(len(pos), dtype=np.float64)] @ wm.T
            # glTF Y-up metres -> CAD Z-up millimetres: (x, -z, y) preserves right-handed winding
            vcad = np.c_[vh[:, 0] * 1000.0, -vh[:, 2] * 1000.0, vh[:, 1] * 1000.0]
            if "indices" in pr:
                idx = np.asarray(_read_accessor(gltf, raw, bin_off, bin_len, pr["indices"]), dtype=np.int32).reshape(-1)
            else:
                idx = np.arange(len(pos), dtype=np.int32)
            tris = idx[: (len(idx) // 3) * 3].reshape(-1, 3) + v_off
            v_list.append(vcad)
            t_list.append(tris)
            v_off += len(vcad)
        if v_list:
            va = np.vstack(v_list)
            ta = np.vstack(t_list)
            if pid in parts:
                pv, pt = parts[pid]
                parts[pid] = (np.vstack([pv, va]), np.vstack([pt, ta + len(pv)]))
            else:
                parts[pid] = (va, ta)
    return parts


def export_stl(glb_path, out_path, part_id=None):
    """Write IEEE-754 binary STL (mm, Z-up) for all parts or one `part_id`. -> (tri_count, error)."""
    try:
        parts = glb_extract_parts(glb_path)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:160]
    if part_id is not None:
        if part_id not in parts:
            return 0, "unknown part %r" % part_id
        keys = [part_id]
    else:
        keys = sorted(parts)
    if not keys:
        return 0, "no mesh parts to export"

    tri_chunks = []
    for k in keys:
        v, t = parts[k]
        if len(t):
            tri_chunks.append(v[t])  # shape (M, 3, 3)
    if not tri_chunks:
        return 0, "no triangles to export"
    all_tris = np.concatenate(tri_chunks, axis=0)
    e1 = all_tris[:, 1, :] - all_tris[:, 0, :]
    e2 = all_tris[:, 2, :] - all_tris[:, 0, :]
    nrm = np.cross(e1, e2)
    lens = np.linalg.norm(nrm, axis=1, keepdims=True)
    nrm = np.divide(nrm, np.where(lens > 1e-12, lens, 1.0))

    n_tri = len(all_tris)
    stl_dtype = np.dtype([
        ("n", "<f4", 3),
        ("v0", "<f4", 3),
        ("v1", "<f4", 3),
        ("v2", "<f4", 3),
        ("attr", "<u2"),
    ])
    rec = np.empty(n_tri, dtype=stl_dtype)
    rec["n"] = nrm.astype(np.float32)
    rec["v0"] = all_tris[:, 0, :].astype(np.float32)
    rec["v1"] = all_tris[:, 1, :].astype(np.float32)
    rec["v2"] = all_tris[:, 2, :].astype(np.float32)
    rec["attr"] = 0

    hdr = ("CadFabric binary STL (mm, Z-up) " + (part_id or "scene")).encode("ascii", "replace")[:80].ljust(80, b" ")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp.stl"
    with open(tmp, "wb") as f:
        f.write(hdr)
        f.write(struct.pack("<I", n_tri))
        f.write(rec.tobytes())
    os.replace(tmp, out_path)
    return n_tri, ""


def _op_to_cq(o):
    """Convert one validated bxlib recipe op (mm, world Z-up) into a CadQuery Solid/Shape."""
    import cadquery as cq
    from cadfabric.blender import bxlib

    t = o["op"]
    bev = float(o.get("bevel") or 0.0)
    if t == "box":
        dx, dy, dz = [float(x) for x in o["size"]]
        s = cq.Solid.makeBox(dx, dy, dz, cq.Vector(-dx / 2.0, -dy / 2.0, -dz / 2.0))
        if 0.0 < bev < min(dx, dy, dz) * 0.45:
            try:
                s = cq.Shape.cast(s).fillet(bev, s.Edges()) if hasattr(cq.Shape.cast(s), "fillet") else cq.Workplane(obj=s).edges().fillet(bev).val()
            except Exception:  # noqa: BLE001
                pass
        if "rot" in o:
            ax = bxlib._axis(o["rot"]["axis"])
            s = s.rotate(cq.Vector(0, 0, 0), cq.Vector(*ax), float(o["rot"]["deg"]))
        cx, cy, cz = [float(x) for x in o["center"]]
        return s.translate(cq.Vector(cx, cy, cz))

    if t == "shell":
        dx, dy, dz = [float(x) for x in o["size"]]
        wall = float(o["wall"])
        opn = o.get("open", "+z")
        outer = cq.Solid.makeBox(dx, dy, dz, cq.Vector(-dx / 2.0, -dy / 2.0, -dz / 2.0))
        if 0.0 < bev < min(dx, dy, dz) * 0.45:
            try:
                outer = cq.Shape.cast(outer).fillet(bev, outer.Edges()) if hasattr(cq.Shape.cast(outer), "fillet") else cq.Workplane(obj=outer).edges().fillet(bev).val()
            except Exception:  # noqa: BLE001
                pass
        in_sz = [dx - 2.0 * wall, dy - 2.0 * wall, dz - 2.0 * wall]
        in_loc = [0.0, 0.0, 0.0]
        if opn != "none":
            sgn = 1.0 if opn[0] == "+" else -1.0
            ax = "xyz".index(opn[1])
            over = max(2.0, bev + 2.0)
            in_sz[ax] = [dx, dy, dz][ax] - wall + over
            in_loc[ax] = sgn * (wall + over) / 2.0
        inner = cq.Solid.makeBox(in_sz[0], in_sz[1], in_sz[2], cq.Vector(-in_sz[0] / 2.0 + in_loc[0], -in_sz[1] / 2.0 + in_loc[1], -in_sz[2] / 2.0 + in_loc[2]))
        s = outer.cut(inner)
        if "rot" in o:
            ax_r = bxlib._axis(o["rot"]["axis"])
            s = s.rotate(cq.Vector(0, 0, 0), cq.Vector(*ax_r), float(o["rot"]["deg"]))
        cx, cy, cz = [float(x) for x in o["center"]]
        return s.translate(cq.Vector(cx, cy, cz))

    if t == "cylinder":
        a, b = [float(x) for x in o["a"]], [float(x) for x in o["b"]]
        d = [b[i] - a[i] for i in range(3)]
        length = math.sqrt(sum(x * x for x in d))
        r1 = float(o["r"])
        r2 = float(o.get("r2", r1))
        if abs(r1 - r2) < 1e-6:
            return cq.Solid.makeCylinder(r1, length, cq.Vector(*a), cq.Vector(*d))
        return cq.Solid.makeCone(r1, r2, length, cq.Vector(*a), cq.Vector(*d))

    if t == "disc":
        ax = bxlib._norm3(bxlib._axis(o["axis"]))
        c = [float(x) for x in o["center"]]
        th = float(o["thickness"])
        a = [c[i] - ax[i] * th / 2.0 for i in range(3)]
        return cq.Solid.makeCylinder(float(o["r"]), th, cq.Vector(*a), cq.Vector(*ax))

    if t == "ring":
        ax = bxlib._norm3(bxlib._axis(o["axis"]))
        c = [float(x) for x in o["center"]]
        w = float(o["width"])
        r_out, r_in = float(o["r_out"]), float(o["r_in"])
        a_out = [c[i] - ax[i] * w / 2.0 for i in range(3)]
        outer = cq.Solid.makeCylinder(r_out, w, cq.Vector(*a_out), cq.Vector(*ax))
        if r_in <= 1e-6:
            return outer
        a_in = [c[i] - ax[i] * (w / 2.0 + 0.1) for i in range(3)]
        inner = cq.Solid.makeCylinder(r_in, w + 0.2, cq.Vector(*a_in), cq.Vector(*ax))
        return outer.cut(inner)

    if t == "sphere":
        c = [float(x) for x in o["center"]]
        rr = o.get("radii")
        if rr:
            rx, ry, rz = [float(x) for x in rr]
            if abs(rx - ry) < 1e-6 and abs(rx - rz) < 1e-6:
                return cq.Solid.makeSphere(rx).translate(cq.Vector(*c))
            from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
            from OCP.gp import gp_GTrsf
            unit = cq.Solid.makeSphere(1.0)
            gt = gp_GTrsf()
            gt.SetValue(1, 1, rx)
            gt.SetValue(2, 2, ry)
            gt.SetValue(3, 3, rz)
            scaled = cq.Shape.cast(BRepBuilderAPI_GTransform(unit.wrapped, gt, True).Shape())
            return scaled.translate(cq.Vector(*c))
        return cq.Solid.makeSphere(float(o["r"])).translate(cq.Vector(*c))

    if t == "tube":
        pts, r = o["points"], float(o["r"])
        sols = []
        for a, b in zip(pts, pts[1:]):
            d = [b[i] - a[i] for i in range(3)]
            length = math.sqrt(sum(x * x for x in d))
            if length > 1e-6:
                sols.append(cq.Solid.makeCylinder(r, length, cq.Vector(*a), cq.Vector(*d)))
        for p in pts[1:-1]:
            sols.append(cq.Solid.makeSphere(r).translate(cq.Vector(*p)))
        return sols[0] if len(sols) == 1 else cq.Compound.makeCompound(sols)

    if t == "beam":
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Trsf
        pts, up, w, h = o["points"], o.get("up", (0, 0, 1)), float(o["w"]), float(o["h"])
        sols = []
        for k in range(len(pts) - 1):
            p0, p1 = pts[k], pts[k + 1]
            d = [p1[i] - p0[i] for i in range(3)]
            L = math.sqrt(sum(x * x for x in d))
            if L <= 1e-6:
                continue
            bx, by, bz = bxlib._basis_py(d, up)
            extra = (w * 0.5) if k < len(pts) - 2 else 0.0
            mid = [(p0[i] + p1[i]) / 2.0 + bx[i] * extra / 2.0 for i in range(3)]
            seg = cq.Solid.makeBox(L + extra, w, h, cq.Vector(-(L + extra) / 2.0, -w / 2.0, -h / 2.0))
            tr = gp_Trsf()
            tr.SetValues(
                bx[0], by[0], bz[0], mid[0],
                bx[1], by[1], bz[1], mid[1],
                bx[2], by[2], bz[2], mid[2],
            )
            sols.append(cq.Shape.cast(BRepBuilderAPI_Transform(seg.wrapped, tr, True).Shape()))
        return sols[0] if len(sols) == 1 else cq.Compound.makeCompound(sols)

    if t == "spokes":
        ax = bxlib._norm3(bxlib._axis(o["axis"]))
        c = [float(x) for x in o["center"]]
        n_sp = int(o["n"])
        r_in, r_out, w_sp, th = float(o["r_in"]), float(o["r_out"]), float(o["w"]), float(o["t"])
        length = r_out - r_in
        q_rot = cq.Vector(0, 0, 1).getAngle(cq.Vector(*ax))
        axis_rot = cq.Vector(0, 0, 1).cross(cq.Vector(*ax))
        sols = []
        for k in range(n_sp):
            ang = 360.0 * k / n_sp
            sp = cq.Solid.makeBox(length, w_sp, th, cq.Vector(r_in, -w_sp / 2.0, -th / 2.0))
            sp = sp.rotate(cq.Vector(0, 0, 0), cq.Vector(0, 0, 1), ang)
            if axis_rot.Length > 1e-9:
                sp = sp.rotate(cq.Vector(0, 0, 0), axis_rot, math.degrees(q_rot))
            elif ax[2] < 0:
                sp = sp.rotate(cq.Vector(0, 0, 0), cq.Vector(1, 0, 0), 180.0)
            sols.append(sp.translate(cq.Vector(*c)))
        return sols[0] if len(sols) == 1 else cq.Compound.makeCompound(sols)

    if t == "loft":
        n = max(16, int(o.get("n", 20)))
        p_exp = float(o.get("round", 2.5))
        secs = bxlib._smooth_sections(bxlib._ordered_sections(o["sections"]), o.get("smooth", 0))
        wires = []
        for s in secs:
            pts = []
            for k in range(n):
                y, z = bxlib._ring_pt(s, 2.0 * math.pi * k / n, p_exp)
                pt = cq.Vector(float(s["x"]), float(s["y"]) + y, float(s["z"]) + z)
                if not pts or (pt - pts[-1]).Length > 1e-4:
                    pts.append(pt)
            if len(pts) >= 3:
                wires.append(cq.Wire.makePolygon(pts, close=True))
        return cq.Solid.makeLoft(wires, ruled=True)

    if t == "panel":
        a0, a1 = [math.radians(x) for x in o.get("arc", [0, 180])]
        full = abs((a1 - a0) - 2.0 * math.pi) < 1e-6
        p_exp = float(o.get("round", 2.5))
        n = max(6, int(o.get("n", 12)))
        thick = float(o.get("thick", 3.0))
        secs = bxlib._smooth_sections(bxlib._ordered_sections(o["sections"]), o.get("smooth", 0))
        if full:
            wires_out, wires_in = [], []
            for s in secs:
                si = dict(s, w=max(0.4, float(s["w"]) - 2.0 * thick), h=max(0.4, float(s["h"]) - 2.0 * thick))
                po, pi = [], []
                for k in range(n):
                    ang = a0 + (a1 - a0) * k / n
                    yo, zo = bxlib._ring_pt(s, ang, p_exp)
                    yi, zi = bxlib._ring_pt(si, ang, p_exp)
                    po.append(cq.Vector(float(s["x"]), float(s["y"]) + yo, float(s["z"]) + zo))
                    pi.append(cq.Vector(float(s["x"]), float(s["y"]) + yi, float(s["z"]) + zi))
                wires_out.append(cq.Wire.makePolygon(po, close=True))
                wires_in.append(cq.Wire.makePolygon(pi, close=True))
            return cq.Solid.makeLoft(wires_out, ruled=True).cut(cq.Solid.makeLoft(wires_in, ruled=True))
        wires = []
        for s in secs:
            si = dict(s, w=max(0.4, float(s["w"]) - 2.0 * thick), h=max(0.4, float(s["h"]) - 2.0 * thick))
            outer_pts, inner_pts = [], []
            for k in range(n + 1):
                ang = a0 + (a1 - a0) * k / n
                yo, zo = bxlib._ring_pt(s, ang, p_exp)
                yi, zi = bxlib._ring_pt(si, ang, p_exp)
                outer_pts.append(cq.Vector(float(s["x"]), float(s["y"]) + yo, float(s["z"]) + zo))
                inner_pts.append(cq.Vector(float(s["x"]), float(s["y"]) + yi, float(s["z"]) + zi))
            ring_pts = outer_pts + list(reversed(inner_pts))
            dedup = []
            for pt in ring_pts:
                if not dedup or (pt - dedup[-1]).Length > 1e-4:
                    dedup.append(pt)
            if len(dedup) >= 3:
                wires.append(cq.Wire.makePolygon(dedup, close=True))
        return cq.Solid.makeLoft(wires, ruled=True)

    return None


def _recipe_to_cq(rec):
    import cadquery as cq
    if not isinstance(rec, dict) or not isinstance(rec.get("ops"), list) or not rec["ops"]:
        return None
    acc = None
    extras = []
    for o in rec["ops"]:
        s = _op_to_cq(o)
        if s is None:
            continue
        mode = o.get("mode", "add")
        if acc is None:
            if mode == "cut":
                continue
            acc = s
        elif mode == "cut":
            try:
                acc = acc.cut(s)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                acc = acc.fuse(s)
            except Exception:  # noqa: BLE001
                extras.append(s)
    if acc is None:
        return None
    return acc if not extras else cq.Compound.makeCompound([acc] + extras)


def _mesh_to_cq(verts_mm, tris):
    """Fallback B-Rep builder via OCP Sewing (+ MakeSolid when closed) from triangulated GLB mesh."""
    import cadquery as cq
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.TopoDS import TopoDS, TopoDS_Shell
    from OCP.gp import gp_Pnt

    sew = BRepBuilderAPI_Sewing(1e-2)
    added = 0
    for i, j, k in tris:
        p0, p1, p2 = verts_mm[int(i)], verts_mm[int(j)], verts_mm[int(k)]
        if np.linalg.norm(np.cross(p1 - p0, p2 - p0)) < 1e-6:
            continue
        try:
            poly = BRepBuilderAPI_MakePolygon(gp_Pnt(*map(float, p0)), gp_Pnt(*map(float, p1)), gp_Pnt(*map(float, p2)), True)
            face = BRepBuilderAPI_MakeFace(poly.Wire(), True)
            if face.IsDone():
                sew.Add(face.Face())
                added += 1
        except Exception:  # noqa: BLE001
            continue
    if not added:
        return None
    sew.Perform()
    sh = sew.SewedShape()
    if isinstance(sh, TopoDS_Shell):
        try:
            ms = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(sh))
            if ms.IsDone():
                return cq.Shape.cast(ms.Solid())
        except Exception:  # noqa: BLE001
            pass
    return cq.Shape.cast(sh)


def _hex_color(c):
    import cadquery as cq
    if isinstance(c, str) and len(c) == 7 and c[0] == "#":
        try:
            return cq.Color(int(c[1:3], 16) / 255.0, int(c[3:5], 16) / 255.0, int(c[5:7], 16) / 255.0, 1.0)
        except ValueError:
            pass
    return None


def export_step(scene_dir, glb_path, out_path):
    """Build colored multi-part XCAF STEP assembly from scene_dir (plan.json + parts/*.py + GLB fallback)."""
    import cadquery as cq
    from cadfabric import scene3d

    with open(os.path.join(scene_dir, "plan.json"), encoding="utf-8") as f:
        plan = json.load(f)
    glb_parts = glb_extract_parts(glb_path) if (glb_path and os.path.isfile(glb_path)) else {}
    assy = cq.Assembly(name=os.path.basename(os.path.abspath(scene_dir)) or "scene")
    count = 0
    for p in plan.get("parts") or ():
        pid = p.get("id")
        if not pid:
            continue
        shape = None
        ppath = os.path.join(scene_dir, "parts", pid + ".py")
        if os.path.isfile(ppath):
            try:
                with open(ppath, encoding="utf-8") as f:
                    rec = scene3d._extract_recipe(f.read())
                if rec:
                    shape = _recipe_to_cq(rec)
            except Exception:  # noqa: BLE001
                shape = None
        if shape is None and pid in glb_parts:
            try:
                shape = _mesh_to_cq(*glb_parts[pid])
            except Exception:  # noqa: BLE001
                shape = None
        if shape is None:
            continue
        col = _hex_color(p.get("color"))
        assy.add(shape, name=pid, **({"color": col} if col is not None else {}))
        count += 1
    if not count:
        return 0, "no parts could be converted to STEP"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp.step"
    assy.export(tmp, "STEP")
    os.replace(tmp, out_path)
    return count, ""


def orient_and_pack_parts(glb_parts, plan, report=None):
    """Rotate every part so its print_up points to +Z, drop to Z=0, and 2D shelf-pack onto the build plate.
    -> list of (pid, verts_packed_mm Nx3, tris Mx3, color_hex, up_dir)."""
    from cadfabric.blender import printability

    cfg = printability.resolve_print_cfg((plan or {}).get("print") or "bambu_a1")
    bed = [float(x) for x in cfg["bed_mm"]]
    margin = float(cfg.get("margin_mm", 5.0))
    gap = 6.0
    rep_parts = ((report or {}).get("print") or {}).get("parts") or {}

    items = []
    ordered_parts = list((plan or {}).get("parts") or ())
    seen = set()
    for p in ordered_parts:
        pid = p.get("id")
        if not pid or pid in seen or pid not in glb_parts:
            continue
        seen.add(pid)
        v, t = glb_parts[pid]
        if not len(v) or not len(t):
            continue
        up = str(p.get("print_up") or (rep_parts.get(pid) or {}).get("print_up") or "auto").strip().lower()
        if up not in ("+z", "-z", "+x", "-x", "+y", "-y"):
            up = printability.choose_auto_print_up(v, t)
        R = np.asarray(printability.up_rotation_matrix_3x3(up), dtype=np.float64)
        vr = np.asarray(v, dtype=np.float64) @ R.T
        lo = vr.min(axis=0)
        hi = vr.max(axis=0)
        cx, cy = (lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0
        vr_local = np.c_[vr[:, 0] - cx, vr[:, 1] - cy, vr[:, 2] - lo[2]]
        dx, dy, dz = float(hi[0] - lo[0]), float(hi[1] - lo[1]), float(hi[2] - lo[2])
        col = str(p.get("color") or "#D9D9D9").strip().upper()
        if not (len(col) == 7 and col[0] == "#"):
            col = "#D9D9D9"
        items.append({"pid": pid, "v": vr_local, "t": t, "dx": dx, "dy": dy, "dz": dz, "color": col, "up": up})

    for pid in sorted(glb_parts):
        if pid in seen:
            continue
        v, t = glb_parts[pid]
        if not len(v) or not len(t):
            continue
        up = printability.choose_auto_print_up(v, t)
        R = np.asarray(printability.up_rotation_matrix_3x3(up), dtype=np.float64)
        vr = np.asarray(v, dtype=np.float64) @ R.T
        lo, hi = vr.min(axis=0), vr.max(axis=0)
        vr_local = np.c_[vr[:, 0] - (lo[0] + hi[0]) / 2.0, vr[:, 1] - (lo[1] + hi[1]) / 2.0, vr[:, 2] - lo[2]]
        items.append({"pid": pid, "v": vr_local, "t": t, "dx": float(hi[0] - lo[0]), "dy": float(hi[1] - lo[1]), "dz": float(hi[2] - lo[2]), "color": "#D9D9D9", "up": up})

    if not items:
        return []

    usable_w = max(bed[0] - 2.0 * margin, max(max(it["dx"], it["dy"]) for it in items) + 1.0)
    items.sort(key=lambda it: max(it["dx"], it["dy"]), reverse=True)
    placed = []
    cur_x, cur_y, row_h = 0.0, 0.0, 0.0
    for it in items:
        v, dx, dy = it["v"], it["dx"], it["dy"]
        # Rotate 90 deg around +Z (det=+1) when it helps fit narrow shelves cleanly
        if (dx > dy and cur_x > 0.0 and cur_x + dx > usable_w and cur_x + dy <= usable_w) or (dy > dx and cur_x == 0.0):
            v = np.c_[-v[:, 1], v[:, 0], v[:, 2]]
            dx, dy = dy, dx
        if cur_x > 0.0 and cur_x + dx > usable_w + 1e-6:
            cur_x = 0.0
            cur_y += row_h + gap
            row_h = 0.0
        px = cur_x + dx / 2.0
        py = cur_y + dy / 2.0
        placed.append((it["pid"], v, it["t"], it["color"], it["up"], px, py, dx, dy))
        cur_x += dx + gap
        row_h = max(row_h, dy)

    total_w = max(px + dx / 2.0 for _, _, _, _, _, px, _, dx, _ in placed)
    total_h = max(py + dy / 2.0 for _, _, _, _, _, _, py, _, dy in placed)
    off_x = (bed[0] - total_w) / 2.0 if total_w <= bed[0] - 2.0 * margin else margin
    off_y = (bed[1] - total_h) / 2.0 if total_h <= bed[1] - 2.0 * margin else margin

    out = []
    for pid, v, t, col, up, px, py, _dx, _dy in placed:
        vp = np.c_[v[:, 0] + px + off_x, v[:, 1] + py + off_y, v[:, 2]]
        out.append((pid, vp, t, col, up))
    return out


def export_3mf(scene_dir, glb_path, out_path):
    """Export auto-oriented, build-plate packed, multi-color Bambu/Orca-compatible 3MF archive. -> (part_count, error)."""
    try:
        with open(os.path.join(scene_dir, "plan.json"), encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        return 0, "missing or invalid plan.json: %s" % str(e)[:120]
    report = {}
    rp = os.path.join(scene_dir, "report.json")
    if os.path.isfile(rp):
        try:
            with open(rp, encoding="utf-8") as f:
                report = json.load(f)
        except (OSError, ValueError):
            report = {}
    try:
        glb_parts = glb_extract_parts(glb_path)
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:160]
    packed = orient_and_pack_parts(glb_parts, plan, report=report)
    if not packed:
        return 0, "no mesh parts to export"

    palette = []
    col_idx = {}
    for _pid, _v, _t, col, _up in packed:
        if col not in col_idx:
            col_idx[col] = len(palette)
            palette.append(col)

    base_lines = [
        '      <base name="Slot %d (%s)" displaycolor="%sFF"/>' % (i + 1, _xml_esc(c), _xml_esc(c))
        for i, c in enumerate(palette)
    ]
    obj_blocks = []
    build_lines = []
    cfg_blocks = []
    for idx, (pid, verts, tris, col, _up) in enumerate(packed, start=2):
        pindex = col_idx[col]
        extruder = pindex + 1
        v_lines = ["          <vertex x=\"%.4f\" y=\"%.4f\" z=\"%.4f\"/>" % (p[0], p[1], p[2]) for p in verts]
        t_lines = ["          <triangle v1=\"%d\" v2=\"%d\" v3=\"%d\"/>" % (int(tri[0]), int(tri[1]), int(tri[2])) for tri in tris]
        obj_blocks.append(
            '    <object id="%d" name="%s" type="model" pid="1" pindex="%d">\n'
            '      <mesh>\n'
            '        <vertices>\n%s\n        </vertices>\n'
            '        <triangles>\n%s\n        </triangles>\n'
            '      </mesh>\n'
            '    </object>' % (idx, _xml_esc(pid), pindex, "\n".join(v_lines), "\n".join(t_lines))
        )
        build_lines.append('    <item objectid="%d" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>' % idx)
        cfg_blocks.append(
            '  <object id="%d">\n'
            '    <metadata key="name" value="%s"/>\n'
            '    <metadata key="extruder" value="%d"/>\n'
            '  </object>' % (idx, _xml_esc(pid), extruder)
        )

    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        '  <metadata name="Application">CadFabric</metadata>\n'
        '  <resources>\n'
        '    <basematerials id="1">\n' + "\n".join(base_lines) + '\n    </basematerials>\n'
        + "\n".join(obj_blocks) + '\n'
        '  </resources>\n'
        '  <build>\n' + "\n".join(build_lines) + '\n  </build>\n'
        '</model>\n'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        '  <Default Extension="config" ContentType="text/plain"/>\n'
        '</Types>\n'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        '</Relationships>\n'
    )
    model_cfg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<config>\n' + "\n".join(cfg_blocks) + '\n</config>\n'
    )

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp.3mf"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types.encode("utf-8"))
        zf.writestr("_rels/.rels", rels_xml.encode("utf-8"))
        zf.writestr("3D/3dmodel.model", model_xml.encode("utf-8"))
        zf.writestr("Metadata/model_settings.config", model_cfg.encode("utf-8"))
    os.replace(tmp, out_path)
    return len(packed), ""


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "step" and len(sys.argv) >= 5:
        cnt, err = export_step(sys.argv[2], sys.argv[3], sys.argv[4])
        if err:
            sys.stderr.write(err + "\n")
            raise SystemExit(1)
        print(json.dumps({"ok": True, "parts": cnt}))
    elif mode == "3mf" and len(sys.argv) >= 5:
        cnt, err = export_3mf(sys.argv[2], sys.argv[3], sys.argv[4])
        if err:
            sys.stderr.write(err + "\n")
            raise SystemExit(1)
        print(json.dumps({"ok": True, "parts": cnt}))
    elif mode == "stl" and len(sys.argv) >= 4:
        pid = sys.argv[4] if len(sys.argv) > 4 else None
        cnt, err = export_stl(sys.argv[2], sys.argv[3], pid)
        if err:
            sys.stderr.write(err + "\n")
            raise SystemExit(1)
        print(json.dumps({"ok": True, "triangles": cnt}))
    else:
        raise SystemExit(2)
