"""P1.2: the proto compiles and keeps the fields the plan depends on."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROTO = ROOT / "contracts/protobuf/aicf/v1/fabric.proto"

pytestmark = pytest.mark.skipif(shutil.which("protoc") is None, reason="protoc not installed")


def _describe(tmp_path):
    out = tmp_path / "fabric.desc"
    subprocess.run(
        ["protoc", f"-I{ROOT/'contracts/protobuf'}", f"--descriptor_set_out={out}", str(PROTO)],
        check=True,
    )
    from google.protobuf import descriptor_pb2  # type: ignore

    fds = descriptor_pb2.FileDescriptorSet()
    fds.ParseFromString(out.read_bytes())
    return {m.name: {f.name: f.number for f in m.field} for m in fds.file[0].message_type}


def test_protoc_compiles(tmp_path):
    subprocess.run(
        ["protoc", f"-I{ROOT/'contracts/protobuf'}", f"--descriptor_set_out={tmp_path/'x'}", str(PROTO)],
        check=True,
    )


def test_required_messages_and_field_numbers(tmp_path):
    pytest.importorskip("google.protobuf")
    m = _describe(tmp_path)
    for name in ["WorkerCapacity", "ModelIdentity", "VramBuckets", "VramEstimate", "WorkloadJob", "LeaseToken"]:
        assert name in m, name
    assert m["ModelIdentity"]["gguf_sha256"] == 9
    assert m["ModelIdentity"]["trust_remote_code"] == 11
    assert m["VramBuckets"]["headroom_bytes"] == 6  # 6th bucket, plan §B.2
    assert m["VramBuckets"]["allocatable_bytes"] == 7


def test_wire_compat_with_architecture_doc(tmp_path):
    """[KT] §11.2 field numbers must not move (backward compatibility)."""
    pytest.importorskip("google.protobuf")
    m = _describe(tmp_path)
    assert m["WorkerCapacity"]["worker_id"] == 1 and m["WorkerCapacity"]["user_is_active"] == 7
    assert m["ModelIdentity"]["weight_sha256"] == 2 and m["ModelIdentity"]["quantization"] == 5
    assert m["WorkloadJob"]["deadline_unix_ms"] == 5
