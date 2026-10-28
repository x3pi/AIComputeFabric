#!/usr/bin/env python3
"""Builds benchmarks/reports/p3_three_modes_report.{json,md} from the raw run files in benchmarks/reports/logs/.
Nothing is typed in by hand: every number comes from a p3_mode*_*.json / p3_server_cfg.jsonl / p3_gpu_modes_sweep.json."""
import json
from pathlib import Path

R = Path(__file__).resolve().parents[2] / "benchmarks/reports"
L = R / "logs"


def load(name):
    return json.loads((L / name).read_text())


def url_stats(d, port):
    return next(v for u, v in d["urls"].items() if u.endswith(":" + port))


def mode1(tag):
    out = {}
    for sc in ("linux", "windows", "both"):
        for c in (1, 4):
            d = load(f"p3_mode1_{tag}_{sc}_c{c}.json")
            out[f"{sc}_c{c}"] = {"aggregate_tok_s": d["aggregate_tok_s"],
                                 "linux": (lambda v: {"tok_s": v["own_aggregate_tok_s"], "per_stream": v["per_stream_tok_s_median"], "failed": v["failed"]})(url_stats(d, "18091")) if sc != "windows" else None,
                                 "windows": (lambda v: {"tok_s": v["own_aggregate_tok_s"], "per_stream": v["per_stream_tok_s_median"], "failed": v["failed"]})(url_stats(d, "18090")) if sc != "linux" else None}
    out["vram"] = load(f"p3_mode1_{tag}_vram.json")
    return out


def mode2():
    sweep = json.loads((R / "p3_gpu_modes_sweep.json").read_text())
    conc = {}
    for c in (1, 2):
        d = load(f"p3_mode2_linux_N20_c{c}.json")
        v = next(iter(d["urls"].values()))
        conc[c] = {"aggregate_tok_s": d["aggregate_tok_s"], "per_stream": v["per_stream_tok_s_median"], "failed": v["failed"]}
    return {"sweep_server": sweep["llama_server_realistic_ctx"], "sweep_bench": sweep["llama_bench_small_ctx"], "np2_ctx8192_N20": conc}


def mode3():
    out = {}
    for tag in ("4b_cold", "4b_warm", "4b_split46"):
        meta = load(f"p3_mode3_{tag}_meta.json")
        row = {"load_s": meta["load_s"], "tensor_split": meta["tensor_split"]}
        for c in (1, 2):
            d = load(f"p3_mode3_{tag}_c{c}.json")
            v = next(iter(d["urls"].values()))
            row[f"c{c}"] = {"aggregate_tok_s": d["aggregate_tok_s"], "per_stream": v["per_stream_tok_s_median"],
                            "min_max": v["per_stream_tok_s_min_max"], "failed": v["failed"]}
        out[tag] = row
    cable = {}
    for name in ("p3_merged4b_cable_c1.json", "p3_merged4b_cable_c4.json",
                 "p3_merged30b_cable_c1.json", "p3_merged30b_cable_c2.json", "p3_merged30b_cable_c4.json"):
        if (L / name).exists():
            d = load(name)
            v = next(iter(d["urls"].values()))
            cable[d["label"]] = {"concurrency": d["concurrency_per_url"], "aggregate_tok_s": d["aggregate_tok_s"],
                                 "per_stream": v["per_stream_tok_s_median"], "min_max": v["per_stream_tok_s_min_max"],
                                 "failed": v["failed"]}
    if cable:
        out["cable_2p5gbe_2026_10_28"] = cable
    return out


def main():
    rep = {"title": "Bao cao do 3 che do cum 2 GPU", "date": "2026-10-28", "mode1": {"Qwen3-4B-Q4_K_M": mode1("4b"), "Qwen3-8B-Q4_K_M": mode1("8b")},
           "mode2": mode2(), "mode3": mode3()}
    (R / "p3_three_modes_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    print("wrote json")


if __name__ == "__main__":
    main()
