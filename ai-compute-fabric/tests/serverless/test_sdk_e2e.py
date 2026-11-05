# ruff: noqa: F811  (pytest fixtures imported from test_e2e are intentionally re-declared as test arguments)
"""The SDK end to end against a real gateway + service + NATS + worker (reuses the module fixtures of test_e2e)."""
import subprocess
import sys
import textwrap

import pytest

from tests.serverless.test_e2e import ADMIN, ROOT, nats_url, secrets, stack, worker  # noqa: F401  (fixtures)

SDK = ROOT / "sdk/python"
sys.path.insert(0, str(SDK))
import aicf  # noqa: E402


def test_function_remote_roundtrip(stack):
    app = aicf.App("demo", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))

    @app.function(keep_warm_s=5)
    def square(x):
        return x * x

    assert square.remote(9) == 81
    assert square.local(3) == 9
    assert square.remote(x=4) == 16


def test_closures_and_defaults_travel_by_value(stack):
    app = aicf.App("demo", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))
    offset = 100

    @app.function()
    def add(x, y=5):
        return x + y + offset

    assert add.remote(1) == 106


def test_remote_exception_has_original_type_message_and_traceback(stack):
    app = aicf.App("demo", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))

    @app.function()
    def boom():
        print("before boom")
        raise KeyError("missing-key")

    with pytest.raises(aicf.RemoteError) as e:
        boom.remote()
    assert e.value.remote_type == "KeyError" and "missing-key" in e.value.remote_message and "boom" in e.value.remote_traceback
    assert "before boom" in e.value.stdout


def test_timeout_and_validation_errors_are_typed(stack):
    app = aicf.App("demo", client=aicf.Client(stack.gateway_url, stack.keys["alpha"]))

    @app.function(timeout_s=2)
    def slow():
        import time
        time.sleep(30)

    with pytest.raises(aicf.FunctionTimeout):
        slow.remote()

    @app.function(memory_mb=8192)
    def big():
        return 1

    with pytest.raises(aicf.QuotaExceeded, match="max_memory_mb=1024"):
        big.remote()

    @app.function(memory_mb=999999)
    def absurd():
        return 1

    with pytest.raises(aicf.InvalidRequest, match="memory_mb"):
        absurd.remote()

    @app.function(image=aicf.Image.debian_slim().uv_pip_install("git+https://evil/x"))
    def bad_env():
        return 1

    with pytest.raises(aicf.InvalidRequest):
        bad_env.remote()


def test_auth_errors_are_actionable(stack):
    app = aicf.App("demo", client=aicf.Client(stack.gateway_url, "aicf_wrong"))

    @app.function()
    def f():
        return 1

    with pytest.raises(aicf.AuthError, match="API key"):
        f.remote()
    app2 = aicf.App("demo", client=aicf.Client(stack.gateway_url, stack.keys["gamma"]))
    with pytest.raises(aicf.AuthError, match="not enabled for your tenant"):
        app2.function()(lambda: 1).remote()
    app3 = aicf.App("demo", client=aicf.Client("http://127.0.0.1:1", stack.keys["alpha"]))
    with pytest.raises(aicf.ConnectionFailed, match="aicf doctor"):
        app3.function()(lambda: 1).remote()


def test_config_errors(monkeypatch):
    monkeypatch.delenv("AICF_GATEWAY_URL", raising=False)
    with pytest.raises(aicf.ConfigError, match="AICF_GATEWAY_URL"):
        aicf.Client()
    monkeypatch.setenv("AICF_GATEWAY_URL", "http://x")
    monkeypatch.delenv("AICF_API_KEY", raising=False)
    with pytest.raises(aicf.ConfigError, match="AICF_API_KEY"):
        aicf.Client()


def test_python_version_mismatch_is_caught_locally():
    img = aicf.Image.debian_slim("3.11")
    with pytest.raises(aicf.PythonVersionMismatch, match="3.11"):
        img.check_local_python()


def test_cli_run_and_doctor(stack, tmp_path):
    script = tmp_path / "hello.py"
    script.write_text(textwrap.dedent('''
        import aicf
        app = aicf.App("cli")

        @app.function()
        def cube(x):
            return x ** 3
    '''))
    env = {"PATH": "/usr/bin:/bin", "AICF_GATEWAY_URL": stack.gateway_url, "AICF_API_KEY": stack.keys["alpha"], "PYTHONPATH": str(SDK)}
    r = subprocess.run([sys.executable, "-m", "aicf.cli", "run", str(script), "4"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and r.stdout.strip() == "64", r.stderr
    r = subprocess.run([sys.executable, "-m", "aicf.cli", "doctor"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout + r.stderr
    env["AICF_API_KEY"] = "aicf_nope"
    r = subprocess.run([sys.executable, "-m", "aicf.cli", "doctor"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "FAIL" in r.stdout
