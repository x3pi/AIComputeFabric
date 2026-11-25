---
name: printable-design-blender
description: Design watertight, 3D-printable multi-part and multi-color assemblies in Blender (Bambu Lab A1 AMS 4-color, FDM 0.4/0.6mm, SLA, SLS) with exact tolerances, print orientation (print_up), shell/cut bxlib recipes, snap-fits, screw bosses, and print-in-place hinges.
license: MIT
metadata:
  audience: both
---

# Printable 3D Design in Blender (Bambu Lab A1 AMS Lite & FDM/SLA/SLS)

## 1. Printer Profiles & Thresholds
Declare `"print": {"profile": "<id>"}` at top level of the plan (default `"bambu_a1"` unless the user specifies another printer):

| Profile ID | Machine / Tech | Usable Bed (mm) | Min Wall / Rec Wall | Min Pin / Emboss | Mating Gap | Print-in-Place Hinge Gap | Max Overhang | Max AMS Colors |
|---|---|---|---|---|---|---|---|---|
| `bambu_a1` | Bambu Lab A1 + AMS Lite (`0.4 mm`) | `246 × 246 × 256` | `1.0` / `1.6 mm` | `1.6 mm` | `0.15–0.25 mm` | `0.30–0.40 mm` | `45°` | **4** |
| `bambu_a1_mini` | Bambu Lab A1 Mini (`0.4 mm`) | `170 × 170 × 180` | `1.0` / `1.6 mm` | `1.6 mm` | `0.15–0.25 mm` | `0.30–0.40 mm` | `45°` | **4** |
| `fdm_04` | Generic FDM (`0.4 mm`) | `210 × 210 × 250` | `1.0` / `1.6 mm` | `1.6 mm` | `0.20 mm` | `0.35 mm` | `45°` | — |
| `fdm_06` | Fast Structural FDM (`0.6 mm`) | `246 × 246 × 256` | `1.4` / `2.0 mm` | `2.0 mm` | `0.22 mm` | `0.40 mm` | `45°` | 4 |
| `sla` | MSLA Resin (`0.05 mm`) | `214 × 119 × 220` | `0.8` / `1.2 mm` | `0.6 mm` | `0.10 mm` | `0.20 mm` | `50°` | 1 |
| `sls` | Nylon Powder | `154 × 154 × 200` | `0.8` / `1.2 mm` | `0.8 mm` | `0.25 mm` | `0.40 mm` | Any | 1 |

## 2. Bambu Lab A1 AMS Lite 4-Color Rules
- **Hard 4-color limit**: keep unique `#RRGGBB` values across the entire scene `<= 4` (`max_colors: 4`).
- **Color-by-part = 0 purge waste**: put each accent color (buttons, badges, status rings, latches, lids) on its own separate watertight part with `0.15 mm` mating clearance rather than adding vertical multi-color inlays onto tall vertical walls.
- Group tiny accents of the same color cleanly; each distinct color maps to an AMS extruder slot (`1..4`) in the exported `.3mf`.

## 3. Watertight CSG & Zero-Overlap Clearances
- **Never overlap meshes**: `print_overlap` rejects interpenetrating solids even if listed in `allowed_contacts`. Mating parts (lid rim inside box, button in bezel, hinge pin inside knuckle) MUST have explicit air clearance.
- **Tolerances (FDM `0.4 mm` / Bambu A1)**:
  - **Press-fit / color inlay**: `0.12–0.15 mm` per side.
  - **Removable lid / snap clasp / slider**: `0.20–0.25 mm` per side (`0.40–0.50 mm` total clearance).
  - **Print-in-place hinge / axle (`joints`)**: **`0.35 mm` radial and `0.35 mm` axial gap** (`>= 0.30 mm` minimum) between `parent` knuckles and `children` pins/knuckles so layers never fuse!
  - **M3 fasteners**: `r = 1.7 mm` (`Ø3.4 mm`) through-hole; heat-set boss `r_out = 4.2 mm`, hole `r = 2.0 mm`; self-tap M3 pilot `r = 1.4 mm`.
  - **Horizontal circular holes**: keep diameters `<= 10 mm` or chamfer/teardrop top to stay under `45°` overhang.

## 4. Print Orientation (`print_up`) & Splitting Oversized Parts
- Build EVERY part in its **assembled world pose** (so 3D preview and hinge animation look right), and declare `"print_up": "+z" | "-z" | "+x" | "-x" | "+y" | "-y" | "auto"` on every part:
  - Bottom enclosure tray (`open: "+z"`) -> `"print_up": "+z"` (solid base flat on build plate).
  - Top lid (`open: "-z"`) -> `"print_up": "-z"` (exporter flips its flat top onto `Z = 0` so no supports are needed!).
  - Vertical bracket or clip -> orient its largest flat face toward `-print_up`.
- If a part exceeds `246 × 246 mm` on Bambu A1, split it into interlocking halves with keyed tenon/mortise alignment pins (`0.15 mm` clearance) and M3 screw tabs.

## 5. Canonical `bxlib` Recipes (`shell` & `"mode": "cut"`)
ALWAYS prefer `bxlib` recipes over raw `bmesh` code; multi-op `"add"`/`"cut"` and `"shell"` use Exact Boolean CSG to produce true closed 2-manifold solids:

- **Hollow Electronics Enclosure Base (with USB-C slot + M3 corner screw holes)**:
```json
{"ops": [
  {"op": "shell", "center": [0, 0, 15], "size": [100, 70, 30], "wall": 2.4, "open": "+z", "bevel": 3.0},
  {"op": "cylinder", "a": [-43, -28, 2.4], "b": [-43, -28, 26], "r": 4.2, "mode": "add"},
  {"op": "cylinder", "a": [-43, -28, -1], "b": [-43, -28, 28], "r": 1.7, "mode": "cut"},
  {"op": "box", "center": [50, 0, 12], "size": [8, 12, 6], "bevel": 1.2, "mode": "cut"}
]}
```
- **Flipped Top Snap-Fit Lid (`"print_up": "-z"`, `0.22 mm` clearance around base lip)**:
```json
{"ops": [
  {"op": "shell", "center": [0, 0, 34], "size": [100, 70, 8], "wall": 2.0, "open": "-z", "bevel": 2.0},
  {"op": "shell", "center": [0, 0, 28], "size": [94.76, 64.76, 4], "wall": 1.6, "open": "-z", "mode": "add"}
]}
```
*(Notice outer base inner opening is `100 - 2*2.4 = 95.2 mm`; lid inner lip `94.76 mm` gives `(95.2 - 94.76)/2 = 0.22 mm` slip/snap clearance on all 4 sides!)*

