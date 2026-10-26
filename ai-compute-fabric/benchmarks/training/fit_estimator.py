"""Fits the 3 constants of training/memory_estimator.py on the 'calib' rows and reports error on held-out and 1.7B rows.
usage: fit_estimator.py RAW.jsonl REPORT.json"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from training import memory_estimator as me  # noqa: E402

rows = [json.loads(l) for l in open(sys.argv[1])]
specs = {m: me.ModelSpec.from_config(ROOT.parent / ".aicf-work/hf-train" / m / "config.json") for m in ("Qwen3-0.6B", "Qwen3-1.7B")}


def feats(r):
    m, b, s, rk, ck, op = r["_cfg"]
    sp = specs[m]
    base = (2 * sp.model_params() + sp.lora_params(rk) * (8 + me.OPT_BYTES_PER_PARAM[op])) / me.MIB
    t = b * s
    logit = t * sp.vocab / me.MIB
    act = (t * sp.layers * 2 * sp.hidden / me.MIB) if ck else 0.0
    nact = 0.0 if ck else t * sp.layers * (sp.hidden + sp.inter) / me.MIB
    return base, logit, act, nact


cal = [r for r in rows if r["set"] == "calib" and r["ok"]]
A = np.array([[1.0, feats(r)[1], feats(r)[3]] for r in cal])
y = np.array([r["peak_used_mib"] - feats(r)[0] - feats(r)[2] for r in cal])
(ctx, cl, ca), *_ = np.linalg.lstsq(A, y, rcond=None)
fit = {"ctx_mib": round(float(ctx), 1), "c_logits": round(float(cl), 4), "c_act": round(float(ca), 4)}
Path(me.__file__).with_name("memory_estimator_fit.json").write_text(json.dumps(fit, indent=1))


def evaluate(which):
    out = []
    for r in rows:
        if r["set"] != which:
            continue
        m, b, s, rk, ck, op = r["_cfg"]
        est = me.estimate_mib(specs[m], b, s, rk, ck, op, fit)
        out.append({"cfg": r["_cfg"], "measured_mib": r.get("peak_used_mib"), "estimated_mib": round(est), "oom": not r["ok"],
                    "err_pct": round(100 * (est - r["peak_used_mib"]) / r["peak_used_mib"], 1) if r["ok"] else None})
    return out


rep = {"fit": fit, "n_calib_fit": len(cal), "calib": evaluate("calib"), "heldout": evaluate("heldout"), "scale": evaluate("scale"),
       "gpu_total_mib": 12288}
for k in ("calib", "heldout", "scale"):
    e = [abs(x["err_pct"]) for x in rep[k] if x["err_pct"] is not None]
    signed = [x["err_pct"] for x in rep[k] if x["err_pct"] is not None]
    rep[k + "_summary"] = {"n": len(e), "mean_abs_err_pct": round(float(np.mean(e)), 1), "max_abs_err_pct": round(float(np.max(e)), 1),
                           "worst_underestimate_pct": round(float(min(signed)), 1)}
    # OOM rows: does the estimate (with 8% margin) predict "does not fit"?
    oom = [x for x in rep[k] if x["oom"]]
    rep[k + "_summary"]["oom_rows"] = len(oom)
    rep[k + "_summary"]["oom_predicted"] = sum(x["estimated_mib"] * 1.08 > 12288 for x in oom)
    ok = [x for x in rep[k] if not x["oom"]]
    rep[k + "_summary"]["fit_rows_predicted_to_fit"] = sum(x["estimated_mib"] * 1.08 <= 12288 for x in ok)
    rep[k + "_summary"]["fit_rows"] = len(ok)
Path(sys.argv[2]).write_text(json.dumps(rep, indent=1))
print(json.dumps(fit), *[(k, rep[k + "_summary"]) for k in ("calib", "heldout", "scale")], sep="\n")
for k in ("heldout", "scale"):
    for x in rep[k]:
        print(k, x)
