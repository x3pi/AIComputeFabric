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


# ---- Go <-> Python wire compatibility: contracts/testdata/*.bin were written by Go (contracts/gen/go/golden_test.go) ----
import sys

GEN_PY = ROOT / "contracts/gen/python"
DATA = ROOT / "contracts/testdata"


@pytest.fixture(scope="module")
def pb():
    pytest.importorskip("google.protobuf")
    sys.path.insert(0, str(GEN_PY))
    try:
        from aicf.v1 import fabric_pb2
        yield fabric_pb2
    finally:
        sys.path.remove(str(GEN_PY))


def test_python_reads_bytes_written_by_go(pb):
    lt = pb.LeaseToken.FromString((DATA / "lease_token.bin").read_bytes())
    assert (lt.lease_id, lt.worker_id, lt.vram_reserved_bytes, lt.ttl_ms, lt.issued_unix_ms) == ("lease-1", "w-1", 6 << 30, 30000, 1790000000000)
    mi = pb.ModelIdentity.FromString((DATA / "model_identity.bin").read_bytes())
    assert (mi.gguf_sha256, mi.quantization, mi.license_spdx, mi.trust_remote_code) == ("cc", "Q4_K_M", "Apache-2.0", False)
    ve = pb.VramEstimate.FromString((DATA / "vram_estimate.bin").read_bytes())
    assert ve.confidence == pb.CONFIDENCE_CALIBRATED and ve.fit_status == "fits"
    assert ve.per_gpu[0].layer_end == 36 and ve.per_gpu[0].projected_buckets.weights_bytes == 4683 << 20
    wc = pb.WorkerCapacity.FromString((DATA / "worker_capacity.bin").read_bytes())
    assert list(wc.supported_engines) == ["llama"] and wc.user_is_active and wc.vram.total_bytes == 12 << 30


def test_python_reserializes_to_the_same_bytes_as_go(pb):
    for name, cls in [("lease_token", pb.LeaseToken), ("model_identity", pb.ModelIdentity), ("vram_estimate", pb.VramEstimate), ("worker_capacity", pb.WorkerCapacity)]:
        raw = (DATA / f"{name}.bin").read_bytes()
        assert cls.FromString(raw).SerializeToString(deterministic=True) == raw, name


def test_unknown_future_field_is_preserved_not_fatal(pb):
    raw = (DATA / "lease_token.bin").read_bytes() + bytes([0x78, 0x01])   # field 15 varint 1: a newer sender
    lt = pb.LeaseToken.FromString(raw)
    assert lt.lease_id == "lease-1"
