"""The warm runner protocol, exercised as a plain subprocess (the runner is the only code allowed to call cloudpickle.loads)."""
import os

import pytest

from tests.serverless.util import RunnerProc, bundle_of, by_value, frame, value

by_value(__name__)


def add(a, b=1):
    return a + b


def noisy():
    print("hello from user code")
    import sys
    print("to stderr", file=sys.stderr)
    os.write(1, b"RAW-C-LEVEL-WRITE-TO-FD1")        # must not corrupt the protocol channel
    return 7


def boom():
    raise ValueError("kaput")


def exits():
    raise SystemExit(3)


def reads_protocol_stdin():
    return os.read(0, 10)                           # fd 0 points at /dev/null for user code


class Counter:
    n = 0

    def __init__(self):
        self.started = 0

    def load(self):
        Counter.n += 1
        self.started += 1

    def bump(self, k):
        return (Counter.n, self.started, k)

    def bye(self):
        print("exit hook")


@pytest.fixture
def runner():
    r = RunnerProc()
    yield r
    r.close()


def test_ready_frame(runner):
    assert runner.ready["ready"] is True and runner.ready["python"]


def test_call_and_bundle_cache(runner):
    sha, b = bundle_of(add)
    r1 = runner.call(sha, b, 2, b=3)
    assert value(r1) == 5 and r1["loaded"] is True
    r2 = runner.call(sha, None, 10)                 # bundle no longer needed
    assert value(r2) == 11 and r2["loaded"] is False


def test_unknown_bundle_without_payload_is_an_error_not_a_crash(runner):
    sha, _ = bundle_of(add)
    r = runner.call(sha, None, 1)
    assert r["ok"] is False and r["error"]["type"] == "LookupError"
    assert runner.call(sha, bundle_of(add)[1], 1)["ok"] is True


def test_sha_mismatch_rejected(runner):
    sha, b = bundle_of(add)
    bad = "0" * 64
    r = runner.call(bad, b, 1)
    assert r["ok"] is False and "sha256 mismatch" in r["error"]["message"]


def test_stdout_stderr_captured_and_channel_not_corrupted(runner):
    sha, b = bundle_of(noisy)
    r = runner.call(sha, b)
    assert value(r) == 7
    assert "hello from user code" in r["stdout"] and "to stderr" in r["stderr"]
    assert "RAW" not in r["stdout"]
    assert runner.call(sha, None)["ok"]             # channel still in sync afterwards


def test_output_is_capped(runner):
    def spam():
        print("x" * 5_000_000)
    sha, b = bundle_of(spam)
    f = {"op": "call", **frame(sha), "bundle_b64": b, "max_output_bytes": 1000}
    runner.send(f)
    r = runner.recv()
    assert r["ok"] and len(r["stdout"]) <= 1000 and r["truncated"] is True


def test_exception_reports_type_message_traceback_and_runner_survives(runner):
    sha, b = bundle_of(boom)
    r = runner.call(sha, b)
    assert r["ok"] is False and r["error"]["type"] == "ValueError" and r["error"]["message"] == "kaput" and "boom" in r["error"]["traceback"]
    assert value(runner.call(*bundle_of(add)[:1], bundle_of(add)[1], 1, 1)) == 2


def test_system_exit_in_user_code_does_not_kill_runner(runner):
    sha, b = bundle_of(exits)
    r = runner.call(sha, b)
    assert r["ok"] is False and r["error"]["type"] == "SystemExit"
    assert runner.call(sha, None)["ok"] is False and runner.p.poll() is None


def test_user_code_cannot_read_protocol_stdin(runner):
    sha, b = bundle_of(reads_protocol_stdin)
    assert value(runner.call(sha, b)) == b""


def test_json_result_format_and_unserialisable_result(runner):
    sha, b = bundle_of(add)
    r = runner.call(sha, b, 1, 2, result_format="json")
    assert r["format"] == "json" and value(r) == 3
    sha2, b2 = bundle_of(lambda: object())
    r2 = runner.call(sha2, b2, result_format="json")
    assert r2["ok"] is False and r2["error"]["type"] == "TypeError"


def test_class_enter_runs_once_and_state_persists(runner):
    Counter.n = 0
    sha, b = bundle_of(Counter, kind="cls", enter=["load"], exit=["bye"])
    r = [runner.call(sha, b if i == 0 else None, i, method="bump") for i in range(10)]
    assert [value(x)[0] for x in r] == [1] * 10 and [value(x)[1] for x in r] == [1] * 10
    assert r[0]["loaded"] is True and all(x["loaded"] is False for x in r[1:])


def test_oversized_request_frame_stops_runner_cleanly():
    r = RunnerProc(extra_env={"AICF_MAX_FRAME": "1000"})
    sha, b = bundle_of(add)
    r.send({"op": "call", **frame(sha, 1), "bundle_b64": b * 3})
    resp = r.recv()
    assert resp["ok"] is False and "exceeds" in resp["error"]["message"]
    r.p.wait(timeout=10)


def test_unknown_op(runner):
    runner.send({"op": "evil"})
    r = runner.recv()
    assert r["ok"] is False and r["error"]["type"] == "BadOp"
