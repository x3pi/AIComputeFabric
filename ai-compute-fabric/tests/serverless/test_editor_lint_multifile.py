"""Pha C3 & C5 Unit & Integration Tests:
- lint_python_source (ast.parse + undefined name + unused import + subprocess RLIMIT/timeout <= 2s, zero host execution)
- compile_source_bundle & DeploymentStore multi-file support (user_app.py importing helpers.py inside sandbox)
"""
from __future__ import annotations

import base64
import cloudpickle
import pytest

from serverless.deployments import (
    DeploymentError,
    DeploymentStore,
    compile_source_bundle,
    lint_python_source,
)


def test_lint_valid_code_and_unused_import_warning() -> None:
    src = (
        "import math\n"
        "import json\n\n"
        "def classify(payload: dict) -> dict:\n"
        "    return {'pi': round(math.pi, 4), 'n': len(payload)}\n"
    )
    res = lint_python_source(src, "user_app.py")
    assert res["syntax_valid"] is True
    assert res["ok"] is True
    assert res["functions"] == ["classify"]
    codes = [(d["code"], d["severity"], d["line"]) for d in res["diagnostics"]]
    assert ("unused_import", "warning", 2) in codes


def test_lint_detects_syntax_error_and_undefined_name_without_executing_code(tmp_path) -> None:
    marker = tmp_path / "pwned.txt"
    bad_syntax = f"import os; os.system('touch {marker}')\ndef broken(:\n    pass\n"
    res_syn = lint_python_source(bad_syntax, "user_app.py")
    assert res_syn["syntax_valid"] is False
    assert res_syn["ok"] is False
    assert res_syn["diagnostics"][0]["code"] == "syntax_error"
    assert res_syn["diagnostics"][0]["line"] == 2
    assert not marker.exists(), "Lint must NEVER execute user code on host"

    undef_src = (
        "def classify(payload: dict) -> dict:\n"
        "    return {'v': mystery_undefined_helper(payload)}\n"
    )
    res_undef = lint_python_source(undef_src, "user_app.py")
    assert res_undef["syntax_valid"] is True
    assert res_undef["ok"] is False
    assert any(d["code"] == "undefined_name" and d["line"] == 2 for d in res_undef["diagnostics"])


def test_lint_enforces_size_limit_and_multi_file_diagnostics() -> None:
    huge = "def f():\n    return 1\n" + ("# " + "a" * 1024 + "\n") * 520
    res_huge = lint_python_source(huge, "user_app.py")
    assert res_huge["ok"] is False
    assert res_huge["diagnostics"][0]["code"] == "source_too_large"

    res_multi = lint_python_source(
        "import helpers\ndef classify(p: dict) -> dict:\n    return {'x': helpers.compute(p)}\n",
        "user_app.py",
        extra_files={"helpers.py": "def compute(p):\n    return bad_var + 1\n"},
    )
    assert res_multi["ok"] is False
    assert any(d["file"] == "helpers.py" and d["code"] == "undefined_name" for d in res_multi["diagnostics"])


def test_multifile_bundle_compiles_and_executes_helper_module(tmp_path) -> None:
    ds = DeploymentStore(tmp_path / "deployments.json")
    summary = ds.deploy(
        "t_dev",
        "multi-app",
        [
            {
                "fn_id": "classify",
                "source_code": (
                    "import helpers\n\n"
                    "def classify(payload: dict) -> dict:\n"
                    "    return {'sum': helpers.add_bonus(payload.get('x', 0)), 'tag': helpers.TAG}\n"
                ),
                "files": {
                    "helpers.py": "TAG = 'from-helper-v1'\ndef add_bonus(v: int) -> int:\n    return int(v) + 42\n"
                },
            }
        ],
    )
    assert summary["active_version"].startswith("v-")
    assert "helpers.py" in summary["functions_detail"]["classify"]["files"]
    assert summary["active_version"] in summary["version_sources"]

    fndef, ver, _ = ds.resolve_function("t_dev", "multi-app", "classify")
    assert ver == summary["active_version"]
    bundle = cloudpickle.loads(base64.b64decode(fndef["bundle_b64"]))
    assert "helpers.py" in bundle["files"]
    out = bundle["obj"]({"x": 8})
    assert out == {"sum": 50, "tag": "from-helper-v1"}

    # Reject path traversal in extra_files
    with pytest.raises(DeploymentError) as exc_info:
        compile_source_bundle("def f(x): return x\n", "f", "user_app.py", extra_files={"../evil.py": "x=1"})
    assert exc_info.value.code == "invalid_filename"


def test_lint_missing_entrypoint_unicode_col_and_deep_ast_protection() -> None:
    # 1. Missing entrypoint diagnostic
    res_ep = lint_python_source(
        "def helper_only(x: int) -> int:\n    return x + 1\n",
        "user_app.py",
        entrypoint="classify",
    )
    assert res_ep["syntax_valid"] is True
    assert res_ep["ok"] is False
    assert any(d["code"] == "missing_entrypoint" for d in res_ep["diagnostics"])

    # 2. Unicode + tab column positioning and NUL byte rejection
    res_uni = lint_python_source(
        "# Chào buổi sáng Tiếng Việt\ndef f(x):\n\treturn x + biến_chưa_khai_báo\n",
        "user_app.py",
    )
    assert res_uni["ok"] is False
    diag = [d for d in res_uni["diagnostics"] if d["code"] == "undefined_name"][0]
    assert diag["line"] == 3
    assert diag["col"] >= 12

    res_nul = lint_python_source("def f():\x00\n    return 1\n", "user_app.py")
    assert res_nul["ok"] is False
    assert res_nul["diagnostics"][0]["code"] == "nul_byte"

    # 3. Deeply nested AST / subprocess recursion or timeout never crashes host process
    deep_expr = "def f():\n    return " + "(" * 500 + "1" + ")" * 500 + "\n"
    res_deep = lint_python_source(deep_expr, "user_app.py")
    assert res_deep["ok"] is False
    assert res_deep["diagnostics"][0]["code"] in ("syntax_error", "lint_resource_limit", "lint_timeout")

