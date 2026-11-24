#!/usr/bin/env python3
"""Agent Skills open-standard conformance: nested metadata, audience, spec notes, discovery, export, relevance fitting, role filtering."""
import os, tempfile, unittest
from cadfabric import skills as K

SK = """---
name: demo-skill
description: Demo for tests
license: MIT
compatibility: any
disable-model-invocation: true
metadata:
  audience: worker
  author: x
---
# Intro
Use bevels on visible edges.
# Wheels
Rim radius 214 and tire radius 289 for the wheels.
# Lights
Headlight lens disc radius 70.
"""


class Std(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sk-"); self.src = os.path.join(self.d, "demo-skill"); os.makedirs(self.src)
        with open(os.path.join(self.src, "SKILL.md"), "w") as f: f.write(SK)

    def test_nested_metadata_and_flat_fields(self):
        m, body = K.parse_frontmatter(SK)
        self.assertEqual(m["metadata"], {"audience": "worker", "author": "x"}); self.assertEqual(m["license"], "MIT"); self.assertIn("Intro", body)
        self.assertEqual(K.audience_of(m), "worker"); self.assertEqual(K.audience_of({}), "both"); self.assertEqual(K.audience_of({"metadata": {"audience": "zzz"}}), "both")

    def test_import_records_spec_fields(self):
        dd = os.path.join(self.d, "data"); os.makedirs(os.path.join(dd, "skills"))
        m, err = K.import_folder(dd, "", self.src, "test"); self.assertEqual(err, "")
        self.assertEqual((m["audience"], m["implicit"], m["license"]), ("worker", False, "MIT"))
        self.assertTrue(K.for_role(m, "worker")); self.assertFalse(K.for_role(m, "director"))

    def test_spec_notes(self):
        n = " | ".join(K.spec_notes({"name": "other", "description": "x" * 1100, "allowed-tools": "Bash"}, self.src, "a\n" * 600))
        for w in ("differs", "1024", "500 lines", "allowed-tools"): self.assertIn(w, n)
        self.assertEqual(K.spec_notes({"name": "demo-skill", "description": "ok"}, self.src, "short"), [])

    def test_fit_keeps_whole_skill_when_it_fits_and_picks_relevant_sections_when_not(self):
        body = K.parse_frontmatter(SK)[1]
        self.assertEqual(K.fit(body, "x", 10_000), body)
        big = body + "\n# Filler\n" + ("lorem ipsum dolor " * 200)
        out = K.fit(big, "wheel rim tire radius", 260)
        self.assertIn("Rim radius 214", out); self.assertNotIn("lorem", out); self.assertIn("left out for length", out); self.assertLessEqual(len(out), 260 + 220)

    def test_export_copies_without_metadata_and_never_overwrites(self):
        dd = os.path.join(self.d, "data"); os.makedirs(os.path.join(dd, "skills"))
        K.import_folder(dd, "", self.src, "t"); K.set_field(dd, "", "demo-skill", status="approved")
        dest = os.path.join(self.d, "out")
        p, err = K.export(dd, "", "demo-skill", "agents", dest); self.assertEqual(err, "")
        self.assertTrue(os.path.isfile(os.path.join(p, "SKILL.md"))); self.assertFalse(os.path.exists(os.path.join(p, "skill.json")))
        self.assertIn("already exists", K.export(dd, "", "demo-skill", "agents", dest)[1])
        self.assertIn("target", K.export(dd, "", "demo-skill", "x", dest)[1])

    def test_discover_reads_claude_code_folders(self):
        home = os.path.join(self.d, "home"); os.makedirs(os.path.join(home, ".claude", "skills")); import shutil
        shutil.copytree(self.src, os.path.join(home, ".claude", "skills", "demo-skill"))
        dd = os.path.join(self.d, "data"); os.makedirs(os.path.join(dd, "skills")); old = os.environ.get("HOME"); os.environ["HOME"] = home
        try: self.assertEqual(K.discover(dd, ""), ["demo-skill"])
        finally: os.environ["HOME"] = old
        self.assertEqual(K.list_for(dd, "")[0]["status"], "pending")


class Roles(unittest.TestCase):
    def test_worker_gets_only_worker_skills_fitted_to_the_part(self):
        from cadfabric import scene3d as S
        sc = S.Scene.__new__(S.Scene)
        big = "# Wheels\nrim radius 214\n# Lights\n" + "lamp " * 5000
        sc.skills = {"names": ["a", "b"], "worker_names": ["b"], "read": lambda n: big, "fit": K.fit, "explicit": "DIRECTOR TEXT", "explicit_worker": ""}
        sc.pick_skills({"skills": ["a", "b"]}); self.assertEqual(sc.skill_names, ["b"])
        t = sc.worker_skill_text("wheel rim radius")
        self.assertIn("rim radius 214", t); self.assertNotIn("DIRECTOR TEXT", t); self.assertLessEqual(len(t), 14600)
        self.assertIn("DIRECTOR TEXT", sc.director_skill_text())


if __name__ == "__main__":
    unittest.main()
