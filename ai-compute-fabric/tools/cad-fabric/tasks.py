"""Benchmark tasks. The exact answer is computed by the kernel from a reference solution (never typed in by hand), and cross-checked analytically where a closed form exists."""
import math

TASKS = {
    "cup_side_hole": {
        "text": "Tao part moi: coc hinh tru duong kinh ngoai 40 mm, cao 50 mm. Khoet ruot thanh dung 3 mm, mo mat tren (day coc cung day 3 mm). Sau do khoan mot lo duong kinh 10 mm xuyen qua CHI MOT thanh ben cua coc, o do cao 25 mm; thanh doi dien khong duoc bi khoan. Cuoi cung bao cao the tich.",
        "ref": "cup = cq.Workplane('XY').circle(20).extrude(50).faces('>Z').shell(-3)\ncutter = cq.Workplane('YZ').workplane(offset=0).center(0, 25).circle(5).extrude(30)\nresult = cup.cut(cutter)",
        "tol": 0.0005, "analytic": None},
    "side_hole_block": {
        "text": "Tao part moi: khoi 40 x 30 x 20 mm. Khoan mot lo duong kinh 6 mm xuyen het khoi theo chieu dai 40 mm (lo ngang, nam o giua mat 30 x 20). Cuoi cung bao cao the tich.",
        "ref": "result = cq.Workplane('XY').box(40,30,20).faces('>X').workplane().hole(6)", "tol": 0.002, "analytic": 40 * 30 * 20 - math.pi * 9 * 40},
    "L_bracket": {
        "text": "Tao part moi: thanh chu L: tiet dien chu L co hai canh 40 mm x 40 mm, day 5 mm moi canh, keo dai 60 mm. Cuoi cung bao cao the tich.",
        "ref": "result = cq.Workplane('XY').polyline([(0,0),(40,0),(40,5),(5,5),(5,40),(0,40)]).close().extrude(60)", "tol": 0.002, "analytic": (40 * 5 + 35 * 5) * 60},
    "step_shaft": {
        "text": "Tao part moi: truc bac xoay tron: doan 1 duong kinh 20 mm dai 30 mm, noi lien doan 2 duong kinh 30 mm dai 20 mm, cung truc. Cuoi cung bao cao the tich.",
        "ref": "result = cq.Workplane('XY').circle(10).extrude(30).faces('>Z').workplane().circle(15).extrude(20)", "tol": 0.0005, "analytic": math.pi * (100 * 30 + 225 * 20)},
    "pocket": {
        "text": "Tao part moi: tam 80 x 50 x 10 mm. Khoet mot tui chu nhat 40 x 10 mm sau 5 mm o chinh giua mat tren (khong xuyen thung). Cuoi cung bao cao the tich.",
        "ref": "result = cq.Workplane('XY').box(80,50,10).faces('>Z').workplane().rect(40,10).cutBlind(-5)", "tol": 0.002, "analytic": 80 * 50 * 10 - 40 * 10 * 5},
}


def exact(backend, tid):
    """Kernel volume of the reference solution."""
    backend.reset()
    r = backend.execute(TASKS[tid]["ref"])
    if not r.ok or not r.measure:
        raise RuntimeError("reference for %s failed: %s" % (tid, r.error))
    return r.measure.volume


def passed(vol, exp, tol):
    return vol is not None and abs(vol - exp) <= tol * exp
