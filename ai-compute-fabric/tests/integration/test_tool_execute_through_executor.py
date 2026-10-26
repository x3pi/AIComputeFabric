"""TOOL_EXECUTE as the orchestrator sees it: Task -> LlamaExecutor.execute -> signed ToolRegistry ->
sandbox -> TaskResult -> validate_result. The model is not involved (it only proposes calls); this
checks the path a proposed call takes. The sandbox is real Docker, so without Docker these tests
are skipped, not passed."""
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agents.executors import LlamaExecutor
from agents.sandbox import DockerSandbox
from agents.tasks import Task, TaskType
from agents.tool_registry import ToolDenied, ToolRegistry, ToolSpec, file_sha256, sign_manifest
from agents.validator import validate_result

TOOLS = Path(__file__).resolve().parents[2] / "agents/tools"
# NB: the registry rejects descriptions that merely contain "eval", even as in "Evaluate" (fail closed by design)
CALC_IN = {"type": "object", "properties": {"expr": {"type": "string", "maxLength": 200}}, "required": ["expr"], "additionalProperties": False}
pytestmark = pytest.mark.skipif(not DockerSandbox.available(), reason="needs Docker for the real sandbox")


@pytest.fixture(scope="module")
def executor():
    key = Ed25519PrivateKey.generate()
    spec = ToolSpec(name="calc", version="1", description="Compute an arithmetic expression.", input_schema=CALC_IN,
                    output_schema={"type": "object"}, tenant_scope=("acme",), entry="calc.py", entry_sha256=file_sha256(TOOLS / "calc.py"))
    manifest, sig = sign_manifest([spec], key)
    reg = ToolRegistry(TOOLS, key.public_key())
    reg.load(manifest, sig)
    return LlamaExecutor(tools=reg, sandbox=DockerSandbox())


def task(tenant, tool, args, tid="t1"):
    return Task(task_id=tid, tenant_id=tenant, type=TaskType.TOOL_EXECUTE, input={"tool": tool, "args": args})


def test_allowed_call_runs_in_the_sandbox_and_validates(executor):
    t = task("acme", "calc", {"expr": "2 + 3 * 4"})
    res = executor.execute(t)
    assert res.output["result"] == {"value": 14}
    assert validate_result(t, res).ok


@pytest.mark.parametrize("tenant,tool,args,reason", [
    ("other", "calc", {"expr": "1+1"}, "tenant_not_in_scope"),
    ("acme", "calc", {"expr": "1+1", "extra": "x"}, "arguments_violate_schema"),
    ("acme", "calc", {"expr": 5}, "arguments_violate_schema"),
    ("acme", "bash", {"cmd": "id"}, "unknown_tool"),
    ("acme", "calc", {}, "arguments_violate_schema"),
])
def test_refused_calls_raise_with_a_reason_and_are_audited(executor, tenant, tool, args, reason):
    before = len(executor.tools.audit)
    with pytest.raises(ToolDenied) as e:
        executor.execute(task(tenant, tool, args))
    assert e.value.reason == reason
    assert executor.tools.audit[-1]["outcome"] == "denied" and len(executor.tools.audit) == before + 1


def test_tool_error_inside_the_sandbox_is_returned_but_fails_validation(executor):
    t = task("acme", "calc", {"expr": "__import__('os').system('id')"})
    res = executor.execute(t)
    assert "error" in res.output["result"] and "value" not in res.output["result"]
    rep = validate_result(t, res)
    assert not rep.ok and any("tool reported an error" in e for e in rep.errors)
