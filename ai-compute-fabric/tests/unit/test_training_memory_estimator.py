"""P6.3 training-memory estimator: checks against RECORDED measurements (no GPU needed) plus invariants.
The measurements are benchmarks/reports/p6_lora_memory_raw.jsonl (RTX 3060, Qwen3-0.6B/1.7B, LoRA r on 7 projections)."""
import json
from pathlib import Path

import pytest

from training import memory_estimator as me

ROOT = Path(__file__).resolve().parents[2]
RAW = [json.loads(l) for l in (ROOT / "benchmarks/reports/p6_lora_memory_raw.jsonl").read_text().splitlines()]
CFG = ROOT.parent / ".aicf-work/hf-train"
pytestmark = pytest.mark.skipif(not (CFG / "Qwen3-0.6B/config.json").exists(), reason="needs the downloaded Qwen3 config.json files")
SPEC = {m: me.ModelSpec.from_config(CFG / m / "config.json") for m in ("Qwen3-0.6B", "Qwen3-1.7B")}
S06 = SPEC["Qwen3-0.6B"]


def est(r):
    m, b, s, rk, ck, op = r["_cfg"]
    return me.estimate_mib(SPEC[m], b, s, rk, bool(ck), op)


def test_lora_parameter_count_matches_what_peft_reported():
    seen = {(r["_cfg"][0], r["_cfg"][3]): r["trainable_params"] for r in RAW if "trainable_params" in r}
    assert seen, "no recorded trainable_params"
    for (m, rk), n in seen.items():
        assert SPEC[m].lora_params(rk) == n, (m, rk)


def test_model_parameter_count_matches_the_checkpoint_size():
    for m, spec in SPEC.items():
        size = sum(f.stat().st_size for f in (CFG / m).glob("*.safetensors"))   # may be sharded
        # the hub file also stores the tied lm_head as a second copy of the embedding matrix; in memory it is one tensor
        dup = 2 * spec.vocab * spec.hidden if spec.tied else 0
        assert abs(2 * spec.model_params() + dup - size) / size < 0.005


def test_every_recorded_peak_is_within_the_safety_margin_and_never_far_underestimated():
    ok = [r for r in RAW if r["ok"]]
    assert len(ok) >= 40
    for r in ok:
        e = est(r)
        assert e * 1.08 >= r["peak_used_mib"], (r["_cfg"], e, r["peak_used_mib"])   # margin covers every measured peak
        assert e <= r["peak_used_mib"] * 1.12, (r["_cfg"], e, r["peak_used_mib"])   # and it is not uselessly pessimistic


def test_heldout_and_larger_model_rows_are_as_good_as_the_calibration_rows():
    for which in ("heldout", "scale"):
        rows = [r for r in RAW if r["set"] == which and r["ok"]]
        assert rows
        assert max(abs(est(r) - r["peak_used_mib"]) / r["peak_used_mib"] for r in rows) < 0.08


def test_every_recorded_out_of_memory_is_predicted_by_fits_in():
    oom = [r for r in RAW if not r["ok"]]
    assert len(oom) >= 8
    for r in oom:
        m, b, s, rk, ck, op = r["_cfg"]
        assert not me.fits_in(SPEC[m], 12288, b, s, rk, bool(ck), op), r["_cfg"]


def test_a_config_that_ran_with_room_to_spare_is_accepted():
    assert me.fits_in(S06, 12288, 2, 512, 16, True, "adamw")
    assert not me.fits_in(S06, 12288, 16, 1024, 16, True, "adamw8bit")


def test_invariants():
    base = dict(batch=2, seq=512, rank=16, checkpointing=False, optimizer="adamw")
    assert me.estimate_mib(S06, **{**base, "checkpointing": True}) < me.estimate_mib(S06, **base)
    assert me.estimate_mib(S06, **{**base, "optimizer": "adamw8bit"}) < me.estimate_mib(S06, **base)
    assert me.estimate_mib(S06, **{**base, "batch": 4}) > me.estimate_mib(S06, **base)
    assert me.estimate_mib(S06, **{**base, "rank": 64}) > me.estimate_mib(S06, **base)
    assert me.estimate_mib(S06, **{**base, "batch": 1, "seq": 1024}) == pytest.approx(me.estimate_mib(S06, **{**base, "batch": 2, "seq": 512}))
    with pytest.raises(ValueError):
        me.estimate_mib(S06, 1, 128, 8, True, "sgd")
    with pytest.raises(ValueError):
        me.estimate_mib(S06, 0, 128, 8, True, "adamw")
