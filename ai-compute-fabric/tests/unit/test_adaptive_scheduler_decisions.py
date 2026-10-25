import pytest

from core.adaptive_scheduler.scheduler import Request, Worker, select

ID = ("W", "T", "r1")
GB = 10**9


def req(**kw):
    base = dict(model_id="qwen3-8b", weight_sha256="W", tokenizer_sha256="T", revision="r1", required_bytes=7 * GB)
    base.update(kw)
    return Request(**base)


def w(wid, **kw):
    base = dict(worker_id=wid, site_id="A", allocatable_bytes=10 * GB)
    base.update(kw)
    return Worker(**base)


def reason(worker, r):
    best, why = select([worker], r)
    return best, why[worker.worker_id]


def test_picks_faster_decode():
    best, _ = select([w("slow", decode_tps=20), w("fast", decode_tps=50)], req())
    assert best.worker_id == "fast"


def test_prefers_already_loaded_model():
    cold = w("cold", decode_tps=60)
    warm = w("warm", decode_tps=40, loaded={"qwen3-8b": ID})
    best, _ = select([cold, warm], req())
    assert best.worker_id == "warm"          # T_cold_load = 0 beats faster decode


def test_rejects_tokenizer_mismatch():
    bad = w("x", loaded={"qwen3-8b": ("W", "OTHER", "r1")})
    assert reason(bad, req())[1] == "MODEL_IDENTITY_MISMATCH"


def test_rejects_revision_mismatch():
    bad = w("x", loaded={"qwen3-8b": ("W", "T", "r0")})
    assert reason(bad, req())[1] == "MODEL_IDENTITY_MISMATCH"


def test_rejects_other_site_for_restricted_data():
    other = w("x", site_id="B")
    assert reason(other, req(data_class="DATA_RESTRICTED", site_allowlist=("A",)))[1] == "DATA_RESIDENCY"


def test_restricted_without_allowlist_is_refused():
    assert reason(w("x"), req(data_class="DATA_RESTRICTED"))[1] == "RESTRICTED_NEEDS_ALLOWLIST"


def test_nominal_12gb_is_not_enough_only_allocatable_counts():
    # 12 GB card but only 5 GB allocatable (user + headroom hold the rest)
    assert reason(w("x", allocatable_bytes=5 * GB), req())[1] == "INSUFFICIENT_VRAM"


def test_user_active_worker_is_excluded():
    assert reason(w("x", user_is_active=True), req())[1] == "USER_ACTIVE"


def test_sla_violation_rejected():
    assert reason(w("x", queue_s=100), req(sla_s=30))[1] == "SLA"


def test_engine_unsupported():
    assert reason(w("x"), req(engine="vllm"))[1] == "ENGINE_UNSUPPORTED"


def test_no_candidate_returns_none_with_reasons():
    best, why = select([w("a", user_is_active=True), w("b", allocatable_bytes=1)], req())
    assert best is None and set(why.values()) == {"USER_ACTIVE", "INSUFFICIENT_VRAM"}


def test_tie_break_is_deterministic():
    best, _ = select([w("b"), w("a")], req())
    assert best.worker_id == "a"


def test_queue_pushes_choice_to_idle_worker():
    best, _ = select([w("busy", queue_s=30, decode_tps=60), w("idle", decode_tps=40)], req())
    assert best.worker_id == "idle"


def test_cold_load_counted_against_slow_disk():
    slow_disk = w("sd", cold_load_s=120, decode_tps=60)
    ok = w("ok", cold_load_s=10, decode_tps=40)
    best, _ = select([slow_disk, ok], req())
    assert best.worker_id == "ok"


def test_rtt_breaks_near_ties():
    a = w("far", rtt_ms=200); b = w("near", rtt_ms=1)
    best, _ = select([a, b], req())
    assert best.worker_id == "near"


def test_empty_pool():
    best, why = select([], req())
    assert best is None and why == {}
