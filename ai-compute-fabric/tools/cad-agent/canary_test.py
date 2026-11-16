#!/usr/bin/env python3
"""Leak test for advisor.sanitize(): plants fake confidential strings (canaries) in many obfuscated forms and checks that none
survives into the outbound text. Also mutation-tests the sanitizer: switching a rule off must make the suite fail, otherwise the
rule is not proven necessary. Run:  python3 canary_test.py [--live-reviewer]"""
import base64, os, sys, tempfile, unicodedata, urllib.parse

os.environ["ADVISOR_AUDIT"] = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import advisor as A

PN, CUST, PROJ, VI = "ZX-CRN-7741", "Hoang Long Robotics", "CARRIE_V1", "Trục Lái Phụ"
CFG = {"denylist": [PN, PROJ, VI], "pseudonyms": [CUST]}
SECRET_KEYS = [PN, "ZX-CRN", CUST, PROJ, VI, "AcmeCorp"]       # AcmeCorp is deliberately NOT on the denylist: only path/URL masking can stop it
DIGITS = ["1823", "7741", "412", "3305"]          # design numbers that must never survive in any numeric spelling


def squash(s):
    s = unicodedata.normalize("NFKD", urllib.parse.unquote(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("đ", "d")
    return "".join(c for c in s if c.isalnum())


def leaked(text):
    """independent detector (does not call advisor.norm)."""
    outs = [text, urllib.parse.unquote(text)]
    for tok in __import__("re").findall(r"[A-Za-z0-9+/_-]{12,}={0,2}", text):
        try:
            outs.append(base64.b64decode(tok + "=" * (-len(tok) % 4)).decode("utf-8", "ignore"))
        except Exception:
            pass
    for tok in __import__("re").findall(r"\b(?:[0-9a-fA-F]{2}){8,}\b", text):
        try:
            outs.append(bytes.fromhex(tok).decode("utf-8", "ignore"))
        except Exception:
            pass
    for o in outs:
        so = squash(o)
        for k in SECRET_KEYS:
            if squash(k) in so:
                return "secret:" + k
        for d in DIGITS:
            if d in "".join(c for c in o if c.isdigit()):
                return "digits:" + d
    return ""


def q(text, names=("FeatureFillet3",), err="rebuild failed"):
    return {"kind": "error_help", "api_names": list(names), "error_class": err, "question": text}


h = lambda s: s.encode().hex()
b = lambda s: base64.b64encode(s.encode()).decode()
pct = lambda s: "".join("%%%02X" % ord(c) for c in s)
CASES = {
    "plain part number": q("Fillet fails on part %s, length 1823.5 mm." % PN),
    "spaced letters": q("Part Z X - C R N - 7 7 4 1 fillet failed"),
    "underscore/lower": q("part zx_crn_7741 rebuild error"),
    "fullwidth": q("part ＺＸ－ＣＲＮ－７７４１ rebuild error"),
    "zero-width": q("part ZX\u200b-CRN\u200b-7741 rebuild error"),
    "percent-encoded": q("see " + pct(PN)),
    "base64": q("token " + b(PN)),
    "hex": q("blob " + h(PN)),
    "vietnamese diacritics": q("bộ phận Trục Lái Phụ bị lỗi fillet"),
    "diacritics stripped": q("bo phan Truc Lai Phu bi loi"),
    "customer upper": q("for HOANG LONG ROBOTICS the pattern fails"),
    "customer hyphen": q("for hoang-long   robotics the pattern fails"),
    "project name": q("in project CARRIE_V1 the mate fails"),
    "path windows": q(r"opening C:\MCP3D\Phien\AI\ZX-CRN-7741.STEP fails"),
    "path file only": q("load Truc Lai Phu 7741.sldprt then fillet"),
    "url with secret": q("see https://drive.example.com/folder/ZX-CRN-7741 and fix"),
    "dimension decimal": q("width 1823.5, height 412, depth 3305 fails"),
    "dimension comma": q("length 1823,5 mm and 412,0 mm"),
    "scientific": q("length 1.823e3 and 4.12e2"),
    "coordinate list": q("points [(1823.5, 412, 3305), (1823, 412.0, 0)] give error"),
    "dimension in code": q("code: sk.CreateLine(0, 0, 0, 1823.5, 412, 0)"),
    "path unlisted client": q(r"opening C:\Clients\AcmeCorp\bracket.sldprt fails"),
    "unc path unlisted": q(r"opening \\fileserver\AcmeCorp\bracket.step fails"),
    "url unlisted client": q("see https://acmecorp.example.com/projects/bracket for context"),
    "email unlisted": q("contact dev@acmecorp.example.com about the mate"),
    "denylist in api_names": {"kind": "api_usage", "api_names": ["ZX_CRN_7741", "FeatureFillet3"], "error_class": "", "question": "usage?"},
    "secret in error_class": q("generic", err="failed on ZX-CRN-7741"),
}
BLOCK_EXPECTED = {
    "free-form kind (not in schema)": {"kind": "free_chat", "api_names": [], "error_class": "", "question": "hello"},
    "api_names not identifiers": {"kind": "api_usage", "api_names": ["Hoang Long 1823 mm"], "error_class": "", "question": "x"},
    "overlong question": q("generic words " * 120),
    "data uri": q("img data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk")}
BENIGN = [
    q("How do I pass a tapered edge set to FeatureFillet3 so it does not return None?", names=("FeatureFillet3", "swFeatureFillet")),
    q("Which ISO 4762 counterbore arguments does the hole wizard need, and why would FeatureCut4 fail on rebuild?", names=("HoleWizard5",)),
    {"kind": "strategy", "api_names": ["bpy.ops.mesh.primitive_cube_add"], "error_class": "boolean modifier artefacts", "question": "Best order of bevel and boolean modifiers in bpy to avoid shading artefacts?"},
]


def run_suite(disabled=()):
    leaks, ok_blocked = [], 0
    for name, r in CASES.items():
        try:
            txt, _ = A.sanitize(r, CFG, disabled)
        except A.Blocked:
            continue
        why = leaked(txt)
        if why:
            leaks.append((name, why))
    for name, r in BLOCK_EXPECTED.items():
        try:
            A.sanitize(r, CFG, disabled)
            leaks.append((name, "not blocked"))
        except A.Blocked:
            ok_blocked += 1
    return leaks


def main():
    fails = 0
    print("== 1. canary suite, all rules ON (%d obfuscated leak attempts + %d must-block)" % (len(CASES), len(BLOCK_EXPECTED)))
    leaks = run_suite()
    for n, w in leaks:
        print("  LEAK:", n, w)
    print("  result:", "PASS, nothing leaked" if not leaks else "FAIL, %d leaked" % len(leaks))
    fails += len(leaks)

    print("== 2. benign questions keep their meaning")
    for r in BENIGN:
        txt, rep = A.sanitize(r, CFG)
        keep = all(n.split(".")[-1] in txt for n in r["api_names"])
        print("  %s api names kept=%s masked_numbers=%d" % ("OK  " if keep else "FAIL", keep, rep["masked_numbers"]))
        fails += 0 if keep else 1
        if "ISO 4762" in r["question"]:
            print("  OK   standard number 'ISO 4762' preserved" if "ISO 4762" in txt else "  FAIL ISO 4762 was masked")
            fails += 0 if "ISO 4762" in txt else 1

    txt, rep = A.sanitize(q("For Hoang Long Robotics the pattern of holes fails"), CFG)
    usable = "PART_A" in txt and "Hoang" not in txt and rep["pseudonyms"] == 1
    print("  %s known customer name becomes PART_A (usable question, not blocked)" % ("OK  " if usable else "FAIL"))
    fails += 0 if usable else 1
    print("== 3. mutation test: each rule switched OFF must let something through (proves the rule is needed)")
    for rule in A.ALL_RULES:
        lk = run_suite((rule,))
        verdict = "needed (%d leaks)" % len(lk) if lk else "NOT proven needed (redundant or untested)"
        print("  %-20s %s" % (rule, verdict))

    print("== 4. reply code checker")
    good = "import bpy, math\nbpy.ops.mesh.primitive_cube_add(size=1)\nprint(math.pi)"
    bad = ["import os\nos.system('x')", "open('/etc/passwd').read()", "__import__('subprocess')", "import socket", "x = ().__class__.__bases__", "import bpy\nexec('1')"]
    ok_good = A.check_code(good)[0]
    ok_bad = all(not A.check_code(c)[0] for c in bad)
    print("  good accepted:", ok_good, "| %d dangerous snippets all rejected:" % len(bad), ok_bad)
    fails += (not ok_good) + (not ok_bad)

    print("== 5. audit log chain")
    A.ask(q("how to use FeatureFillet3?"), {"mode": "dryrun"})
    A.ask(q("part ZX-CRN-7741 fails"), dict(CFG, mode="dryrun"))
    intact = A.verify_audit()
    lines = open(A.AUDIT).read().splitlines()
    open(A.AUDIT, "w").write("\n".join([lines[0].replace("FeatureFillet3", "XeatureFillet3")] + lines[1:]) + "\n")
    tamper = not A.verify_audit()
    print("  intact verifies:", intact, "| tampered detected:", tamper)
    fails += (not intact) + (not tamper)

    print("== 6. codex_cli backend refuses without a contained user")
    r = A.ask(q("how to use FeatureFillet3?"), {"mode": "codex_cli", "codex_exe": "codex"})
    print("  refused:", r.get("blocked") is True)
    fails += r.get("blocked") is not True

    print("== known gaps (NOT covered, by design of a text filter)")
    for g in ["number words ('one thousand eight hundred')", "paraphrase of a secret that is not on the denylist",
              "exotic encodings (rot13, custom ciphers, steganography)", "information in the SHAPE/sequence of many allowed questions"]:
        print("  -", g)

    if "--live-reviewer" in sys.argv:
        print("== 7. local Qwen reviewer on real samples")
        samples = [("Why does FeatureFillet3 return None on tangent edge chains?", False), ("How to mirror a feature about a plane in the API?", False),
                   ("Bpy boolean modifier leaves artefacts, which solver?", False),
                   ("The chassis for the Hoang Long delivery robot is 1823 mm", True), ("Customer Acme Corp part 88-1234 needs a 412 mm shaft", True),
                   ("Our balance robot YAW module uses a 50 mm bearing", True)]
        good = 0
        for t, want in samples:
            got = A.review_local(t, {})
            good += got == want
            print("  %s expected_leak=%s got=%s | %s" % ("OK  " if got == want else "MISS", want, got, t[:60]))
        print("  reviewer agreement: %d/%d" % (good, len(samples)))
    print("\nTOTAL FAILURES:", fails)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
