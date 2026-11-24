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


if __name__ == "__main__":
    unittest.main(verbosity=1)
