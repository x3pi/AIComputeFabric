"""Deterministic 3D-printability geometry gate (domain-free, profile-driven).
Pure-Python constants/orientation helpers may be imported anywhere; `check_scene(plan)` runs inside Blender."""
import math

PRINT_UP_VALUES = ("auto", "+z", "-z", "+x", "-x", "+y", "-y")

PRINT_PROFILES = {
    "bambu_a1": {
        "profile": "bambu_a1",
        "label": "Bambu Lab A1 (AMS 4 màu · 256×256×256 mm)",
        "tech": "fdm",
        "nozzle_mm": 0.4,
        "layer_mm": 0.20,
        "bed_mm": [256.0, 256.0, 256.0],
        "margin_mm": 5.0,
        "min_wall_mm": 1.0,
        "rec_wall_mm": 1.2,
        "min_feature_mm": 1.2,
        "min_clearance_mm": 0.15,
        "hinge_clearance_mm": 0.30,
        "max_overhang_deg": 50.0,
        "max_bridge_mm": 25.0,
        "max_colors": 4,
    },
    "bambu_a1_mini": {
        "profile": "bambu_a1_mini",
        "label": "Bambu Lab A1 Mini (AMS 4 màu · 180×180×180 mm)",
        "tech": "fdm",
        "nozzle_mm": 0.4,
        "layer_mm": 0.20,
        "bed_mm": [180.0, 180.0, 180.0],
        "margin_mm": 5.0,
        "min_wall_mm": 1.0,
        "rec_wall_mm": 1.2,
        "min_feature_mm": 1.2,
        "min_clearance_mm": 0.15,
        "hinge_clearance_mm": 0.30,
        "max_overhang_deg": 50.0,
        "max_bridge_mm": 25.0,
        "max_colors": 4,
    },
    "fdm_04": {
        "profile": "fdm_04",
        "label": "FDM chuẩn vòi 0.4 mm (220×220×250 mm)",
        "tech": "fdm",
        "nozzle_mm": 0.4,
        "layer_mm": 0.20,
        "bed_mm": [220.0, 220.0, 250.0],
        "margin_mm": 5.0,
        "min_wall_mm": 1.0,
        "rec_wall_mm": 1.2,
        "min_feature_mm": 1.2,
        "min_clearance_mm": 0.15,
        "hinge_clearance_mm": 0.30,
        "max_overhang_deg": 50.0,
        "max_bridge_mm": 25.0,
        "max_colors": 0,
    },
    "fdm_06": {
        "profile": "fdm_06",
        "label": "FDM vòi 0.6 mm (256×256×256 mm)",
        "tech": "fdm",
        "nozzle_mm": 0.6,
        "layer_mm": 0.30,
        "bed_mm": [256.0, 256.0, 256.0],
        "margin_mm": 5.0,
        "min_wall_mm": 1.4,
        "rec_wall_mm": 1.8,
        "min_feature_mm": 1.6,
        "min_clearance_mm": 0.20,
        "hinge_clearance_mm": 0.40,
        "max_overhang_deg": 50.0,
        "max_bridge_mm": 25.0,
        "max_colors": 4,
    },
    "sla": {
        "profile": "sla",
        "label": "SLA Resin (218×123×220 mm)",
        "tech": "sla",
        "nozzle_mm": 0.05,
        "layer_mm": 0.05,
        "bed_mm": [218.0, 123.0, 220.0],
        "margin_mm": 2.0,
        "min_wall_mm": 0.8,
        "rec_wall_mm": 1.2,
        "min_feature_mm": 0.5,
        "min_clearance_mm": 0.10,
        "hinge_clearance_mm": 0.20,
        "max_overhang_deg": 55.0,
        "max_bridge_mm": 10.0,
        "max_colors": 1,
    },
    "sls": {
        "profile": "sls",
        "label": "SLS Powder (160×160×200 mm)",
        "tech": "sls",
        "nozzle_mm": 0.10,
        "layer_mm": 0.10,
        "bed_mm": [160.0, 160.0, 200.0],
        "margin_mm": 3.0,
        "min_wall_mm": 0.8,
        "rec_wall_mm": 1.0,
        "min_feature_mm": 0.8,
        "min_clearance_mm": 0.25,
        "hinge_clearance_mm": 0.40,
        "max_overhang_deg": 89.0,
        "max_bridge_mm": 40.0,
        "max_colors": 1,
    },
}

_ROT_3X3 = {
    "+z": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    "-z": [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]],
    "+x": [[0.0, 0.0, -1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]],
    "-x": [[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]],
    "+y": [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]],
    "-y": [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]],
}


def resolve_print_cfg(raw):
    """Normalize `plan.get('print')` into a full profile config dict, or None when disabled."""
    if not raw:
        return None
    if raw is True:
        cfg = dict(PRINT_PROFILES["bambu_a1"])
        cfg["in_place"] = False
        return cfg
    if isinstance(raw, str):
        key = raw.strip().lower()
        cfg = dict(PRINT_PROFILES.get(key, PRINT_PROFILES["bambu_a1"]))
        cfg["in_place"] = False
        return cfg
    if not isinstance(raw, dict):
        return None
    prof = str(raw.get("profile") or "bambu_a1").strip().lower()
    cfg = dict(PRINT_PROFILES.get(prof, PRINT_PROFILES["bambu_a1"]))
    cfg["in_place"] = bool(raw.get("in_place", False))
    for k in ("tech", "nozzle_mm", "layer_mm", "bed_mm", "margin_mm", "min_wall_mm", "rec_wall_mm",
              "min_feature_mm", "min_clearance_mm", "hinge_clearance_mm", "max_overhang_deg", "max_bridge_mm", "max_colors", "label"):
        if k in raw and raw[k] is not None:
            cfg[k] = list(raw[k]) if k == "bed_mm" else raw[k]
    return cfg


def up_rotation_matrix_3x3(print_up):
    """Proper rotation matrix (det=+1) bringing `print_up` direction onto world +Z."""
    return [list(r) for r in _ROT_3X3.get(print_up, _ROT_3X3["+z"])]


def _tri_data(verts, tris):
    """Compute per-triangle area, outward unit normal, and vertex indices as plain lists."""
    out = []
    for tri in tris:
        i0, i1, i2 = int(tri[0]), int(tri[1]), int(tri[2])
        p0, p1, p2 = verts[i0], verts[i1], verts[i2]
        e1x, e1y, e1z = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        e2x, e2y, e2z = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
        cx = e1y * e2z - e1z * e2y
        cy = e1z * e2x - e1x * e2z
        cz = e1x * e2y - e1y * e2x
        a2 = math.sqrt(cx * cx + cy * cy + cz * cz)
        if a2 <= 1e-12:
            continue
        area = 0.5 * a2
        out.append((i0, i1, i2, area, cx / a2, cy / a2, cz / a2))
    return out


def choose_auto_print_up(verts_mm, tris):
    """Pick the cardinal print_up maximizing flat bed contact area minus unsupported overhangs."""
    if not len(verts_mm) or not len(tris):
        return "+z"
    tinfo = _tri_data(verts_mm, tris)
    if not tinfo:
        return "+z"
    prefs = {"+z": 3e-3, "-z": 2e-3, "+y": 1e-3, "-y": 1e-3, "+x": 0.0, "-x": 0.0}
    best_up, best_score = "+z", float("-inf")
    for cand in ("+z", "-z", "+x", "-x", "+y", "-y"):
        rz = _ROT_3X3[cand][2]
        pz = [rz[0] * float(v[0]) + rz[1] * float(v[1]) + rz[2] * float(v[2]) for v in verts_mm]
        z_min = min(pz)
        z_h = max(pz) - z_min
        contact, overhang = 0.0, 0.0
        for i0, i1, i2, area, nx, ny, nz in tinfo:
            rnz = rz[0] * nx + rz[1] * ny + rz[2] * nz
            tri_z_min = min(pz[i0], pz[i1], pz[i2])
            tri_z_max = max(pz[i0], pz[i1], pz[i2])
            if rnz < -0.95 and tri_z_max <= z_min + 0.35:
                contact += area
            elif rnz < -0.64 and tri_z_min > z_min + 0.5:
                overhang += area
        score = contact - 0.35 * overhang - 0.05 * z_h + prefs[cand]
        if score > best_score:
            best_score, best_up = score, cand
    return best_up


def _oriented_size_mm(verts_m, up_dir):
    r = _ROT_3X3.get(up_dir, _ROT_3X3["+z"])
    xs, ys, zs = [], [], []
    for co in verts_m:
        x, y, z = float(co[0]) * 1000.0, float(co[1]) * 1000.0, float(co[2]) * 1000.0
        xs.append(r[0][0] * x + r[0][1] * y + r[0][2] * z)
        ys.append(r[1][0] * x + r[1][1] * y + r[1][2] * z)
        zs.append(r[2][0] * x + r[2][1] * y + r[2][2] * z)
    return [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]


def _wall_thickness_stats(bm_tri, tree, min_wall_mm):
    """Raycast inward along -face.normal across non-tiny faces; ignores chamfers/corners via opposing-normal filter."""
    lim_m = float(min_wall_mm) * 0.001 + 1e-4
    cands = [f for f in bm_tri.faces if f.calc_area() >= 2e-7]
    if not cands:
        return None
    step = max(1, len(cands) // 300)
    sampled = cands[::step]
    tot_area, thin_area = 0.0, 0.0
    hits, thin_hits = [], []
    for f in sampled:
        area = f.calc_area()
        tot_area += area
        n1 = f.normal
        c = f.calc_center_median()
        hit = tree.ray_cast(c - n1 * 2e-5, -n1, lim_m)
        if hit[0] is None or hit[2] is None:
            continue
        hf = bm_tri.faces[hit[2]]
        if hf.calc_area() < 2e-7 or n1.dot(hf.normal) >= -0.85:
            continue
        d_mm = (float(hit[3]) + 2e-5) * 1000.0
        if d_mm <= 0.02:
            continue
        hits.append(d_mm)
        if d_mm < float(min_wall_mm) - 0.03:
            thin_hits.append(d_mm)
            thin_area += area
    if not hits:
        return None
    return {
        "min_mm": min(hits),
        "thin_count": len(thin_hits),
        "thin_min_mm": min(thin_hits) if thin_hits else None,
        "thin_area_ratio": (thin_area / tot_area) if tot_area > 0 else 0.0,
    }


def _box_gap_m(ba, bb):
    d2 = 0.0
    for i in range(3):
        g = max(ba[0][i] - bb[1][i], bb[0][i] - ba[1][i], 0.0)
        d2 += g * g
    return d2 ** 0.5


_OBLIQUE_DIRS = None


def _point_inside_bvh(tree, pt):
    """Majority-vote 3-ray parity raycast along non-axis-aligned directions (avoids vertical/edge grazing on boxes)."""
    from mathutils import Vector
    global _OBLIQUE_DIRS
    if _OBLIQUE_DIRS is None:
        _OBLIQUE_DIRS = [
            Vector((0.37139067, 0.55708601, 0.74278135)).normalized(),
            Vector((-0.61237244, 0.48989795, 0.62097141)).normalized(),
            Vector((0.52103412, -0.73412891, 0.43528901)).normalized(),
        ]
    inside_votes = 0
    for d in _OBLIQUE_DIRS:
        hits = 0
        cur = pt.copy()
        for _ in range(32):
            res = tree.ray_cast(cur, d)
            if res[0] is None:
                break
            hits += 1
            cur = res[0] + d * 1e-5
        if (hits % 2) == 1:
            inside_votes += 1
    return inside_votes >= 2


def _in_box(pt, box):
    return box[0][0] <= pt[0] <= box[1][0] and box[0][1] <= pt[1] <= box[1][1] and box[0][2] <= pt[2] <= box[1][2]


def _pair_penetration_mm(pts_a, box_a, tree_b, pts_b, box_b, tree_a):
    """Measure deepest strict interior penetration between two meshes when their BVHs overlap."""
    worst_m = 0.0
    step_a = max(1, len(pts_a) // 250)
    for p in pts_a[::step_a]:
        if _in_box(p, box_b) and _point_inside_bvh(tree_b, p):
            near = tree_b.find_nearest(p)
            if near and near[3] is not None and near[3] > worst_m:
                worst_m = float(near[3])
    step_b = max(1, len(pts_b) // 250)
    for p in pts_b[::step_b]:
        if _in_box(p, box_a) and _point_inside_bvh(tree_a, p):
            near = tree_a.find_nearest(p)
            if near and near[3] is not None and near[3] > worst_m:
                worst_m = float(near[3])
    return worst_m * 1000.0


def _pair_surface_gap_mm(pts_a, tree_b, pts_b, tree_a):
    """Approximate minimum surface gap (mm) between two non-penetrating meshes."""
    best = 1e9
    step_a = max(1, len(pts_a) // 350)
    for p in pts_a[::step_a]:
        near = tree_b.find_nearest(p)
        if near and near[3] is not None and near[3] < best:
            best = float(near[3])
    step_b = max(1, len(pts_b) // 350)
    for p in pts_b[::step_b]:
        near = tree_a.find_nearest(p)
        if near and near[3] is not None and near[3] < best:
            best = float(near[3])
    return best * 1000.0


def check_scene(plan):
    """Run all deterministic printability checks inside Blender. -> (issues_list, summary_dict)."""
    import bpy, bmesh
    from mathutils.bvhtree import BVHTree

    cfg = resolve_print_cfg(plan.get("print"))
    if not cfg:
        return [], None

    issues = []
    part_summary = {}
    trees = {}
    pts_map = {}
    boxes = {}
    manifold_map = {}
    colors = set()

    bed = [float(x) for x in cfg["bed_mm"]]
    margin = float(cfg.get("margin_mm", 5.0))
    ux = max(10.0, bed[0] - 2.0 * margin)
    uy = max(10.0, bed[1] - 2.0 * margin)
    uz = max(10.0, bed[2])

    for p in plan.get("parts") or ():
        pid = p.get("id")
        if not pid:
            continue
        col = p.get("color")
        if isinstance(col, str) and len(col) == 7 and col[0] == "#":
            colors.add(col.lower())
        o = bpy.data.objects.get(pid)
        if o is None or o.type != "MESH" or not o.data.vertices:
            continue

        bm = bmesh.new()
        bm_tri = None
        try:
            bm.from_mesh(o.data)
            bm.transform(o.matrix_world)
            bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
            bm.normal_update()

            bad_edges = sum(1 for e in bm.edges if len(e.link_faces) != 2)
            watertight = (len(bm.faces) > 0 and bad_edges == 0)
            manifold_map[pid] = watertight
            if not watertight:
                issues.append({
                    "kind": "non_manifold",
                    "part": pid,
                    "open_or_nonmanifold_edges": bad_edges,
                    "hint": "3D printable mesh must be closed watertight 2-manifold; use bxlib recipes (shell / mode='cut') instead of open faces",
                })
            else:
                try:
                    svol = float(bm.calc_volume(signed=True))
                except Exception:  # noqa: BLE001
                    svol = 1.0
                if svol < -1e-12:
                    issues.append({
                        "kind": "inverted_normals",
                        "part": pid,
                        "hint": "part faces are inside-out; recalculate outward normals",
                    })

            bm_tri = bm.copy()
            bmesh.ops.triangulate(bm_tri, faces=bm_tri.faces)
            bm_tri.verts.ensure_lookup_table()
            bm_tri.edges.ensure_lookup_table()
            bm_tri.faces.ensure_lookup_table()
            bm_tri.normal_update()
            tree = BVHTree.FromBMesh(bm_tri)
            trees[pid] = tree

            verts_mm = [[v.co.x * 1000.0, v.co.y * 1000.0, v.co.z * 1000.0] for v in bm_tri.verts]
            tris = [[f.verts[0].index, f.verts[1].index, f.verts[2].index] for f in bm_tri.faces if len(f.verts) == 3]
            spec_up = str(p.get("print_up") or "auto").strip().lower()
            if spec_up in ("+z", "-z", "+x", "-x", "+y", "-y"):
                up_dir = spec_up
            else:
                up_dir = choose_auto_print_up(verts_mm, tris)

            osize = _oriented_size_mm([v.co for v in bm_tri.verts], up_dir)
            if min(osize) < float(cfg["min_feature_mm"]) - 0.02:
                issues.append({
                    "kind": "feature_too_small",
                    "part": pid,
                    "min_dim_mm": round(min(osize), 2),
                    "required_mm": cfg["min_feature_mm"],
                    "hint": "smallest dimension %.2f mm is below min_feature %.2f mm for %s" % (min(osize), cfg["min_feature_mm"], cfg["profile"]),
                })

            fits_xy = (osize[0] <= ux + 0.05 and osize[1] <= uy + 0.05) or (osize[1] <= ux + 0.05 and osize[0] <= uy + 0.05)
            fits_z = osize[2] <= uz + 0.05
            fits_bed = bool(fits_xy and fits_z)
            if not fits_bed:
                issues.append({
                    "kind": "exceeds_build_volume",
                    "part": pid,
                    "oriented_size_mm": [round(x, 1) for x in osize],
                    "usable_bed_mm": [round(ux, 1), round(uy, 1), round(uz, 1)],
                    "print_up": up_dir,
                    "hint": "reorient print_up or split oversized part into smaller interlocking pieces with keyed tenons/tabs",
                })

            measured_min_wall = None
            if watertight:
                wstats = _wall_thickness_stats(bm_tri, tree, cfg["min_wall_mm"])
                if wstats:
                    measured_min_wall = round(wstats["min_mm"], 2)
                    if wstats["thin_count"] >= 3 and (wstats["thin_area_ratio"] >= 0.02 or (wstats["thin_min_mm"] or 999.0) < float(cfg["min_wall_mm"]) * 0.85):
                        issues.append({
                            "kind": "wall_too_thin",
                            "part": pid,
                            "measured_min_wall_mm": round(wstats["thin_min_mm"], 2),
                            "required_mm": cfg["min_wall_mm"],
                            "hint": "opposing wall %.2f mm is thinner than %.2f mm (%s); thicken wall/shell" % (wstats["thin_min_mm"], cfg["min_wall_mm"], cfg["profile"]),
                        })

            pts = [v.co.copy() for v in bm_tri.verts] + [f.calc_center_median() for f in bm_tri.faces]
            pts_map[pid] = pts
            lo = [min(q[i] for q in pts) for i in range(3)]
            hi = [max(q[i] for q in pts) for i in range(3)]
            boxes[pid] = (lo, hi)

            part_summary[pid] = {
                "print_up": up_dir,
                "oriented_size_mm": [round(x, 1) for x in osize],
                "watertight": watertight,
                "fits_bed": fits_bed,
                "min_wall_mm": measured_min_wall,
            }
        finally:
            if bm_tri is not None:
                bm_tri.free()
            bm.free()

    # Pairwise penetration check (all distinct pairs, including allowed_contacts, must not intersect > 0.08 mm when 3D printing)
    pids = sorted(trees)
    checked_hinge_pairs = set()
    for j in plan.get("joints") or ():
        par = j.get("parent")
        if par and par in trees:
            for ch in j.get("children") or ():
                if ch in trees and ch != par:
                    checked_hinge_pairs.add(tuple(sorted((par, ch))))
                    if _box_gap_m(boxes[par], boxes[ch]) > 0.01:
                        continue
                    pen_mm = _pair_penetration_mm(pts_map[par], boxes[par], trees[ch], pts_map[ch], boxes[ch], trees[par]) if trees[par].overlap(trees[ch]) else 0.0
                    gap_mm = 0.0 if pen_mm > 0.0 else _pair_surface_gap_mm(pts_map[par], trees[ch], pts_map[ch], trees[par])
                    req_gap = float(cfg.get("hinge_clearance_mm", 0.30))
                    if pen_mm > 0.08 or gap_mm < req_gap - 0.03:
                        issues.append({
                            "kind": "clearance_too_tight",
                            "joint": j.get("name"),
                            "pair": "%s|%s" % (par, ch),
                            "gap_mm": round(-pen_mm if pen_mm > 0.0 else gap_mm, 3),
                            "required_mm": req_gap,
                            "hint": "print-in-place / moving joint needs >= %.2f mm air gap between %s and %s so parts do not fuse" % (req_gap, par, ch),
                        })

    for i, a in enumerate(pids):
        for b in pids[i + 1:]:
            pair = (a, b)
            if pair in checked_hinge_pairs:
                continue
            if _box_gap_m(boxes[a], boxes[b]) > 0.002:
                continue
            if trees[a].overlap(trees[b]):
                pen_mm = _pair_penetration_mm(pts_map[a], boxes[a], trees[b], pts_map[b], boxes[b], trees[a])
                if pen_mm > 0.08:
                    issues.append({
                        "kind": "print_overlap",
                        "pair": "%s|%s" % (a, b),
                        "penetration_mm": round(pen_mm, 2),
                        "hint": "3D printable parts cannot interpenetrate (%.2f mm overlap); add >= %.2f mm clearance between mating parts" % (pen_mm, cfg["min_clearance_mm"]),
                    })

    if cfg.get("in_place") and boxes:
        s_lo = [min(b[0][i] for b in boxes.values()) * 1000.0 for i in range(3)]
        s_hi = [max(b[1][i] for b in boxes.values()) * 1000.0 for i in range(3)]
        s_sz = [s_hi[i] - s_lo[i] for i in range(3)]
        fits_all = (
            ((s_sz[0] <= ux + 0.05 and s_sz[1] <= uy + 0.05) or (s_sz[1] <= ux + 0.05 and s_sz[0] <= uy + 0.05))
            and s_sz[2] <= uz + 0.05
        )
        if not fits_all:
            issues.append({
                "kind": "exceeds_build_volume",
                "part": "scene_in_place",
                "oriented_size_mm": [round(x, 1) for x in s_sz],
                "usable_bed_mm": [round(ux, 1), round(uy, 1), round(uz, 1)],
                "print_up": "+z",
                "hint": "print-in-place assembly exceeds usable bed dimensions; scale down or curl/pose tighter",
            })

    summary = {
        "profile": cfg["profile"],
        "label": cfg.get("label") or cfg["profile"],
        "bed_mm": bed,
        "min_wall_mm": cfg["min_wall_mm"],
        "hinge_clearance_mm": cfg["hinge_clearance_mm"],
        "max_colors": int(cfg.get("max_colors") or 0),
        "colors_used": len(colors),
        "all_watertight": bool(part_summary) and all(v["watertight"] for v in part_summary.values()),
        "in_place": bool(cfg.get("in_place", False)),
        "parts": part_summary,
    }
    return issues, summary

