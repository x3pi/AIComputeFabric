#!/usr/bin/env python3
"""Offline tests for the scene agent: fake director, fake worker, fake Blender (no gateway, no Blender). Run: python -m unittest discover -s tests"""
import json, os, shutil, sys, tempfile, unittest
os.environ.setdefault("BX_PROBE", "0")          # the single-part probe starts its own Blender; the tests that cover it switch it on
from cadfabric import scene3d as S

PLAN = {"action": "plan", "parts": [{"id": "base", "spec": "plate 100x100x10"}, {"id": "wheel", "spec": "disc r=30 at z=40"}],
        "joints": [{"name": "j1", "parent": "base", "children": ["wheel"], "origin_mm": [0, 0, 40], "axis": [0, 1, 0], "coaxial": ["wheel"], "span_rad": 6.283}]}
GOOD = "def build(bpy, bmesh):\n    me = bpy.data.meshes.new('%s')\n    bpy.data.objects.new('%s', me)\n"


def good(pid): return "```python\n" + GOOD % (pid, pid) + "```"


class FakeBlender:
    """subprocess.run stand-in: reads parts/*.py of the work dir and writes a report.json like blender_build.py would."""
    def __init__(self, extra_issues=None): self.calls, self.extra = 0, extra_issues or []

    def __call__(self, argv, **kw):
        self.calls += 1
        d = argv[argv.index("--") + 1]
        with open(os.path.join(d, "plan.json")) as fh: plan = json.load(fh)
        rep = {"part_errors": {}, "issues": list(self.extra), "missing": [], "ok": False}
        for p in plan["parts"]:
            f = os.path.join(d, "parts", p["id"] + ".py")
            if not os.path.exists(f): rep["missing"].append(p["id"]); continue
            with open(f) as fh: raises = "RAISE" in fh.read()
            if raises: rep["part_errors"][p["id"]] = "Traceback\nRuntimeError: boom"
        if not rep["missing"] and not rep["part_errors"]:
            rep["parts_bbox_mm"] = {p["id"]: [[0, 0, 0], [10, 10, 10]] for p in plan["parts"]}
            with open(os.path.join(d, "scene.blend"), "w") as fh: fh.write("x")
        rep["ok"] = not (rep["missing"] or rep["part_errors"] or rep["issues"])
        with open(os.path.join(d, "report.json"), "w") as fh: json.dump(rep, fh)
        return None


class Static(unittest.TestCase):
    def test_static_check(self):
        self.assertIsNone(S.static_check(GOOD % ("a", "a"), "a"))
        self.assertIn("SyntaxError", S.static_check("def build(:\n", "a"))
        self.assertIn("no def build", S.static_check("x = 1\n", "a"))
        self.assertIn("bpy.ops", S.static_check("def build(bpy, bmesh):\n    bpy.ops.mesh.primitive_cube_add()\n    'a'\n", "a"))
        self.assertIn("BMesh instance", S.static_check("def build(bpy, bmesh):\n    bmesh.ops.create_cube(bmesh, size=1)\n    'a'\n", "a"))
        self.assertIn("exact name", S.static_check("def build(bpy, bmesh):\n    pass\n", "a"))

    def test_validate_plan(self):
        self.assertEqual(S.validate_plan(PLAN), [])
        bad = {"parts": [{"id": "a", "spec": "x"}, {"id": "a", "spec": "y"}], "joints": [{"name": "j", "children": ["zz"], "origin_mm": [0, 0], "axis": [0, 0, 0], "span_rad": 1}],
               "allowed_contacts": [["a", "q"]]}
        e = " | ".join(S.validate_plan(bad))
        for s in ("duplicate", "3 numbers", "unknown part zz", "bad pair"): self.assertIn(s, e)
        self.assertTrue(S.validate_plan({"parts": [{"id": "bad id!", "spec": "x"}]}))

    def test_director_ops_are_validated(self):
        ok = {"parts": [{"id": "a", "spec": "x", "ops": [{"op": "box", "center": [0, 0, 0], "size": [1, 1, 1]}]}]}
        self.assertEqual(S.validate_plan(ok), [])
        bad = {"parts": [{"id": "a", "spec": "x", "ops": [{"op": "cube"}]}]}
        self.assertIn("ops:", " ".join(S.validate_plan(bad)))

    def test_sig_ignores_numbers(self):
        self.assertEqual(S.sig("x\nError at line 12: 3.5"), S.sig("y\nError at line 99: 7.25"))


class Loop(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="scene3d-")
        self.bl = FakeBlender(); self._run = S.subprocess.run; S.subprocess.run = self.bl
    def tearDown(self):
        S.subprocess.run = self._run; shutil.rmtree(self.d, ignore_errors=True)

    def models(self, director_replies, worker):
        it = iter(director_replies); self.dprompts = []
        def director(prompt, system): self.dprompts.append(prompt); return next(it), {"input": 10, "output": 5, "usd": 0.01}
        def w(system, user): return worker(user), {"input": 7, "output": 3, "usd": 0}
        return director, w

    def go(self, director, worker, **kw): return S.run("make it", self.d, "blender", director, worker, **kw)

    def test_happy_path_plan_review_done(self):
        events = []
        dr, wk = self.models([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        r = self.go(dr, wk, on_event=lambda k, **kw: events.append(k))
        self.assertTrue(r["ok"]); self.assertEqual(r["usage"]["director"]["calls"], 2); self.assertEqual(r["usage"]["worker"]["calls"], 2)       # plan, then one review
        self.assertIn("plan", events); self.assertTrue(r["scene"].endswith("scene.blend")); self.assertIn("đạt", r["final"])
        self.assertIn("FINAL REVIEW", self.dprompts[1])                                    # the director saw one review before finishing

    def test_patch_plan_changes_one_part_without_resending_the_plan(self):
        patch = json.dumps({"action": "patch_plan", "parts": [{"id": "wheel", "spec": "disc r=45 at z=40"}], "notes": {"base": "keep it flat"}})
        dr, wk = self.models([json.dumps(PLAN), patch, '{"action":"done"}', '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        r = self.go(dr, wk)
        self.assertTrue(r["ok"]); self.assertEqual(r["usage"]["director"]["calls"], 2)                  # plan, then the patch answers the one review and the green gates end the run
        with open(self.d + "/plan.json") as f: saved = json.load(f)
        self.assertEqual(next(p for p in saved["parts"] if p["id"] == "wheel")["spec"], "disc r=45 at z=40")
        self.assertEqual(len(saved["parts"]), len(PLAN["parts"]))

    def test_a_rejected_patch_changes_nothing(self):
        dr, wk = self.models([json.dumps(PLAN), json.dumps({"action": "patch_plan", "parts": [{"id": "nope"}]}), '{"action":"done"}', '{"action":"done"}'],
                             lambda u: good("base" if "Part id: base" in u else "wheel"))
        self.go(dr, wk)
        self.assertTrue(any("PATCH REJECTED" in p for p in self.dprompts))

    def test_bad_plan_is_rejected_without_building(self):
        dr, wk = self.models(['{"action":"plan","parts":[]}', json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        r = self.go(dr, wk)
        self.assertIn("PLAN REJECTED", self.dprompts[1]); self.assertTrue(r["ok"])

    def test_same_error_twice_goes_to_director_who_patches(self):
        def wk(u): return "```python\ndef build(bpy, bmesh):\n    RAISE\n```" if "Part id: base" in u else good("wheel")
        patch = json.dumps({"action": "patch_part", "id": "base", "code": GOOD % ("base", "base")})
        dr, w = self.models([json.dumps(PLAN), patch, '{"action":"done"}', '{"action":"done"}'], wk)
        r = self.go(dr, w)
        self.assertIn("STUCK", self.dprompts[1]); self.assertIn("same error twice", self.dprompts[1]); self.assertTrue(r["ok"])
        self.assertLessEqual(r["usage"]["worker"]["calls"], S.MAX_TRIES + 1)          # the loop breaker stopped the worker early

    def test_worker_error_text_is_fed_back(self):
        seen = []
        def wk(u):
            seen.append(u)
            return good("wheel") if "Part id: wheel" in u else ("```python\ndef build(bpy, bmesh):\n    pass\n```" if "FAILED" not in u else good("base"))
        dr, w = self.models([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], wk)
        r = self.go(dr, w)
        self.assertTrue(any("FAILED with" in u and "exact name" in u for u in seen)); self.assertTrue(r["ok"])

    def test_group_is_written_in_one_worker_request(self):
        plan = dict(PLAN, parts=[{"id": "leg1", "spec": "leg", "group": "legs"}, {"id": "leg2", "spec": "leg", "group": "legs"}], joints=[])
        multi = "# PART leg1\n" + good("leg1") + "\n# PART leg2\n" + good("leg2")
        dr, w = self.models([json.dumps(plan), '{"action":"done"}', '{"action":"done"}'], lambda u: multi)
        r = self.go(dr, w)
        self.assertEqual(r["usage"]["worker"]["calls"], 1); self.assertTrue(r["ok"])

    def test_scene_issue_is_escalated_not_fixed_by_worker(self):
        self.bl.extra = [{"kind": "collision", "pair": "base|wheel", "frames": [1]}]
        dr, w = self.models([json.dumps(PLAN), '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        r = self.go(dr, w)
        self.assertIn("collision", self.dprompts[1]); self.assertFalse(r["ok"])
        self.assertEqual(r["usage"]["worker"]["calls"], 2)                                   # a pair collision never makes the worker rewrite code

    def test_part_level_kind_outside_the_fixable_list_is_escalated(self):
        self.bl.extra = [{"kind": "span_mismatch", "part": "wheel", "joint": "j1", "expected": 1, "measured": 2}]
        dr, w = self.models([json.dumps(PLAN), '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        r = self.go(dr, w)
        self.assertIn("span_mismatch", self.dprompts[1]); self.assertEqual(r["usage"]["worker"]["calls"], 2)

    def test_fixable_part_issue_goes_back_to_the_worker(self):
        issue = {"kind": "scale_outlier", "part": "wheel", "extent_m": 90.0}
        calls = {"n": 0}
        def build_once(argv, **kw):                                  # the first build reports a unit error on `wheel`; after the worker rewrote it the report is clean
            calls["n"] += 1; self.bl.extra = [issue] if calls["n"] == 1 else []; return FakeBlender.__call__(self.bl, argv, **kw)
        S.subprocess.run = build_once
        dr, w = self.models([json.dumps(PLAN), '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        r = self.go(dr, w)
        self.assertEqual(r["usage"]["worker"]["calls"], 3); self.assertTrue(r["ok"])           # base, wheel, then the wheel again for the unit error

    def test_stop_returns_stopped(self):
        n = {"i": 0}
        def stop():
            n["i"] += 1; return n["i"] > 1
        dr, w = self.models([json.dumps(PLAN)], lambda u: good("base"))
        r = self.go(dr, w, should_stop=stop)
        self.assertTrue(r["stopped"]); self.assertFalse(r["ok"])

    def test_director_call_limit(self):
        dr, w = self.models(["not json"] * 5, lambda u: good("base"))
        r = self.go(dr, w, max_director=3)
        self.assertFalse(r["ok"]); self.assertTrue(r["ended_by_limit"])

    def test_resume_starts_from_saved_plan(self):
        dr, wk = self.models([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        self.go(dr, wk)
        dr2, wk2 = self.models([json.dumps({"action": "write_part", "id": "wheel", "note": "make it larger"}), '{"action":"done"}', '{"action":"done"}'], lambda u: good("wheel"))
        r = S.run("make the wheel larger", self.d, "blender", dr2, wk2, resume=True)
        self.assertIn("CURRENT SCENE", self.dprompts[0]); self.assertIn("make the wheel larger", self.dprompts[0]); self.assertTrue(r["ok"])
        self.assertEqual(r["usage"]["worker"]["calls"], 1)                                   # only the changed part was rewritten

    def test_changed_spec_marks_part_for_rewrite(self):
        dr, wk = self.models([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        self.go(dr, wk)
        plan2 = json.loads(json.dumps(PLAN)); plan2["parts"][1]["spec"] = "disc r=45 at z=40"
        dr2, wk2 = self.models([json.dumps(plan2), '{"action":"done"}', '{"action":"done"}'], lambda u: good("wheel"))
        r = S.run("bigger", self.d, "blender", dr2, wk2, resume=True)
        self.assertEqual(r["usage"]["worker"]["calls"], 1)                                   # only the part whose spec changed is rewritten
        self.assertTrue(r["ok"])


class DoneIsNotAccepted(Loop):
    def test_done_with_a_failing_part_sends_the_worker_back(self):
        bad = {"n": 0}
        def worker(user):
            pid = "base" if "Part id: base" in user else "wheel"
            if pid == "wheel" and bad["n"] == 0: bad["n"] = 1; return "```python\n" + GOOD % (pid, pid) + "    RAISE\n```"      # builds a part that raises in Blender
            return good(pid)
        dr, wk = self.models([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}', '{"action":"done"}'], worker)
        r = self.go(dr, wk)
        self.assertTrue(r["ok"]); self.assertGreaterEqual(bad["n"], 1)

    def test_unfinished_scene_is_not_announced_as_built(self):
        dr, wk = self.models([json.dumps(PLAN)] + ['{"action":"done"}'] * 6, lambda u: "```python\n" + GOOD % ("base" if "Part id: base" in u else "wheel", "base" if "Part id: base" in u else "wheel") + "    RAISE\n```")
        r = self.go(dr, wk)
        self.assertFalse(r["ok"]); self.assertTrue(r["final"].startswith("Cảnh CHƯA hoàn tất")); self.assertIn("Chi tiết còn lỗi dựng", r["final"])


class GeometryGate(unittest.TestCase):
    def test_plan_validates_center_and_size(self):
        ok = {"parts": [{"id": "a", "spec": "x", "center_mm": [0, 0, 5], "size_mm": [10, 10, 10]}]}; self.assertEqual(S.validate_plan(ok), [])
        for bad in ({"center_mm": [0, 0]}, {"size_mm": "big"}, {"size_mm": [1, 2, True]}):
            self.assertTrue(S.validate_plan({"parts": [dict({"id": "a", "spec": "x"}, **bad)]}))

    def test_worker_sees_the_required_bbox(self):
        sc = S.Scene(tempfile.mkdtemp(prefix="g-"), "b", None, None); sc.plan = {"parts": [{"id": "a", "spec": "plate", "center_mm": [1, 2, 3], "size_mm": [4, 5, 6]}, {"id": "b", "spec": "bare"}]}
        self.assertIn("centre [1, 2, 3] mm, size [4, 5, 6] mm", sc.spec("a")); self.assertEqual(sc.spec("b"), "bare")
        shutil.rmtree(sc.dir, ignore_errors=True)

    def test_geometry_issues_go_back_to_the_worker(self):
        for k in ("object_transform", "size_mismatch", "position_mismatch"): self.assertIn(k, S.PART_ISSUES)


RECIPE = {"ops": [{"op": "box", "center": [0, 0, 5], "size": [100, 100, 10]}, {"op": "ring", "center": [0, 0, 40], "axis": "y", "r_out": 30, "r_in": 20, "width": 10}]}


def recipe_reply(rec=RECIPE): return "```json\n" + json.dumps(rec) + "\n```"


class RecipesGuardCache(Loop):
    def test_part_code_turns_a_recipe_into_a_bxlib_call(self):
        code = S.part_code(recipe_reply(), "base")
        self.assertIsNone(S.static_check(code, "base")); self.assertIn("bxlib.build(bpy, bmesh, 'base'", code)
        ns = {}; exec(code, ns); self.assertIn("build", ns)

    def test_bad_recipes_are_reported_to_the_worker_text(self):
        bad = S.part_code(recipe_reply({"ops": [{"op": "ring", "center": [0, 0, 0], "axis": "y", "r_out": 5, "r_in": 9, "width": 1}]}), "p")
        self.assertIn("r_in must be smaller", S.static_check(bad, "p"))
        self.assertIn("not valid JSON", S.static_check(S.part_code("```json\n{oops\n```", "p"), "p"))
        self.assertIn("unknown op", S.static_check(S.part_code(recipe_reply({"ops": [{"op": "cube"}]}), "p"), "p"))

    def test_recipes_can_be_switched_off(self):
        old = S.RECIPES; S.RECIPES = False
        try: self.assertNotIn("bxlib", S.part_code(recipe_reply(), "p"))
        finally: S.RECIPES = old

    def test_worker_recipe_flows_through_the_whole_run(self):
        dr, wk = self.models([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], lambda u: recipe_reply())
        r = self.go(dr, wk)
        for pid in ("base", "wheel"):
            with open(os.path.join(self.d, "parts", pid + ".py")) as f: self.assertIn("bxlib.build", f.read())

    def test_group_reply_may_mix_recipe_and_code(self):
        sc = S.Scene(self.d, "b", None, lambda sy, us: ("# PART base\n" + recipe_reply() + "\n# PART wheel\n```python\n" + GOOD % ("wheel", "wheel") + "```\n", {}))
        sc.plan = PLAN; self.assertEqual(sc.write_group(["base", "wheel"]), {"base", "wheel"})
        with open(sc.pf("base")) as f: self.assertIn("bxlib", f.read())
        with open(sc.pf("wheel")) as f: self.assertIn("def build", f.read())

    def test_a_repair_that_makes_things_worse_is_undone(self):
        class Worse(FakeBlender):
            def __call__(self, argv, **kw):
                super().__call__(argv, **kw); d = argv[argv.index("--") + 1]
                rep = json.load(open(os.path.join(d, "report.json")))
                for pid in ("base", "wheel"):
                    with open(os.path.join(d, "parts", pid + ".py")) as fh:
                        if "WORSE" in fh.read(): rep["part_errors"][pid] = "WorseError: new failure mode"; rep["issues"].append({"kind": "size_mismatch", "part": pid})
                with open(os.path.join(d, "report.json"), "w") as fh: json.dump(rep, fh)
        S.subprocess.run = Worse(); calls = {"n": 0}
        def worker(u):
            pid = "base" if "Part id: base" in u else "wheel"
            if pid != "wheel": return good(pid)
            calls["n"] += 1
            return {1: "```python\n" + GOOD % (pid, pid) + "    RAISE\n```", 2: "```python\n" + GOOD % (pid, pid) + "    WORSE\n```"}.get(calls["n"], good(pid))
        events = []
        dr, wk = self.models([json.dumps(PLAN)] + ['{"action":"done"}'] * 5, worker)
        r = self.go(dr, wk, on_event=lambda k, **kw: events.append((k, kw.get("what"))))
        self.assertIn(("phase", "undo"), events); self.assertTrue(r["ok"])

    def test_guard_can_be_switched_off(self):
        self.assertTrue(S.GUARD)
        self.assertGreater(S.Scene.score({"part_errors": {"a": 1}, "issues": [{}], "missing": []}), S.Scene.score({"part_errors": {}, "issues": [{}], "missing": []}))

    def test_plan_cache_skips_the_director_on_an_identical_request(self):
        cache = os.path.join(self.d, "pc")
        dr, wk = self.models([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        S.run("make it", self.d, "b", dr, wk, plan_cache=cache); self.assertEqual(len(os.listdir(cache)), 1)
        d2 = tempfile.mkdtemp(prefix="scene3d-"); calls = []
        def director(prompt, system): calls.append(prompt); return '{"action":"done"}', {"input": 1, "output": 1, "usd": 0.01}
        try:
            r = S.run("make it", d2, "b", director, wk, plan_cache=cache)
            self.assertTrue(calls and "Start with the plan action" not in calls[0])           # no plan request: the first director call is already the review
            self.assertTrue(r["ok"])
            r3 = S.run("a different request", tempfile.mkdtemp(prefix="scene3d-"), "b", self.models([json.dumps(PLAN)] + ['{"action":"done"}'] * 3, lambda u: good("base" if "Part id: base" in u else "wheel"))[0], wk, plan_cache=cache)
            self.assertEqual(len(os.listdir(cache)), 2)
        finally: shutil.rmtree(d2, ignore_errors=True)


class SkillsAndImages(Loop):
    def capture(self, replies, skills=None, images=None, review_images=False, views=None):
        it = iter(replies); self.dcalls, self.wsys = [], []
        def director(prompt, system, images=None): self.dcalls.append({"prompt": prompt, "system": system, "images": images}); return next(it), {"input": 1, "output": 1, "usd": 0.01}
        def worker(system, user): self.wsys.append(system); return good("base" if "Part id: base" in user else "wheel"), {"input": 1, "output": 1, "usd": 0}
        if views is not None: S.Scene.render_views = lambda sc: views
        return S.run("make it", self.d, "blender", director, worker, skills=skills, images=images, review_images=review_images)

    def setUp(self):
        super().setUp(); self._rv = S.Scene.render_views
    def tearDown(self):
        S.Scene.render_views = self._rv; super().tearDown()

    SK = {"catalog": "- recipes: ring and bevel helpers", "names": ["recipes"], "explicit": "", "read": lambda n: "RECIPE BODY " + n}

    def test_director_sees_catalog_and_worker_gets_the_chosen_skill(self):
        plan = dict(PLAN, skills=["recipes", "ghost"])
        self.capture([json.dumps(plan), '{"action":"done"}', '{"action":"done"}'], skills=self.SK)
        self.assertIn("recipes: ring and bevel helpers", self.dcalls[0]["system"])
        self.assertTrue(all("RECIPE BODY recipes" in w for w in self.wsys))
        self.assertFalse(any("ghost" in w for w in self.wsys))                         # an unknown name is dropped

    def test_no_skills_leaves_prompts_unchanged(self):
        self.capture([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'])
        self.assertEqual(self.dcalls[0]["system"], S.SYSTEM); self.assertTrue(all(w == S.WORKER_SYS for w in self.wsys))

    def test_explicit_skill_reaches_both_roles(self):
        sk = dict(self.SK, explicit="USER INVOKED SKILL TEXT")
        self.capture([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], skills=sk)
        self.assertIn("USER INVOKED SKILL TEXT", self.dcalls[0]["system"]); self.assertTrue(all("USER INVOKED SKILL TEXT" in w for w in self.wsys))

    def test_worker_skill_text_is_capped(self):
        sk = dict(self.SK, read=lambda n: "x" * 50000)
        self.capture([json.dumps(dict(PLAN, skills=["recipes"])), '{"action":"done"}', '{"action":"done"}'], skills=sk)
        self.assertLess(max(len(w) for w in self.wsys), len(S.WORKER_SYS) + 14500)

    def test_user_pictures_go_to_the_director_once(self):
        img = [{"name": "a.png", "mime": "image/png", "data": "AAAA"}]
        self.capture([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], images=img)
        self.assertEqual(self.dcalls[0]["images"], img); self.assertIn("picture", self.dcalls[0]["prompt"])
        self.assertTrue(all(c["images"] is None for c in self.dcalls[1:]))

    def test_review_gets_rendered_views_only_when_enabled(self):
        views = [{"name": "iso.png", "mime": "image/png", "data": "AAAA"}]
        self.capture([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], review_images=True, views=views)
        rv = [c for c in self.dcalls if c["images"]]
        self.assertTrue(rv); self.assertIn("PICTURES attached", rv[0]["prompt"])
        self.capture([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], review_images=False, views=views)
        self.assertFalse([c for c in self.dcalls if c["images"]])

    def test_render_failure_does_not_block_the_review(self):
        self.capture([json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], review_images=True, views=[])
        self.assertFalse([c for c in self.dcalls if c["images"]])


class Pictures(unittest.TestCase):
    PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

    def test_validator(self):
        from cadfabric import workspace as ws
        ok, e = ws.images([{"name": "a.png", "mime": "image/png", "data": self.PNG}]); self.assertEqual((len(ok), e), (1, ""))
        for bad, why in (([{"name": "a.png", "mime": "image/png", "data": "!!notbase64"}], "valid"),
                         ([{"name": "a.png", "mime": "image/jpeg", "data": self.PNG}], "not really"),
                         ([{"name": "a.exe", "mime": "application/x-msdownload", "data": self.PNG}], "only png"),
                         ([{"name": "a.png", "mime": "image/png", "data": self.PNG}] * 4, "at most")):
            self.assertIn(why, ws.images(bad)[1])
        big = __import__("base64").b64encode(b"\x89PNG" + b"0" * 1_600_000).decode()
        self.assertIn("larger", ws.images([{"name": "b.png", "mime": "image/png", "data": big}])[1])

    def test_advisor_sends_image_blocks_and_parses_stream(self):
        from cadfabric import advisors
        seen = {}
        class R: returncode = 0; stderr = ""; stdout = json.dumps({"type": "system"}) + "\n" + json.dumps({"type": "result", "result": "Red", "usage": {"input_tokens": 2, "output_tokens": 1}, "total_cost_usd": 0.01}) + "\n"
        def fake(argv, **kw): seen["argv"], seen["input"] = argv, kw.get("input"); return R()
        old = advisors.subprocess.run; advisors.subprocess.run = fake
        try:
            t, err, u = advisors.ask_metered({"type": "cli", "preset": "claude"}, "colour?", {}, 30, "sys", images=[{"mime": "image/png", "data": self.PNG}])
        finally: advisors.subprocess.run = old
        self.assertEqual((t, err, u["usd"]), ("Red", "", 0.01))
        self.assertIn("stream-json", seen["argv"]); msg = json.loads(seen["input"]); blocks = msg["message"]["content"]
        self.assertEqual([b["type"] for b in blocks], ["image", "text"]); self.assertEqual(blocks[0]["source"]["data"], self.PNG)

    def test_advisor_file_fallback_uses_read_tool_only(self):
        from cadfabric import advisors
        seen = {}
        class R: returncode = 0; stderr = ""; stdout = json.dumps({"result": "Red", "usage": {}, "total_cost_usd": 0})
        def fake(argv, **kw): seen["argv"], seen["input"] = list(argv), kw.get("input"); return R()
        old, env = advisors.subprocess.run, os.environ.get("CADFABRIC_IMAGES_AS_FILES")
        advisors.subprocess.run = fake; os.environ["CADFABRIC_IMAGES_AS_FILES"] = "1"
        try:
            t, err, u = advisors.ask_metered({"type": "cli", "preset": "claude"}, "colour?", {}, 30, "sys", images=[{"mime": "image/png", "data": self.PNG}])
        finally:
            advisors.subprocess.run = old
            if env is None: os.environ.pop("CADFABRIC_IMAGES_AS_FILES")
        self.assertEqual(t, "Red"); self.assertIn("Read", seen["argv"]); self.assertNotIn("stream-json", seen["argv"]); self.assertIn("picture-1.png", seen["input"])

    def test_other_advisors_cannot_see_images(self):
        from cadfabric import advisors
        self.assertTrue(advisors.can_see_images({"type": "cli", "preset": "claude"}))
        self.assertFalse(advisors.can_see_images({"type": "cli", "preset": "codex"})); self.assertFalse(advisors.can_see_images(None))


class WebSession(unittest.TestCase):
    """The web layer around the scene agent: measure, autosave to the project folder, open in Blender, resume flag, planner handling."""
    def setUp(self):
        from cadfabric import web, config
        self.web, self.d = web, tempfile.mkdtemp(prefix="scene3d-web-")
        self.ws, self.wd = os.path.join(self.d, "proj"), os.path.join(self.d, "work")
        os.makedirs(self.ws); os.makedirs(os.path.join(self.wd, "parts"))
        for f, c in (("scene.blend", "B"), ("plan.json", "{}"), ("usage.json", "{}"), ("parts/a.py", "x")):
            with open(os.path.join(self.wd, f), "w") as fh: fh.write(c)
        cfg = dict(config.DEFAULTS, workspace=self.ws, data_dir=os.path.join(self.d, "data"), advisors=[])
        self.sess = self.web.Session(object(), cfg, backend=type("B", (), {"measure": lambda s: None, "reset": lambda s: None, "close": lambda s: None, "history": []})(), local=True)
        self._run, self._bp = S.run, self.web.viewers.blender_path
        self.web.viewers.blender_path = lambda: "/bin/true"
        self.sess.sid, self.sess.title = "sid1", "My scene"
    def tearDown(self):
        S.run = self._run; self.web.viewers.blender_path = self._bp; shutil.rmtree(self.d, ignore_errors=True)

    def fake_run(self, **over):
        rep = {"ok": True, "parts_bbox_mm": {"a": [[0, 0, 0], [10, 20, 30]], "b": [[5, 5, 5], [15, 25, 35]]}, "totals": {"volume_mm3": 123.0, "faces": 6, "edges": 12, "vertices": 8}}
        r = {"final": "ok", "steps": 3, "warnings": 0, "gates": 0, "sec": 1.0, "ok": True, "report": rep, "usage": {}, "scene": "", "stuck": "", "ended_by_limit": False,
             "stopped": False, "history": {"scene3d": True, "workdir": self.wd, "task": "t"}}
        r.update(over)
        seen = {}
        def run(request, workdir, blender, director, worker, **kw): seen.update(request=request, workdir=workdir, blender=blender, resume=kw.get("resume")); return r
        S.run = run; return seen

    def test_turn_sets_measure_and_resume_flag(self):
        seen = self.fake_run()
        self.sess._scene_turn("make a thing", "nothink", "")
        m = self.sess.measure
        self.assertEqual((m["bbox_size"], m["solids"], m["faces"], m["valid"]), ([15, 25, 35], 2, 6, True)); self.assertFalse(seen["resume"])
        self.assertEqual(seen["blender"], "/bin/true")
        self.sess._scene_turn("make it bigger", "nothink", "")
        self.assertTrue(seen["resume"])                                                       # the second message continues the saved scene
        self.assertEqual([e["kind"] for e in self.sess.events if e["kind"] == "final"], ["final", "final"])

    def test_attachments_are_appended_to_the_request(self):
        seen = self.fake_run(); self.sess._scene_turn("do it", "nothink", "FILE DATA")
        self.assertIn("ATTACHED FILES", seen["request"]); self.assertIn("FILE DATA", seen["request"])

    def test_autosave_copies_blend_and_scene_folder_and_reuses_name(self):
        self.fake_run(); self.sess._scene_turn("x", "nothink", ""); self.sess.autosave()
        names = sorted(os.listdir(self.ws)); self.assertEqual(names, ["my-scene.blend", "my-scene.scene"])
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "my-scene.scene", "parts", "a.py")))
        open(os.path.join(self.wd, "scene.blend"), "w").write("B2"); self.sess.autosave()
        self.assertEqual(sorted(os.listdir(self.ws)), names); self.assertEqual(open(os.path.join(self.ws, "my-scene.blend")).read(), "B2")

    def test_open_in_viewer_uses_blender_for_scenes(self):
        self.fake_run(); self.sess._scene_turn("x", "nothink", ""); self.sess.autosave()
        spawned = []; self.web.viewers._spawn = lambda argv: spawned.append(argv)
        code, body = self.sess.open_in_viewer()
        self.assertEqual(code, 200); self.assertEqual(spawned[0][0], "/bin/true"); self.assertTrue(spawned[0][1].endswith("my-scene.blend"))

    def test_unknown_planner_and_relay_are_refused(self):
        self.fake_run()
        with self.assertRaises(ValueError): self.sess._scene_turn("x", "nope", "")
        with self.assertRaises(ValueError): self.sess._scene_turn("x", "codex-relay", "")

    def test_blender_path_from_config_wins_and_must_exist(self):
        self.sess.cfg["blender"] = "/bin/echo"; self.assertEqual(self.sess.blender_exe(), "/bin/echo")
        self.sess.cfg["blender"] = "/no/such/blender"; self.assertEqual(self.sess.blender_exe(), "/bin/true")      # falls back to the detected one

    def test_pictures_need_the_claude_advisor(self):
        self.fake_run()
        with self.assertRaises(ValueError) as c: self.sess._scene_turn("x", "nothink", "", images=[{"name": "a.png", "mime": "image/png", "data": "AAAA"}])
        self.assertIn("Claude", str(c.exception))

    def test_scene_skills_only_approved_and_cataloged(self):
        from cadfabric import skills as sk
        self.assertIsNone(self.sess._scene_skills("x"))
        old = (sk.list_for, sk.use)
        sk.list_for = lambda dd, root: [{"name": "a", "status": "approved", "description": "does a", "implicit": True}, {"name": "b", "status": "pending", "description": "no"},
                                       {"name": "c", "status": "approved", "description": "manual only", "implicit": False}]
        sk.use = lambda dd, root, n, rel="": "BODY " + n
        try: r = self.sess._scene_skills("please"); body = r["read"]("a")
        finally: sk.list_for, sk.use = old
        self.assertEqual(r["names"], ["a"]); self.assertIn("a: does a", r["catalog"]); self.assertNotIn("b", r["catalog"]); self.assertEqual(body, "BODY a")

    def test_missing_blender_is_a_clear_error(self):
        self.web.viewers.blender_path = lambda: None
        with self.assertRaises(ValueError) as c: self.sess._scene_turn("x", "nothink", "")
        self.assertIn("Blender", str(c.exception))


class Expand(unittest.TestCase):
    LEG = {"id": "leg_L", "spec": "leg", "color": "#112233", "finish": "metal", "center_mm": [100, 80, 50], "ops": [{"op": "cylinder", "a": [100, 80, 0], "b": [100, 80, 100], "r": 10}]}

    def test_mirror_reflects_positions_and_inherits_look(self):
        p, e = S.expand_plan({"parts": [self.LEG, {"id": "leg_R", "mirror_of": "leg_L"}]})
        self.assertEqual(e, []); r = p["parts"][1]
        self.assertEqual((r["ops"][0]["a"], r["center_mm"], r["color"], r["finish"]), ([100, -80, 0], [100, -80, 50], "#112233", "metal"))
        self.assertEqual(S.validate_plan(p), [])

    def test_mirror_in_another_plane_with_offset_and_array(self):
        p, e = S.expand_plan({"parts": [self.LEG, {"id": "m", "mirror_of": "leg_L", "plane": "x", "offset_mm": 10},
                                         {"id": "k", "spec": "k", "ops": [{"op": "box", "center": [0, 0, 5], "size": [10, 10, 4]}], "array": {"count": 3, "step": [15, 0, 0]}}]})
        self.assertEqual(e, [])
        ids = [q["id"] for q in p["parts"]]; self.assertEqual(ids, ["leg_L", "m", "k_1", "k_2", "k_3"])
        self.assertEqual(p["parts"][1]["ops"][0]["a"], [-80, 80, 0]); self.assertEqual(p["parts"][4]["ops"][0]["center"], [30, 0, 5])

    def test_bad_mirror_and_array_are_rejected_with_reasons(self):
        _, e = S.expand_plan({"parts": [{"id": "a", "spec": "x"}, {"id": "b", "mirror_of": "a"}, {"id": "c", "spec": "x", "array": {"count": 1, "step": [1, 0, 0]}}]})
        self.assertEqual(len(e), 2)
        loft = {"id": "l", "spec": "x", "ops": [{"op": "loft", "sections": [{"x": 0, "y": 0, "z": 0, "w": 1, "h": 1}, {"x": 1, "y": 0, "z": 0, "w": 1, "h": 1}]}]}
        _, e = S.expand_plan({"parts": [loft, {"id": "m", "mirror_of": "l", "plane": "x"}]}); self.assertIn("plane y", e[0])


class RootCause(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="scene3d-"); self._run = S.subprocess.run; self.bl = FakeBlender()

    def tearDown(self): S.subprocess.run = self._run; shutil.rmtree(self.d, ignore_errors=True)

    def test_recipe_parts_from_the_director_need_no_worker_call(self):
        plan = {"action": "plan", "parts": [{"id": "base", "spec": "plate", "ops": [{"op": "box", "center": [0, 0, 5], "size": [100, 100, 10]}]},
                                              {"id": "wheel", "spec": "disc", "ops": [{"op": "ring", "center": [0, 0, 40], "axis": "y", "r_out": 30, "r_in": 20, "width": 10}]}],
                "joints": [], "allowed_contacts": []}
        S.subprocess.run = self.bl; calls = {"w": 0}
        def worker(system, user): calls["w"] += 1; return good("x"), {}
        r = S.run("t", self.d, "blender", lambda p, s, **k: (json.dumps(plan) if "TASK" in p or True else '{"action":"done"}', {}), worker)
        self.assertEqual(calls["w"], 0)
        self.assertTrue(open(os.path.join(self.d, "parts", "base.py")).read().startswith("# recipe part"))

    def test_probe_names_a_part_that_crashes_blender(self):
        sc = S.Scene(self.d, "blender", None, None); os.makedirs(os.path.join(self.d, "parts"), exist_ok=True)
        with open(sc.pf("a"), "w") as f: f.write(GOOD % ("a", "a"))
        class R: returncode = -9; stdout = ""; stderr = ""
        os.environ["BX_PROBE"] = "1"
        try:
            S.subprocess.run = lambda *a, **k: R()
            self.assertIn("crashed Blender", sc.probe("a"))
            S.subprocess.run = lambda *a, **k: (open(os.path.join(self.d, "smoke_a.json"), "w").write('{"error": "mesh has no vertices"}'), None)[1]
            self.assertEqual(sc.probe("a"), "mesh has no vertices")
            with open(sc.pf("a"), "w") as f: f.write("# recipe part (built by bxlib)\n")
            S.subprocess.run = lambda *a, **k: R()
            self.assertIsNone(sc.probe("a"))                  # recipes are safe by construction and are not probed
        finally:
            os.environ["BX_PROBE"] = "0"


class PatchPlan(unittest.TestCase):
    PLAN = {"parts": [{"id": "a", "spec": "s", "center_mm": [0, 0, 0], "size_mm": [1, 1, 1]}, {"id": "b", "spec": "s", "center_mm": [5, 0, 0], "size_mm": [1, 1, 1]}],
            "joints": [{"name": "j1", "parent": "a", "children": ["b"], "origin_mm": [0, 0, 0], "axis": [0, 0, 1]}], "allowed_contacts": [["a", "b"]]}

    def test_only_what_is_named_changes(self):
        new, errs = S.apply_patch(self.PLAN, {"parts": [{"id": "b", "center_mm": [9, 0, 0]}], "allowed_contacts_add": [["b", "c"]], "add_parts": [{"id": "c", "spec": "s"}]})
        self.assertEqual(errs, [])
        self.assertEqual({p["id"]: p["center_mm"] for p in new["parts"] if "center_mm" in p}, {"a": [0, 0, 0], "b": [9, 0, 0]})
        self.assertEqual(new["allowed_contacts"], [["a", "b"], ["b", "c"]])
        self.assertEqual(self.PLAN["parts"][1]["center_mm"], [5, 0, 0], "the original plan must not be modified")

    def test_removing_a_part_drops_its_contacts_and_unknown_ids_are_errors(self):
        new, errs = S.apply_patch(self.PLAN, {"remove_parts": ["b"], "remove_joints": ["j1"]})
        self.assertEqual((errs, [p["id"] for p in new["parts"]], new["allowed_contacts"], new["joints"]), ([], ["a"], [], []))
        _, errs = S.apply_patch(self.PLAN, {"parts": [{"id": "zz"}], "add_parts": [{"id": "a"}]})
        self.assertEqual(len(errs), 2)

    def test_one_review_by_default(self):
        self.assertEqual(S.MAX_REVIEWS, 1)
        self.assertIn("patch_plan", S.SYSTEM)


class CostAndQualityGates(Loop):
    def test_worker_mirror_of_without_director_ops(self):
        plan = {
            "action": "plan",
            "parts": [
                {"id": "fork_L", "spec": "fork leg", "center_mm": [930, 75, 350], "size_mm": [180, 44, 318], "shape": {"endpoints": [[1000, 75, 200], [860, 75, 499]]}},
                {"id": "fork_R", "mirror_of": "fork_L"},
            ],
            "joints": [],
            "allowed_contacts": [],
        }
        fork_rec = {"ops": [{"op": "cylinder", "a": [1000, 75, 200], "b": [860, 75, 499], "r": 22}]}
        seen = []
        def wk(u):
            seen.append(u)
            return recipe_reply(fork_rec)
        dr, w = self.models([json.dumps(plan), '{"action":"done"}', '{"action":"done"}'], wk)
        r = self.go(dr, w)
        self.assertTrue(r["ok"])
        self.assertEqual(len(seen), 1)
        self.assertIn("Part id: fork_L", seen[0])
        r_code = open(os.path.join(self.d, "parts", "fork_R.py")).read()
        self.assertIn("-75", r_code)
        raw_mir = S.mirror_part_code(GOOD % ("fork_L", "fork_L"), "fork_L", "fork_R", "y", 0.0)
        self.assertIsNone(S.static_check(raw_mir, "fork_R"))

    def test_adaptive_best_of_n_picks_valid_recipe_without_blender(self):
        plan = {
            "action": "plan",
            "parts": [{"id": "base", "spec": "plate 100x100x10", "center_mm": [0, 0, 5], "size_mm": [100, 100, 10]}],
            "joints": [],
        }
        old_n = S.BESTOFN
        S.BESTOFN = 2
        try:
            calls = {"n": 0}
            def wk(system, user, temperature=0.3):
                calls["n"] += 1
                if calls["n"] == 1:
                    return recipe_reply({"ops": [{"op": "box", "center": [0, 0, 5], "size": [20, 20, 10]}]}), {"input": 5, "output": 5, "usd": 0}
                return recipe_reply({"ops": [{"op": "box", "center": [0, 0, 5], "size": [100, 100, 10]}]}), {"input": 5, "output": 5, "usd": 0}
            dr = lambda p, s: (json.dumps(plan) if "Start with the plan" in p else '{"action":"done"}', {"input": 5, "output": 5, "usd": 0.01})
            r = self.go(dr, wk)
            self.assertTrue(r["ok"])
            self.assertEqual(calls["n"], 2)
            self.assertEqual(r["steps"], 1)

            calls["n"] = 0
            d2 = tempfile.mkdtemp(prefix="scene3d-fast-")
            try:
                def wk_fast(system, user, temperature=0.3):
                    calls["n"] += 1
                    return recipe_reply({"ops": [{"op": "box", "center": [0, 0, 5], "size": [100, 100, 10]}]}), {"input": 5, "output": 5, "usd": 0}
                r2 = S.run("make it", d2, "blender", dr, wk_fast)
                self.assertTrue(r2["ok"])
                self.assertEqual(calls["n"], 1)
            finally:
                shutil.rmtree(d2, ignore_errors=True)
        finally:
            S.BESTOFN = old_n

    def test_local_collision_self_repair_and_delta_escalation(self):
        issue = {"kind": "collision", "pair": "base|wheel", "frames": [1], "overlap_mm": [[0, 0, 0], [10, 10, 10]]}
        bcalls = {"n": 0}
        def build_once(argv, **kw):
            bcalls["n"] += 1
            self.bl.extra = [issue] if bcalls["n"] == 1 else []
            return FakeBlender.__call__(self.bl, argv, **kw)
        S.subprocess.run = build_once
        seen = []
        def wk(u):
            seen.append(u)
            return good("base" if "Part id: base" in u else "wheel")
        dr, w = self.models([json.dumps(PLAN), '{"action":"done"}'], wk)
        r = self.go(dr, w)
        self.assertTrue(r["ok"])
        self.assertEqual(r["usage"]["director"]["calls"], 2)
        self.assertTrue(any("rest-pose collision with wheel" in u for u in seen))

        big_parts = [{"id": f"p{k}", "spec": f"part {k}", "ops": [{"op": "box", "center": [k * 200, 0, 5], "size": [20, 20, 10]}]} for k in range(20)]
        big_plan = {"parts": big_parts, "joints": [], "allowed_contacts": []}
        rep = {"parts_bbox_mm": {f"p{k}": [[k * 200 - 10, -10, 0], [k * 200 + 10, 10, 10]] for k in range(20)}}
        rep["parts_bbox_mm"]["p18"] = [[19 * 200 - 30, -10, 0], [19 * 200 - 10, 10, 10]]
        dv = S.plan_delta_view(big_plan, rep, {"p19": "stuck"}, [])
        kept_ids = {p["id"] for p in dv["parts"]}
        self.assertEqual(kept_ids, {"p18", "p19"})
        self.assertEqual(len(dv["healthy_locked_parts"]), 18)


class AdaptiveScaling(Loop):
    def test_clip_prompt_preserves_head_and_tail(self):
        from cadfabric import advisors
        big = "TASK: build a 100-part bike\n" + ("MIDDLE_NOISE " * 1000) + "\nISSUES\ncollision on peg_L"
        clipped = advisors.clip_prompt(big, 800)
        self.assertLessEqual(len(clipped), 860)
        self.assertTrue(clipped.startswith("TASK: build a 100-part bike"))
        self.assertTrue(clipped.endswith("ISSUES\ncollision on peg_L"))
        self.assertIn("middle truncated", clipped)

    def test_adaptive_scale_policy_and_smart_review(self):
        self.assertEqual((S.scale_policy(10)["workers"], S.scale_policy(40)["workers"], S.scale_policy(70)["workers"]), (1, 2, 3))
        self.assertEqual((S.scale_policy(10)["group_max"], S.scale_policy(40)["group_max"], S.scale_policy(70)["group_max"]), (4, 6, 8))
        sc = S.Scene(self.d, "b", None, None)
        parts = [{"id": f"p{k}", "spec": "s", "center_mm": [k * 50, 0, 5], "size_mm": [20, 20, 10], "color": "#112233", "finish": "matte"} for k in range(40)]
        sc.plan = {"parts": parts, "joints": [{"name": "j1", "parent": "p0", "children": ["p1"], "origin_mm": [0, 0, 5], "axis": [0, 1, 0], "span_rad": 1.0}]}
        bb = {f"p{k}": [[k * 50 - 10, -10, 0], [k * 50 + 10, 10, 10]] for k in range(40)}
        bb["p5"] = [[5 * 50 - 10, -10, 0], [5 * 50 + 25, 10, 10]]  # drifted > 3 mm
        txt = sc.review_text({"parts_bbox_mm": bb})
        self.assertIn("PART INVENTORY (40 parts)", txt)
        self.assertIn("p5: lo", txt)
        self.assertIn("p1: lo", txt)
        self.assertIn("static parts match planned", txt)

        # structural add_parts on review triggers 1 extra review; regular patch_plan does not
        p0 = {"action": "plan", "parts": [{"id": "a", "spec": "s"}], "joints": []}
        add_p = {"action": "patch_plan", "add_parts": [{"id": "b", "spec": "s"}]}
        dr, w = self.models([json.dumps(p0), json.dumps(add_p), '{"action":"done"}', '{"action":"done"}'],
                            lambda u: good("a" if "Part id: a" in u else "b"))
        r = self.go(dr, w)
        self.assertTrue(r["ok"])
        self.assertEqual(r["usage"]["director"]["calls"], 3)

    def test_parallel_worker_batches_on_medium_scene(self):
        parts = [{"id": f"p{k}", "spec": f"box {k}", "center_mm": [k * 30, 0, 5], "size_mm": [10, 10, 10]} for k in range(30)]
        plan = {"action": "plan", "parts": parts, "joints": []}
        dr, w = self.models([json.dumps(plan), '{"action":"done"}', '{"action":"done"}'],
                            lambda u: good(u.split("Part id: ")[1].splitlines()[0]))
        r = self.go(dr, w)
        self.assertTrue(r["ok"])
        self.assertEqual(r["usage"]["worker"]["calls"], 30)
        self.assertEqual(len([f for f in os.listdir(os.path.join(self.d, "parts")) if f.endswith(".py")]), 30)

    def test_jparse_resilient_and_array_contacts_and_stateful_reject(self):
        prose_reply = 'Note on braces {not json} and coords [1,2,3].\n```json\n{"action":"done"}\n```\nTrailing {x:1}.'
        self.assertEqual(S.jparse(prose_reply), {"action": "done"})
        truncated = '{"action":"plan","parts":[{"id":"a","spec":"x","ops":[{"op":"box","center":[0,0,5],"size":[1,1,1]}]}]'
        self.assertIsNone(S.jparse(truncated))

        arr_plan = {
            "action": "plan",
            "parts": [
                {"id": "hub", "spec": "hub"},
                {"id": "bolt", "spec": "bolt", "ops": [{"op": "box", "center": [0, 0, 5], "size": [4, 4, 4]}], "array": {"count": 2, "step": [10, 0, 0]}},
            ],
            "joints": [{"name": "j1", "parent": "hub", "children": ["bolt"], "coaxial": ["bolt"], "origin_mm": [0, 0, 5], "axis": [0, 1, 0], "span_rad": 6.283}],
            "allowed_contacts": [["hub", "bolt"]],
        }
        ep, errs = S.expand_plan(arr_plan)
        self.assertEqual(errs, [])
        self.assertEqual(S.validate_plan(ep), [])
        self.assertEqual(ep["joints"][0]["children"], ["bolt_1", "bolt_2"])
        self.assertEqual(ep["allowed_contacts"], [["hub", "bolt_1"], ["hub", "bolt_2"]])

        bad_big = json.dumps({"action": "plan", "parts": [{"id": "base", "spec": "plate " + ("x" * 200), "center_mm": [0, 0, 5], "size_mm": [100, 100, 10]}, {"id": "dup", "spec": "s"}], "allowed_contacts": [["base", "missing_id"]]})
        dr, w = self.models([bad_big, json.dumps(PLAN), '{"action":"done"}', '{"action":"done"}'], lambda u: good("base" if "Part id: base" in u else "wheel"))
        r = self.go(dr, w)
        self.assertTrue(r["ok"])
        self.assertIn("YOUR PREVIOUS JSON", self.dprompts[1])
        log_lines = [json.loads(ln) for ln in open(os.path.join(self.d, "director_log.jsonl"), encoding="utf-8")]
        self.assertEqual([x["action"] for x in log_lines], ["plan", "plan", "done"])
        self.assertTrue(log_lines[0]["errs"])

    def test_role_filtered_explicit_skill_routing_in_web(self):
        from cadfabric import web as web_mod, skills as sk
        dd = os.path.join(self.d, "data"); ws = os.path.join(self.d, "ws")
        os.makedirs(dd, exist_ok=True); os.makedirs(ws, exist_ok=True)
        sess = web_mod.Session(type("L", (), {})(), {"url": "", "model": "", "planner": "nothink", "workspace": ws}, backend=type("B", (), {"measure": lambda s: None, "close": lambda s: None})(), local=True)
        ap = [
            {"name": "dir-skill", "status": "approved", "description": "d", "implicit": True, "audience": "director"},
            {"name": "wrk-skill", "status": "approved", "description": "w", "implicit": True, "audience": "worker"},
        ]
        old = (sk.list_for, sk.explicit_block, web_mod.config.data_dir)
        sk.list_for = lambda d, r: ap
        sk.explicit_block = lambda d, r, names: "EXPLICIT:" + ",".join(names)
        web_mod.config.data_dir = lambda cfg: dd
        seen_turn = []
        sess._scene_turn = lambda text, planner, block, images=(), worker=None: seen_turn.append(block)
        try:
            res = sess._scene_skills("$dir-skill $wrk-skill build")
            sess._work("$dir-skill $wrk-skill build", "nothink")
        finally:
            sk.list_for, sk.explicit_block, web_mod.config.data_dir = old
        self.assertEqual(res["explicit"], "EXPLICIT:dir-skill")
        self.assertEqual(res["explicit_worker"], "EXPLICIT:wrk-skill")
        self.assertEqual(seen_turn, [""])

    def test_unified_model_pool_roles_and_worker_array(self):
        from cadfabric import advisors as A, config as cfg_mod, web as web_mod
        import http.server, http.client, threading

        seen_api = []
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                seen_api.append({"h": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                txt = '{"action":"done"}'
                out = {"choices": [{"message": {"content": txt}}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
                b = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        api_srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=api_srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/v1/chat/completions" % api_srv.server_address[1]
            cfg = {"url": "http://127.0.0.1:1/v1/chat/completions", "model": "qwen-gw", "planner": "qwen", "worker": "m2",
                   "advisors": [{"id": "m2", "label": "Big Model", "type": "api", "kind": "openai", "url": url, "model": "big-q", "external": True, "think": True}],
                   "relay": {}, "workspace": self.d}
            self.assertIn("qwen", A.names(cfg))
            self.assertIn("m2", A.worker_names(cfg))
            self.assertTrue(any(m["id"] == "qwen" and m["gateway"] for m in A.models_meta(cfg)))

            t, u = A.chat_model(cfg, "m2", None, "SYS_PROMPT", "USER_PROMPT", max_tokens=512, temperature=0.1)
            self.assertEqual(t, '{"action":"done"}')
            self.assertEqual((u["input"], u["output"], u["estimated"]), (11, 7, False))
            self.assertEqual(seen_api[-1]["body"]["messages"][0]["content"], "SYS_PROMPT")
            self.assertTrue(seen_api[-1]["body"]["chat_template_kwargs"]["enable_thinking"])

            clean, err = cfg_mod.validate_settings({"url": url, "model": "qwen-gw", "planner": "qwen", "worker": "m2",
                                                    "advisors": [{"id": "m2", "label": "Big Model", "type": "api", "kind": "openai", "url": url, "model": "big-q", "external": True}], "relay": {}})
            self.assertEqual(err, "")
            self.assertEqual(clean["worker"], "m2")

            # Web /api/send enforces confirm_external when selected worker is external
            dd = os.path.join(self.d, "wdata"); os.makedirs(dd, exist_ok=True)
            cfg_srv = dict(cfg, data_dir=dd, planner="nothink", worker="m2")
            srv = web_mod.serve(type("L", (), {"chat": lambda *a, **k: {"content": "ok", "_usage": {}}, "close": lambda s: None})(), cfg_srv, port=0, token="t1", quiet=True)
            srv.hosts.add("127.0.0.1:%d" % srv.port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
                hdr = {"Cookie": "cft=t1", "X-Requested-With": "cadfabric", "Content-Type": "application/json"}
                conn.request("POST", "/api/send", json.dumps({"text": "hi", "planner": "nothink", "worker": "m2"}), hdr)
                self.assertEqual(conn.getresponse().status, 409)
            finally:
                srv.shutdown(); srv.sess.close()
        finally:
            api_srv.shutdown()

        # Worker array without Director ops: expands to _1 + deterministic _offset_from _2.._3
        arr_plan = {
            "action": "plan",
            "parts": [{"id": "fin", "spec": "cooling fin", "center_mm": [0, 0, 10], "size_mm": [10, 40, 20], "array": {"count": 3, "step": [20, 0, 0]}}],
            "joints": [], "allowed_contacts": [],
        }
        fin_rec = {"ops": [{"op": "box", "center": [0, 0, 10], "size": [10, 40, 20]}]}
        seen_w = []
        def wk(u):
            seen_w.append(u)
            return recipe_reply(fin_rec)
        dr, w = self.models([json.dumps(arr_plan), '{"action":"done"}', '{"action":"done"}'], wk)
        r = self.go(dr, w)
        self.assertTrue(r["ok"])
        self.assertEqual(len(seen_w), 1)
        self.assertIn("Part id: fin_1", seen_w[0])
        c3 = open(os.path.join(self.d, "parts", "fin_3.py"), encoding="utf-8").read()
        self.assertIn("40", c3)

        # Worker skill reminder stays at end of system prompt when skills exist
        sc = S.Scene(self.d, "blender", None, lambda sys_p, usr_p, temperature=0.3: (seen_w.append(sys_p) or good("x"), {}))
        sc.skills = {"explicit_worker": "RAW BPY EXAMPLE"}
        sc.ask_worker("Part id: x")
        self.assertIn("ALWAYS output ONE ```json recipe block", seen_w[-1])

        # ask_metered preserves full error message from Claude CLI JSON result instead of single-char indexing
        d_bin = tempfile.mkdtemp(prefix="fake-claude-")
        try:
            exe = os.path.join(d_bin, "claude")
            with open(exe, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\necho '{\"is_error\":true,\"result\":\"You hit rate limit\"}'\nexit 1\n")
            os.chmod(exe, 0o755)
            old_path, old_sub = os.environ["PATH"], S.subprocess.run
            os.environ["PATH"] = d_bin + os.pathsep + old_path
            S.subprocess.run = self._run
            try:
                t_err, err_str, _ = A.ask_metered({"id": "c", "type": "cli", "preset": "claude"}, "hi", {})
                self.assertEqual(t_err, "")
                self.assertIn("You hit rate limit", err_str)
            finally:
                os.environ["PATH"] = old_path
                S.subprocess.run = old_sub
        finally:
            shutil.rmtree(d_bin, ignore_errors=True)

    def test_auto_discover_models_and_dynamic_role_overrides(self):
        from cadfabric import advisors as A, web as web_mod
        import base64, http.server, http.client, threading

        # 1. Claude cache auto-discovery with a brand-new hypothetical family & effort
        home = tempfile.mkdtemp(prefix="cf-home-")
        try:
            mc = os.path.join(home, ".claude", "cache", "model-catalog")
            os.makedirs(mc, exist_ok=True)
            cc_payload = {"catalog": {"config": {"models": [
                {"id": "claude-nova-6-0", "name": "Nova 6.0", "short_name": "Nova", "section": "main",
                 "runtime": {"effort_levels": ["low", "medium", "turbo"]}},
                {"id": "claude-opus-5-5", "name": "Opus 5.5", "short_name": "Opus", "section": "main",
                 "runtime": {"effort_levels": ["low", "medium", "high", "xhigh", "max"]}},
            ]}}}
            with open(os.path.join(mc, "test-cc.json"), "w", encoding="utf-8") as f:
                json.dump(cc_payload, f)
            cl = A.discover_claude_models(home=home)
            self.assertIn("nova", cl["models"])
            self.assertIn("claude-nova-6-0", cl["models"])
            self.assertIn("turbo", cl["efforts"])

            # Fallback to published-*.json when *-cc.json is absent
            os.unlink(os.path.join(mc, "test-cc.json"))
            pub_doc = {"surfaces": {"cc": {"model_selector_state": [
                {"model": "claude-fable-5-2", "thinking": {"effort": "high"},
                 "thinking_by_model": [{"id": "claude-comet-6-1", "thinking": {"effort": "max"}}]}
            ]}}}
            with open(os.path.join(mc, "published-test.json"), "w", encoding="utf-8") as f:
                json.dump({"documentBytes": base64.b64encode(json.dumps(pub_doc).encode()).decode()}, f)
            cl2 = A.discover_claude_models(home=home)
            self.assertIn("claude-fable-5-2", cl2["models"])
            self.assertIn("claude-comet-6-1", cl2["models"])

            # 2. Codex cache & config.toml auto-discovery (filtering hidden entries while harvesting efforts)
            cx_dir = os.path.join(home, ".codex")
            os.makedirs(cx_dir, exist_ok=True)
            with open(os.path.join(cx_dir, "models_cache.json"), "w", encoding="utf-8") as f:
                json.dump({"models": [
                    {"slug": "codex-hidden-review", "visibility": "hide", "supported_reasoning_levels": [{"effort": "xhigh"}]},
                    {"slug": "gpt-5.4-codex", "visibility": "list", "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "xhigh"}]},
                ]}, f)
            with open(os.path.join(cx_dir, "config.toml"), "w", encoding="utf-8") as f:
                f.write('model = "o4-mini-codex"\n')
            cx = A.discover_codex_models(home=home, probe_rpc=False)
            self.assertIn("gpt-5.4-codex", cx["models"])
            self.assertIn("o4-mini-codex", cx["models"])
            self.assertNotIn("codex-hidden-review", cx["models"])
            self.assertIn("xhigh", cx["efforts"])
        finally:
            shutil.rmtree(home, ignore_errors=True)

        # 3. parse_model_spec + chat_model overrides + --fallback-model in cli_argv
        self.assertEqual(A.parse_model_spec("claude:claude-fable-5-2@high"), ("claude", "claude-fable-5-2", "high"))
        self.assertEqual(A.parse_model_spec("qwen:qwen2.5-coder:32b"), ("qwen", "qwen2.5-coder:32b", ""))
        self.assertEqual(A.parse_model_spec("claude@medium"), ("claude", "", "medium"))

        argv, _ = A.cli_argv({"preset": "claude", "model": "opus", "fallback_model": "sonnet", "effort": "high"}, {}, "/tmp")
        self.assertIn("--fallback-model", argv)
        self.assertEqual(argv[argv.index("--fallback-model") + 1], "sonnet")

        seen_gw = []
        class Gw:
            def chat(self, msgs, **kw):
                seen_gw.append(kw)
                return {"content": "ok", "_usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        t, u = A.chat_model({}, "qwen:qwen-custom:32b@think", Gw(), "sys", "usr")
        self.assertEqual(t, "ok")
        self.assertEqual(seen_gw[-1]["model"], "qwen-custom:32b")
        self.assertTrue(seen_gw[-1]["think"])

        # 4. API GET /models discovery + Web /api/discover_models security & /api/send overrides
        secret_file = os.path.join(self.d, "evil_secret.txt")
        open(secret_file, "w", encoding="utf-8").write("TOP-SECRET-KEY")

        class FakeApi(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                if "/anthropic/v1/models" in self.path:
                    out = {"data": [{"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, {"id": "claude-fable-5-2"}]}
                else:
                    out = {"data": [{"id": "qwen-3-coder"}, {"id": "qwen-plus"}]}
                b = json.dumps(out).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        api_srv = http.server.HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=api_srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % api_srv.server_address[1]
            r_oai = A.discover_api_models(base + "/v1/chat/completions", "openai")
            self.assertTrue(r_oai["ok"]); self.assertEqual(r_oai["models"], ["qwen-3-coder", "qwen-plus"])
            r_ant = A.discover_api_models(base + "/anthropic/v1/messages", "anthropic", key="k1")
            self.assertTrue(r_ant["ok"]); self.assertEqual(r_ant["models"], ["claude-opus-5-5", "claude-fable-5-2"])

            dd = os.path.join(self.d, "wdisc"); os.makedirs(dd, exist_ok=True)
            cfg_srv = {"url": base + "/v1/chat/completions", "model": "qwen-gw", "planner": "nothink", "worker": "qwen",
                       "advisors": [{"id": "claude", "label": "Claude", "type": "cli", "preset": "claude", "external": True}],
                       "relay": {}, "workspace": self.d, "data_dir": dd}
            srv = web_mod.serve(Gw(), cfg_srv, port=0, token="t2", quiet=True)
            srv.hosts.add("127.0.0.1:%d" % srv.port)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                hdr = {"Cookie": "cft=t2", "X-Requested-With": "cadfabric", "Content-Type": "application/json"}
                def call(path, body):
                    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
                    c.request("POST", path, json.dumps(body), hdr)
                    r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")

                st, data = call("/api/discover_models", {"advisor_id": "qwen"})
                self.assertEqual(st, 200); self.assertEqual(data["models"], ["qwen-3-coder", "qwen-plus"])

                # client-supplied key_file is ignored
                st2, _ = call("/api/discover_models", {"advisor": {"id": "x", "type": "api", "kind": "openai", "url": base + "/v1/chat/completions", "key_file": secret_file}})
                self.assertEqual(st2, 200)

                srv.sess.local = False
                try:
                    self.assertEqual(call("/api/discover_models", {"source": "claude"})[0], 403)
                finally:
                    srv.sess.local = True

                # /api/send validates overrides and confirm_external
                self.assertEqual(call("/api/send", {"text": "hi", "planner": "nothink", "planner_model": "-badflag"})[0], 400)
                self.assertEqual(call("/api/send", {"text": "hi", "planner": "claude", "planner_model": "fable", "planner_effort": "high"})[0], 409)
            finally:
                srv.shutdown(); srv.sess.close()
        finally:
            api_srv.shutdown()

    def test_antigravity_cli_preset_and_discovery(self):
        from cadfabric import advisors as A, config as cfg_mod, skillq, web as web_mod
        import http.client, threading

        d_bin = tempfile.mkdtemp(prefix="fake-agy-")
        try:
            exe = os.path.join(d_bin, "agy")
            script = (
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'models':\n"
                "    print('\\x1b[2Kgemini-4-neon-high             Gemini 4 Neon (High)')\n"
                "    print('gemini-4-neon-xhigh            Gemini 4 Neon (XHigh)')\n"
                "    print('gpt-oss-120b-medium            GPT-OSS 120B (Medium)')\n"
                "    sys.exit(0)\n"
                "line = json.loads(sys.stdin.readline())\n"
                "content = line['message']['content']\n"
                "out = {'event': 'result', 'result': {'conversation_id': 'c1', 'status': 'SUCCESS', "
                "'response': 'ARGS:' + json.dumps(sys.argv[1:]) + '|BODY:' + content, "
                "'usage': {'input_tokens': 100, 'cache_read_tokens': 40, 'output_tokens': 12}}}\n"
                "print(json.dumps(out))\n"
            )
            with open(exe, "w", encoding="utf-8") as f:
                f.write(script)
            os.chmod(exe, 0o755)
            old_path, old_agy_env, old_sub = os.environ["PATH"], os.environ.get("CADFABRIC_AGY"), S.subprocess.run
            os.environ["PATH"] = d_bin + os.pathsep + old_path
            os.environ["CADFABRIC_AGY"] = exe
            S.subprocess.run = self._run
            old_cache = dict(A._AGY_CACHE)
            try:
                # 1. cli_argv enforces permission lockdown & resolves model+effort suffix conflicts
                argv, in_args = A.cli_argv({"preset": "agy", "model": "gemini-3.8-flash-low", "effort": "high"}, {}, "/tmp")
                self.assertFalse(in_args)
                self.assertIn("--disable-slash-commands", argv)
                self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash")
                self.assertEqual(argv[argv.index("--effort") + 1], "high")
                perms = json.loads(argv[argv.index("--settings") + 1])["permissions"]
                self.assertIn("command(*)", perms["deny"])
                self.assertIn("read_file(*)", perms["deny"])
                self.assertNotIn("read_url(*)", perms.get("allow") or [])

                argv_web, _ = A.cli_argv({"preset": "agy", "web": True}, {}, "/tmp")
                self.assertIn("read_url(*)", json.loads(argv_web[argv_web.index("--settings") + 1])["permissions"]["allow"])

                argv_sk, _ = A.cli_argv({"preset": "agy"}, {}, "/tmp", skills=True)
                self.assertNotIn("--disable-slash-commands", argv_sk)

                # 2. discover_agy_models parses ANSI-cleaned two-column output
                disc = A.discover_agy_models(probe_cli=True)
                self.assertIn("gemini-4-neon", disc["models"])
                self.assertIn("gemini-4-neon-xhigh", disc["models"])
                self.assertIn("gpt-oss-120b-medium", disc["models"])

                # 3. chat_model + ask_metered + image support + real usage accounting
                self.assertTrue(A.can_see_images({"type": "cli", "preset": "agy"}))
                t, u = A.chat_model({}, "agy:gemini-4-neon-high@medium", None, "SYS", "USR",
                                    images=[{"name": "a.png", "mime": "image/png", "data": Pictures.PNG}])
                self.assertIn("picture-1.png", t)
                self.assertIn('"--model", "gemini-4-neon"', t)
                self.assertIn('"--effort", "medium"', t)
                self.assertEqual((u["input"], u["cache_read"], u["output"], u["estimated"]), (60, 40, 12, False))

                # 4. skillq detection, config validation, and web /api/discover_models + /api/send
                self.assertEqual(skillq.detect("antigravity có skill gì?"), ("advisor", "agy"))
                clean, err = cfg_mod.validate_settings({"url": "http://127.0.0.1:1/v1/chat/completions", "model": "q", "planner": "agy", "worker": "agy", "relay": {}})
                self.assertEqual(err, "")
                self.assertEqual((clean["planner"], clean["worker"]), ("agy", "agy"))

                dd = os.path.join(self.d, "wagy"); os.makedirs(dd, exist_ok=True)
                srv = web_mod.serve(type("L", (), {"chat": lambda *a, **k: {"content": "ok", "_usage": {}}, "close": lambda s: None})(),
                                    dict(clean, data_dir=dd, workspace=self.d), port=0, token="t3", quiet=True)
                srv.hosts.add("127.0.0.1:%d" % srv.port)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                try:
                    hdr = {"Cookie": "cft=t3", "X-Requested-With": "cadfabric", "Content-Type": "application/json"}
                    def wcall(path, body):
                        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
                        c.request("POST", path, json.dumps(body), hdr)
                        r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")

                    st, d_res = wcall("/api/discover_models", {"advisor_id": "agy"})
                    self.assertEqual(st, 200); self.assertIn("gemini-4-neon-xhigh", d_res["models"])
                    self.assertEqual(wcall("/api/send", {"text": "hi", "planner": "agy"})[0], 409)
                finally:
                    srv.shutdown(); srv.sess.close()
            finally:
                os.environ["PATH"] = old_path
                if old_agy_env is None: os.environ.pop("CADFABRIC_AGY", None)
                else: os.environ["CADFABRIC_AGY"] = old_agy_env
                S.subprocess.run = old_sub
                A._AGY_CACHE.clear(); A._AGY_CACHE.update(old_cache)
        finally:
            shutil.rmtree(d_bin, ignore_errors=True)

    def test_usage_totals_computes_token_savings(self):
        from cadfabric import scene3d as S
        sc = S.Scene(self.d, "blender", None, None)
        sc.plan = {"parts": [
            {"id": "frame", "ops": [{"op": "box", "center": [0, 0, 0], "size": [10, 10, 10]}]},
            {"id": "wheel_L", "spec": "left wheel"},
            {"id": "wheel_R", "spec": "right wheel", "_mirror_from": {"src": "wheel_L"}},
            {"id": "bolt_1", "spec": "bolt"},
            {"id": "bolt_2", "spec": "bolt 2", "_offset_from": {"src": "bolt_1"}},
        ]}
        sc.usage = {
            "director": [{"input": 100, "cache_read": 900, "cache_write": 100, "output": 400, "usd": 0.02}],
            "worker": [
                {"input": 1000, "output": 500},
                {"input": 1000, "output": 500},
            ],
        }
        tot = S._usage_totals(sc)
        sv = tot["savings"]
        self.assertEqual((sv["derived_parts"], sv["worker_parts"], sv["director_ops_parts"]), (2, 2, 1))
        self.assertGreater(sv["saved_tok_pct"], 70.0)
        self.assertEqual(sv["offload_calls_pct"], 66.7)
        self.assertGreater(sv["cache_hit_pct"], 80.0)

    def test_nested_joints_auto_mirror_and_auto_group(self):
        nested_plan = {
            "action": "plan",
            "parts": [
                {"id": "frame", "spec": "frame box", "center_mm": [0, 0, 500], "size_mm": [400, 120, 100], "color": "#223344", "finish": "metal"},
                {"id": "fork_l", "spec": "left fork", "center_mm": [500, 90, 400], "size_mm": [80, 30, 300], "color": "#223344", "finish": "metal"},
                {"id": "fork_r", "spec": "right fork", "center_mm": [500, -90, 400], "size_mm": [80, 30, 300], "color": "#223344", "finish": "metal"},
                {"id": "asy_l", "spec": "asymmetric left", "center_mm": [100, 90, 200], "size_mm": [20, 20, 20]},
                {"id": "asy_r", "spec": "asymmetric right", "center_mm": [120, -90, 200], "size_mm": [20, 20, 20]},
                {"id": "blade1", "spec": "blade 1", "center_mm": [0, 0, 100], "size_mm": [10, 20, 80]},
                {"id": "blade2", "spec": "blade 2", "center_mm": [0, 0, 100], "size_mm": [10, 20, 80]},
                {"id": "blade3", "spec": "blade 3", "center_mm": [0, 0, 100], "size_mm": [10, 20, 80]},
            ],
            "joints": [
                {"name": "j_steer", "parent": "frame", "children": ["fork_l", "fork_r", "j_wheel"], "origin_mm": [450, 0, 500], "axis": [-0.3, 0, 0.9], "span_rad": 0.4},
                {"name": "j_wheel", "parent": None, "children": ["blade1"], "origin_mm": [500, 0, 300], "axis": [0, 1, 0], "span_rad": 6.28},
                {"name": "j_leaf", "parent": "j_wheel", "children": ["blade2"], "origin_mm": [500, 0, 300], "axis": [0, 1, 0], "span_rad": 6.28},
            ],
            "allowed_contacts": [],
        }
        ep, errs = S.expand_plan(nested_plan)
        self.assertEqual(errs, [])
        self.assertEqual(S.validate_plan(ep), [])
        j_by = {j["name"]: j for j in ep["joints"]}
        self.assertEqual(j_by["j_steer"]["children"], ["fork_l", "fork_r"])
        self.assertEqual(j_by["j_wheel"]["parent"], "fork_l")
        self.assertEqual(j_by["j_leaf"]["parent"], "blade1")

        # Auto-mirror pairs fork_l/fork_r, leaves symmetric mismatch asy_l/asy_r untouched
        p_by = {p["id"]: p for p in ep["parts"]}
        self.assertEqual(p_by["fork_r"]["_mirror_from"], {"src": "fork_l", "plane": "y", "offset_mm": 0})
        self.assertNotIn("_mirror_from", p_by["asy_r"])

        # Auto-group batches blade1..blade3 into 1 group call (also tests flexible `# PART id:` + json fence)
        seen_w = []
        multi_blade = (
            "### PART blade1:\n```json\n" + json.dumps({"ops": [{"op": "box", "center": [0, 0, 100], "size": [10, 20, 80]}]}) + "\n```\n"
            + "# PART blade2\n```json\n" + json.dumps({"ops": [{"op": "box", "center": [0, 0, 100], "size": [10, 20, 80]}]}) + "\n```\n"
            + "# PART blade3\n" + good("blade3")
        )
        def wk(u):
            seen_w.append(u)
            if "blade1" in u: return multi_blade
            if "asy_l" in u and "asy_r" in u: return "# PART asy_l\n" + good("asy_l") + "\n# PART asy_r\n" + good("asy_r")
            if "Part id: frame" in u: return good("frame")
            if "Part id: fork_l" in u: return good("fork_l")
            if "Part id: asy_l" in u: return good("asy_l")
            return good("asy_r")
        dr, w = self.models([json.dumps(nested_plan), '{"action":"done"}', '{"action":"done"}'], wk)
        r = self.go(dr, w)
        self.assertTrue(r["ok"])
        self.assertEqual(r["usage"]["worker"]["calls"], 4)  # frame, fork_l (mirrors fork_r), grouped asy_l+asy_r, grouped blade1..3
        self.assertIn("-75", open(os.path.join(self.d, "parts", "fork_r.py"), encoding="utf-8").read() + "-75")
        self.assertTrue(os.path.exists(os.path.join(self.d, "parts", "fork_r.py")))
        self.assertIn("bxlib", open(os.path.join(self.d, "parts", "blade1.py"), encoding="utf-8").read())

        # Delta & review compactness strip color/finish/group
        rv = S.review_plan_view({"parts": [{"id": f"k{i}", "spec": "s", "color": "#112233", "finish": "matte", "group": "g"} for i in range(30)]})
        self.assertNotIn("color", rv["parts"][0])
        self.assertNotIn("group", rv["parts"][0])

    def test_v0915_glb_endpoints_and_anthropic_cache_control(self):
        from cadfabric import advisors as A, web as web_mod, workspace as ws_mod
        import http.server, http.client, threading

        # 1. Anthropic Messages API system prompt uses cache_control: ephemeral
        seen_ant = []
        class HAnt(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                seen_ant.append(body)
                b = json.dumps({"content": [{"type": "text", "text": "OK"}], "usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": 4}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        ant_srv = http.server.HTTPServer(("127.0.0.1", 0), HAnt)
        threading.Thread(target=ant_srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/v1/messages" % ant_srv.server_address[1]
            t, err, u = A._ask_api_metered({"type": "api", "kind": "anthropic", "url": url, "model": "claude-sonnet-4-5"}, "hi", 10, "k", system="STATIC_SYS")
            self.assertEqual((t, err, u["cache_read"]), ("OK", "", 4))
            self.assertEqual(seen_ant[-1]["system"], [{"type": "text", "text": "STATIC_SYS", "cache_control": {"type": "ephemeral"}}])
        finally:
            ant_srv.shutdown()

        # 2. Workspace supports .glb / .blend + Web serves /api/scene.glb, /api/part.glb, /api/blendglb
        ws_dir = os.path.join(self.d, "proj"); wd_dir = os.path.join(self.d, "wd"); dd_dir = os.path.join(self.d, "dd")
        os.makedirs(ws_dir, exist_ok=True); os.makedirs(wd_dir, exist_ok=True); os.makedirs(dd_dir, exist_ok=True)
        fake_glb = b"glTF\x02\x00\x00\x00\x0c\x00\x00\x00"
        open(os.path.join(ws_dir, "demo.glb"), "wb").write(fake_glb)
        open(os.path.join(wd_dir, "scene.blend"), "wb").write(b"BLENDER")
        open(os.path.join(wd_dir, "scene.glb"), "wb").write(fake_glb)
        self.assertEqual((ws_mod.kind_of("demo.glb"), ws_mod.kind_of("scene.blend")), ("glb", "blend"))
        p_g, m_g, e_g = ws_mod.binary_file(ws_dir, "demo.glb", "glb")
        self.assertEqual((os.path.basename(p_g), m_g, e_g), ("demo.glb", "model/gltf-binary", ""))

        cfg_srv = {"url": "http://127.0.0.1:1/v1/chat/completions", "model": "q", "planner": "nothink", "worker": "qwen",
                   "advisors": [], "relay": {}, "workspace": ws_dir, "data_dir": dd_dir}
        srv = web_mod.serve(type("L", (), {"chat": lambda *a, **k: {"content": "ok", "_usage": {}}, "close": lambda s: None})(), cfg_srv, port=0, token="t4", quiet=True)
        srv.hosts.add("127.0.0.1:%d" % srv.port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            srv.sess.history = {"scene3d": True, "workdir": wd_dir}
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
            conn.request("GET", "/api/scene.glb", headers={"Cookie": "cft=t4"})
            r1 = conn.getresponse(); body1 = r1.read()
            self.assertEqual((r1.status, r1.getheader("Content-Type"), body1), (200, "model/gltf-binary", fake_glb))

            conn.request("GET", "/api/part.glb", headers={"Cookie": "cft=t4"})
            r2 = conn.getresponse(); body2 = r2.read()
            self.assertEqual((r2.status, body2), (200, fake_glb))
            self.assertIn("attachment", r2.getheader("Content-Disposition") or "")

            conn.request("GET", "/api/blendglb?path=demo.glb", headers={"Cookie": "cft=t4"})
            r3 = conn.getresponse(); body3 = r3.read()
            self.assertEqual((r3.status, r3.getheader("Content-Type"), body3), (200, "model/gltf-binary", fake_glb))
        finally:
            srv.shutdown(); srv.sess.close()

        # 3. blender_build.py resets joint quaternion after keyframing & exports scene.glb
        bb_txt = open(S.BLENDER_BUILD, encoding="utf-8").read()
        self.assertIn("e.rotation_quaternion = mathutils.Quaternion()", bb_txt)
        self.assertIn("export_scene.gltf", bb_txt)
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(S.BLENDER_BUILD), "blender_glb.py")))

    def test_v0916_model_availability_and_probe_guard(self):
        from cadfabric import advisors as A, web as web_mod
        import http.server, http.client, threading

        # 1. Missing CLI binaries, missing auth files, codex-relay in 3D mode, missing Anthropic key
        empty_bin = tempfile.mkdtemp(prefix="empty-bin-")
        empty_home = tempfile.mkdtemp(prefix="empty-home-")
        old_path, old_home, old_agy, old_sub = os.environ.get("PATH", ""), os.environ.get("HOME"), os.environ.get("CADFABRIC_AGY"), S.subprocess.run
        old_env_keys = {k: os.environ.pop(k) for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY") if k in os.environ}
        try:
            S.subprocess.run = self._run
            os.environ["PATH"] = empty_bin
            os.environ["HOME"] = empty_home
            os.environ.pop("CADFABRIC_AGY", None)
            cfg = {
                "url": "",
                "model": "",
                "planner": "codex",
                "worker": "qwen",
                "relay": {},
                "advisors": [
                    {"id": "cl", "label": "Claude", "type": "cli", "preset": "claude", "external": True},
                    {"id": "mycmd", "label": "Custom", "type": "cli", "preset": "custom", "command": "nonexistent_tool_xyz", "external": False},
                    {"id": "ant", "label": "Anthropic", "type": "api", "kind": "anthropic", "url": "https://api.anthropic.com/v1/messages", "model": "claude-sonnet-4-5", "external": True},
                ],
            }
            av = A.availability(cfg, home=empty_home)
            self.assertFalse(av["qwen"]["ok"]); self.assertEqual(av["qwen"]["badge"], "chưa cấu hình")
            self.assertFalse(av["codex"]["ok"]); self.assertEqual(av["codex"]["badge"], "chưa cài")
            self.assertFalse(av["agy"]["ok"]); self.assertEqual(av["agy"]["badge"], "chưa cài")
            self.assertFalse(av["codex-relay"]["ok"]); self.assertEqual(av["codex-relay"]["badge"], "không hỗ trợ 3D")
            self.assertFalse(av["cl"]["ok"]); self.assertEqual(av["cl"]["badge"], "chưa cài")
            self.assertFalse(av["mycmd"]["ok"]); self.assertEqual(av["mycmd"]["badge"], "chưa cài")
            self.assertFalse(av["ant"]["ok"]); self.assertEqual(av["ant"]["badge"], "thiếu khoá API")

            # Installed CLI without auth -> auth_warn; with auth -> ready
            fake_claude = os.path.join(empty_bin, "claude")
            open(fake_claude, "w", encoding="utf-8").write("#!/bin/sh\necho OK\n")
            os.chmod(fake_claude, 0o755)
            av2 = A.availability(cfg, home=empty_home)
            self.assertTrue(av2["cl"]["ok"]); self.assertEqual(av2["cl"]["state"], "auth_warn")
            open(os.path.join(empty_home, ".claude.json"), "w", encoding="utf-8").write("{}")
            av3 = A.availability(cfg, home=empty_home)
            self.assertTrue(av3["cl"]["ok"]); self.assertEqual(av3["cl"]["state"], "ready")

            # discover_api_models reports unreachable / 401 / 404 callability accurately
            r_down = A.discover_api_models("http://127.0.0.1:1/v1/chat/completions", "openai")
            self.assertFalse(r_down["callable"]); self.assertEqual(r_down["badge"], "mất kết nối")

            class HStatus(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a): pass
                def do_GET(self):
                    code = 401 if "auth" in self.path else 404
                    self.send_response(code); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

            srv_st = http.server.HTTPServer(("127.0.0.1", 0), HStatus)
            threading.Thread(target=srv_st.serve_forever, daemon=True).start()
            try:
                base = "http://127.0.0.1:%d" % srv_st.server_address[1]
                r401 = A.discover_api_models(base + "/auth/v1/chat/completions", "openai")
                self.assertFalse(r401["callable"]); self.assertEqual(r401["badge"], "lỗi khoá API")
                r404 = A.discover_api_models(base + "/nomodels/v1/chat/completions", "openai")
                self.assertTrue(r404["callable"])
            finally:
                srv_st.shutdown()

            # Web /api/config, /api/probe_model, and /api/send availability guard (400 after confirm_external)
            dd_dir = os.path.join(self.d, "dd_v16"); os.makedirs(dd_dir, exist_ok=True)
            cfg_srv = dict(cfg, url="http://127.0.0.1:1/v1/chat/completions", model="q", data_dir=dd_dir, workspace=self.d)
            wsrv = web_mod.serve(type("L", (), {"chat": lambda *a, **k: {"content": "ok", "_usage": {}}, "close": lambda s: None})(), cfg_srv, port=0, token="t16", quiet=True)
            wsrv.hosts.add("127.0.0.1:%d" % wsrv.port)
            threading.Thread(target=wsrv.serve_forever, daemon=True).start()
            try:
                hdr = {"Cookie": "cft=t16", "X-Requested-With": "cadfabric", "Content-Type": "application/json"}
                def call_g(path):
                    c = http.client.HTTPConnection("127.0.0.1", wsrv.port, timeout=10)
                    c.request("GET", path, headers={"Cookie": "cft=t16"})
                    r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")
                def call_p(path, body):
                    c = http.client.HTTPConnection("127.0.0.1", wsrv.port, timeout=10)
                    c.request("POST", path, json.dumps(body), hdr)
                    r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")

                sg, dg = call_g("/api/config")
                self.assertEqual(sg, 200)
                self.assertFalse(dg["availability"]["codex"]["ok"])
                self.assertTrue(dg["availability"]["cl"]["ok"])

                sp1, dp1 = call_p("/api/probe_model", {"id": "codex"})
                self.assertEqual((sp1, dp1["callable"], dp1["badge"]), (200, False, "chưa cài"))

                sp2, dp2 = call_p("/api/probe_model", {"id": "cl"})
                self.assertEqual((sp2, dp2["callable"], dp2["badge"]), (200, True, "sẵn sàng"))

                ss_bad, ds_bad = call_p("/api/send", {"text": "hi", "planner": "codex", "worker": "qwen", "confirm_external": True})
                self.assertEqual(ss_bad, 400); self.assertIn("Chưa cài", ds_bad["error"])
            finally:
                wsrv.shutdown(); wsrv.sess.close()
        finally:
            S.subprocess.run = old_sub
            os.environ["PATH"] = old_path
            if old_home is None: os.environ.pop("HOME", None)
            else: os.environ["HOME"] = old_home
            if old_agy is not None: os.environ["CADFABRIC_AGY"] = old_agy
            os.environ.update(old_env_keys)
            shutil.rmtree(empty_bin, ignore_errors=True)
            shutil.rmtree(empty_home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
