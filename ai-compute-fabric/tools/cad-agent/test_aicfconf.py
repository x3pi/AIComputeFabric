#!/usr/bin/env python3
"""Unit tests for aicfconf (config lookup, overrides, key file, tool discovery). Run: python3 test_aicfconf.py"""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aicfconf as C

ENV_KEYS = ["AICF_CAD_CONFIG", "AICF_URL", "AICF_MODEL", "AICF_KEY_FILE", "SW_MCP_EXE", "SW_EXE", "AICF_DATA_DIR", "LOCALAPPDATA"]


class T(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def cfgfile(self, data, bom=False):
        p = os.path.join(self.d, "config.json")
        with open(p, "w", encoding="utf-8-sig" if bom else "utf-8") as f:
            json.dump(data, f)
        os.environ["AICF_CAD_CONFIG"] = p
        return p

    def test_defaults_without_file(self):
        os.environ["AICF_CAD_CONFIG"] = os.path.join(self.d, "missing.json")
        c = C.load()
        self.assertEqual(c["model"], C.DEFAULTS["model"])

    def test_file_values_and_bom(self):
        self.cfgfile({"url": "http://x:1/v1/chat/completions", "model": "m1"}, bom=True)   # PowerShell 5 writes a BOM
        c = C.load()
        self.assertEqual((C.url(c), C.model(c)), ("http://x:1/v1/chat/completions", "m1"))

    def test_env_beats_file(self):
        self.cfgfile({"url": "http://file/", "model": "fm"})
        os.environ["AICF_URL"], os.environ["AICF_MODEL"] = "http://env/", "em"
        c = C.load()
        self.assertEqual((C.url(c), C.model(c)), ("http://env/", "em"))

    def test_key_relative_and_absolute(self):
        k = os.path.join(self.d, "k.txt")
        open(k, "w").write("  secret-123\n")
        self.cfgfile({"key_file": k})
        self.assertEqual(C.read_key(), "secret-123")
        self.cfgfile({"key_file": "nope.txt"})
        with self.assertRaises(SystemExit):
            C.read_key()                                   # relative to the install dir, missing -> clear error, no traceback

    def test_empty_key_rejected(self):
        k = os.path.join(self.d, "k.txt")
        open(k, "w").write("  \n")
        self.cfgfile({"key_file": k})
        with self.assertRaises(SystemExit):
            C.read_key()

    def test_mcp_discovery_order(self):
        exe = os.path.join(self.d, "solidworks-mcp.exe")
        open(exe, "w").write("x")
        self.cfgfile({"mcp_exe": exe})
        self.assertEqual(C.mcp_exe(), exe)
        os.environ["SW_MCP_EXE"] = exe + ".missing"        # env wins only if the file exists, else falls through
        self.assertEqual(C.mcp_exe(), exe)
        self.cfgfile({"mcp_exe": os.path.join(self.d, "nothere.exe")})
        os.environ.pop("SW_MCP_EXE")
        with self.assertRaises(SystemExit):
            C.mcp_exe()

    def test_sw_exe_none_when_absent(self):
        self.cfgfile({})
        self.assertIsNone(C.sw_exe())                       # Linux: no registry, no default folder
        fake = os.path.join(self.d, "SLDWORKS.exe")
        open(fake, "w").write("x")
        self.cfgfile({"sw_exe": fake})
        self.assertEqual(C.sw_exe(), fake)

    def test_data_dir_created(self):
        p = os.path.join(self.d, "data", "sub")
        self.cfgfile({"data_dir": p})
        self.assertEqual(C.data_dir(), p)
        self.assertTrue(os.path.isdir(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
