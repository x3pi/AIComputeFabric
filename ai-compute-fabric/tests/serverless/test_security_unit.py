"""HMAC signing / verification of SERVERLESS_EXEC (threat T3)."""
import base64
import os

import pytest

from agents.sl_security import SignatureError, Verifier, load_keys, new_key_file, sign, sha256_hex

K1, K2 = b"k" * 40, b"j" * 40
H = "a" * 64


def body(**kw):
    b = {"tenant_id": "t1", "call_id": "c" * 16, "app_name": "app", "fn_id": "fn", "env_hash": H, "bundle_sha256": "b" * 64,
         "bundle_b64": "AAAA", "args_b64": base64.b64encode(b"args").decode(), "vram_mb": 0, "memory_mb": 512, "cpus": 1.0, "timeout_s": 30.0,
         "keep_warm_s": 60, "retries": 0, "env_vars": {"A": "1"}}
    b.update(kw)
    return b


def test_roundtrip_and_attempt_binding():
    s = sign([K1], body(), attempt=3)
    v = Verifier([K1])
    v.verify(s, 3)
    with pytest.raises(SignatureError):
        Verifier([K1]).verify(s, 4)


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "t2"), ("call_id", "d" * 16), ("fn_id", "other"), ("app_name", "x"), ("env_hash", "c" * 64), ("bundle_sha256", "e" * 64),
    ("vram_mb", 8192), ("memory_mb", 16384), ("cpus", 8.0), ("timeout_s", 600.0), ("keep_warm_s", 3600), ("retries", 5),
    ("env_vars", {"A": "2"}), ("gpu", "any"), ("method", "evil"), ("result_format", "json"), ("args_format", "json"), ("kind", "cls"),
    ("lease_id", "lease-1"),
])
def test_modifying_any_signed_field_is_detected(field, value):
    s = sign([K1], body(), 1)
    t = s.model_copy(update={field: value})
    with pytest.raises(SignatureError, match="bad signature"):
        Verifier([K1]).verify(t, 1)


def test_modified_args_bytes_detected_but_bundle_bytes_are_checked_by_sha():
    s = sign([K1], body(), 1)
    t = s.model_copy(update={"args_b64": base64.b64encode(b"EVIL").decode()})
    with pytest.raises(SignatureError):
        Verifier([K1]).verify(t, 1)
    # bundle bytes are not in the MAC (they are large): they are bound through the signed bundle_sha256, which the worker re-checks
    t2 = s.model_copy(update={"bundle_b64": "BBBB"})
    Verifier([K1]).verify(t2, 1)
    assert sha256_hex(base64.b64decode("BBBB")) != s.bundle_sha256


def test_args_ref_is_bound():
    s = sign([K1], body(args_b64=None, args_ref="f" * 64), 1)
    Verifier([K1]).verify(s, 1)
    with pytest.raises(SignatureError):
        Verifier([K1]).verify(s.model_copy(update={"args_ref": "0" * 64}), 1)


def test_expired_and_skew():
    now = 1_000_000.0
    s = sign([K1], body(), 1, ttl_s=10, now=now)
    Verifier([K1], clock=lambda: now + 12).verify(s, 1)             # 2 s past exp, inside the 5 s clock-skew allowance
    with pytest.raises(SignatureError, match="expired"):
        Verifier([K1], clock=lambda: now + 16).verify(s, 1)


def test_replayed_nonce_is_refused_and_release_allows_redelivery():
    s = sign([K1], body(), 1)
    v = Verifier([K1])
    v.verify(s, 1)
    with pytest.raises(SignatureError, match="replayed"):
        v.verify(s, 1)
    v.release(s)
    v.verify(s, 1)


def test_wrong_key_rejected_and_rotation():
    s = sign([K1], body(), 1)
    with pytest.raises(SignatureError):
        Verifier([K2]).verify(s, 1)
    Verifier([K2, K1]).verify(s, 1)                                  # old key still accepted during rotation
    s2 = sign([K2, K1], body(call_id="d" * 16), 1)                   # first key signs
    Verifier([K2]).verify(s2, 1)


def test_garbage_mac_shapes():
    s = sign([K1], body(), 1)
    for mac in ("0" * 64, "z" * 64, s.sig.mac.upper()):
        t = s.model_copy(update={"sig": s.sig.model_copy(update={"mac": mac})})
        with pytest.raises(SignatureError):
            Verifier([K1]).verify(t, 1)


def test_key_file_permissions_and_length(tmp_path):
    p = tmp_path / "k"
    new_key_file(p)
    assert (os.stat(p).st_mode & 0o777) == 0o600 and len(load_keys(p)[0]) >= 32
    os.chmod(p, 0o644)
    with pytest.raises(SignatureError, match="group/others"):
        load_keys(p)
    q = tmp_path / "short"
    q.write_text("tiny\n")
    os.chmod(q, 0o600)
    with pytest.raises(SignatureError):
        load_keys(q)
