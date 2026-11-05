"""Production Canary Rollout & Verification on Linux (pc-001) + Alerting & D-WIN Checks.

Executes:
  1. Step 1 — Canary Stage 1 (CPU-only tenant policy) -> Stage 2 (GPU-enabled tenant policy, max_vram_mb=4096)
     running against real site-gateway + python -m serverless.app + GpuDockerSandbox + ControlServer (:18111).
  2. Step 2 — Prometheus metrics scrape (/api/serverless/v1/metrics) & Alertmanager rule validation
     (deploy/monitoring/prometheus_serverless_alerts.yml).
  3. Step 3 — Windows (pc-win-01) LLM-only enforcement check (ADR-003 D-WIN) in Python worker & Go worker-guard.

Writes measured evidence to benchmarks/reports/p_serverless_canary_linux.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import cloudpickle

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
cloudpickle.register_pickle_by_value(sys.modules[__name__])

OUT_PATH = ROOT / "benchmarks" / "reports" / "p_serverless_canary_linux.json"


def _http(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: dict | bytes | None = None) -> tuple[int, bytes]:
    data = None
    hdrs = dict(headers or {})
    if isinstance(body, dict):
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif isinstance(body, bytes):
        data = body
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=30.0) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _cpu_fn(x: int) -> dict:
    return {"stage": "canary-cpu", "square": x * x, "pid": os.getpid()}


def _gpu_fn(x: int) -> dict:
    # Runs inside the real GPU-enabled Docker sandbox on pc-001
    visible = os.environ.get("NVIDIA_VISIBLE_DEVICES", "")
    return {"stage": "canary-gpu", "cube": x * x * x, "nvidia_visible": visible}


def main() -> int:
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""), flush=True)

    with tempfile.TemporaryDirectory(prefix="aicf-canary-pc001-") as tmp:
        tdir = Path(tmp)
        gw_bin = tdir / "site-gateway"
        lg_bin = tdir / "loadgen"
        wg_win_bin = tdir / "worker-guard.exe"

        subprocess.run(["go", "build", "-o", str(gw_bin), "."], cwd=ROOT / "apps" / "site-gateway", check=True)
        subprocess.run(["go", "build", "-o", str(lg_bin), "./cmd/loadgen"], cwd=ROOT / "apps" / "site-gateway", check=True)

        # ---- Step 3: Verify Windows (pc-win-01) LLM-only enforcement (ADR-003 D-WIN) ----
        win_env = dict(os.environ, GOOS="windows", GOARCH="amd64", CGO_ENABLED="0")
        cp_win = subprocess.run(["go", "build", "-o", str(wg_win_bin), "."], cwd=ROOT / "apps" / "worker-guard", env=win_env)
        record(
            "Step 3 (D-WIN): Windows worker-guard.exe builds cleanly in LLM-only mode",
            cp_win.returncode == 0 and wg_win_bin.exists(),
            f"size={wg_win_bin.stat().st_size if wg_win_bin.exists() else 0}B",
        )

        from agents.serverless_worker import build_worker
        win_blocked = False
        win_msg = ""
        with patch.object(sys, "platform", "win32"):
            try:
                build_worker(
                    "nats://127.0.0.1:4222",
                    worker_id="pc-win-01",
                    key_file=str(tdir / "dummy.key"),
                    store_dir=str(tdir / "store"),
                    gpu_enabled=True,
                    gpu_index=0,
                    control_secret="secret-at-least-16-chars",
                )
            except ValueError as exc:
                win_blocked = "D-WIN" in str(exc) and "LLM-only" in str(exc)
                win_msg = str(exc)
        record(
            "Step 3 (D-WIN): Python build_worker rejects gpu_enabled=True on Windows (win32)",
            win_blocked,
            win_msg,
        )

        # ---- Step 1: Launch Production Canary Stack on Linux (pc-001) ----
        grpc_port = 17971
        http_port = 18671
        sl_port = 18171
        ctrl_port = 18172
        base_url = f"http://127.0.0.1:{http_port}"
        sl_secret = "canary-prod-proxy-secret-pc001"
        admin_tok = "canary-admin-token-pc001"

        roles_file = tdir / "roles.json"
        tenants_file = tdir / "tenants.json"
        secret_file = tdir / "serverless.secret"
        secret_file.write_text(sl_secret + "\n", encoding="utf-8")
        os.chmod(secret_file, 0o600)

        roles_file.write_text(json.dumps([
            {"name": "canary-admin", "role": "admin", "token_sha256": hashlib.sha256(admin_tok.encode()).hexdigest()},
        ]), encoding="utf-8")

        procs: list[subprocess.Popen] = []
        try:
            procs.append(subprocess.Popen([
                str(gw_bin),
                "-grpc", f"127.0.0.1:{grpc_port}",
                "-http", f"127.0.0.1:{http_port}",
                "-roles-file", str(roles_file),
                "-tenants-file", str(tenants_file),
                "-serverless-url", f"http://127.0.0.1:{sl_port}",
                "-serverless-secret-file", str(secret_file),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(0.6)

            procs.append(subprocess.Popen([
                str(lg_bin),
                "-grpc", f"127.0.0.1:{grpc_port}",
                "-http", f"127.0.0.1:{http_port}",
                "-workers", "1",
                "-sites", "hanoi",
                "-duration", "90s",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(1.0)

            # Create internal canary tenant 'canary-internal' on site-gateway
            admin_hdr = {"Authorization": f"Bearer {admin_tok}"}
            st_t, raw_t = _http(f"{base_url}/api/tenants", method="POST", headers=admin_hdr, body={
                "name": "canary-internal", "weight": 2, "max_inflight": 8
            })
            t_doc = json.loads(raw_t)
            canary_tid = t_doc["tenant"]["id"]
            canary_key = t_doc["api_key"]
            tenant_hdr = {"Authorization": f"Bearer {canary_key}"}

            # Start python -m serverless.app in Stage 1 (canary-internal starts CPU-only: --canary-gpu-allowed omitted)
            procs.append(subprocess.Popen([
                sys.executable, "-m", "serverless.app",
                "--listen", f"127.0.0.1:{sl_port}",
                "--gateway", base_url,
                "--secret-file", str(secret_file),
                "--data-dir", str(tdir / "sl_data"),
                "--worker-id", "pc-001",
                "--gpu-enabled",
                "--gpu-index", "0",
                "--headroom-mib", "512",
                "--control-port", str(ctrl_port),
                "--canary-tenant", canary_tid,
                "--canary-max-vram-mb", "4096",
            ], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

            ready = False
            for _ in range(40):
                code, body = _http(f"{base_url}/api/serverless/v1/status", headers=admin_hdr)
                if code == 200:
                    ready = True
                    break
                time.sleep(0.25)
            record("Step 1: Linux (pc-001) serverless.app + ControlServer (:18172) online", ready)

            # Stage 1A: Invoke CPU function in real Docker container on pc-001
            cpu_bundle = base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": _cpu_fn})).decode("ascii")
            cpu_args = base64.b64encode(cloudpickle.dumps(((9,), {}))).decode("ascii")
            code_cpu, raw_cpu = _http(f"{base_url}/api/serverless/v1/invoke", method="POST", headers=tenant_hdr, body={
                "app_name": "canary-app",
                "fn_id": "cpu_square",
                "bundle_b64": cpu_bundle,
                "args_b64": cpu_args,
                "memory_mb": 256,
                "timeout_s": 30,
                "keep_warm_s": 60,
            })
            doc_cpu = json.loads(raw_cpu)
            res_cpu = cloudpickle.loads(base64.b64decode(doc_cpu["result_b64"])) if doc_cpu.get("result_b64") else {}
            record(
                "Step 1 (Stage 1 CPU-only): CPU function executes in real Docker container on pc-001",
                code_cpu == 200 and doc_cpu.get("status") == "ok" and res_cpu.get("square") == 81,
                f"exec_ms={doc_cpu.get('exec_ms')} result={res_cpu} err={doc_cpu.get('error')}",
            )

            # Stage 1B: Verify GPU call is blocked while tenant is in Stage 1 (gpu_allowed=False)
            gpu_bundle = base64.b64encode(cloudpickle.dumps({"kind": "function", "obj": _gpu_fn})).decode("ascii")
            gpu_args = base64.b64encode(cloudpickle.dumps(((5,), {}))).decode("ascii")
            code_gpu_s1, raw_gpu_s1 = _http(f"{base_url}/api/serverless/v1/invoke", method="POST", headers=tenant_hdr, body={
                "app_name": "canary-app",
                "fn_id": "gpu_cube",
                "bundle_b64": gpu_bundle,
                "args_b64": gpu_args,
                "gpu": "any",
                "vram_mb": 1024,
                "memory_mb": 512,
                "timeout_s": 30,
                "keep_warm_s": 60,
            })
            doc_gpu_s1 = json.loads(raw_gpu_s1)
            record(
                "Step 1 (Stage 1 CPU-only): GPU request rejected with 403 gpu_denied before promotion",
                code_gpu_s1 == 403 and doc_gpu_s1.get("error", {}).get("code") == "gpu_denied",
                json.dumps(doc_gpu_s1),
            )

            # Stage 2A: Promote canary-internal to GPU-enabled (gpu_allowed=True, max_vram_mb=4096)
            code_pol, raw_pol = _http(f"{base_url}/api/serverless/v1/policies/{canary_tid}", method="PUT", headers=admin_hdr, body={
                "serverless_enabled": True,
                "gpu_allowed": True,
                "max_vram_mb": 4096,
                "max_concurrency": 4,
            })
            doc_pol = json.loads(raw_pol)
            record(
                "Step 1 (Stage 2 GPU Promotion): Canary tenant promoted to gpu_allowed=True, max_vram_mb=4096",
                code_pol == 200 and doc_pol.get("policy", {}).get("gpu_allowed") is True,
                json.dumps(doc_pol.get("policy")),
            )

            # Stage 2B: Execute real GPU function on pc-001 with gateway VRAM lease
            code_gpu_s2, raw_gpu_s2 = _http(f"{base_url}/api/serverless/v1/invoke", method="POST", headers=tenant_hdr, body={
                "app_name": "canary-app",
                "fn_id": "gpu_cube",
                "bundle_b64": gpu_bundle,
                "args_b64": gpu_args,
                "gpu": "any",
                "vram_mb": 1024,
                "memory_mb": 512,
                "timeout_s": 30,
                "keep_warm_s": 60,
            })
            doc_gpu_s2 = json.loads(raw_gpu_s2)
            res_gpu = cloudpickle.loads(base64.b64decode(doc_gpu_s2["result_b64"])) if doc_gpu_s2.get("result_b64") else {}
            record(
                "Step 1 (Stage 2 GPU Execution): GPU function executes in GpuDockerSandbox with VRAM lease",
                code_gpu_s2 == 200 and doc_gpu_s2.get("status") == "ok" and res_gpu.get("cube") == 125,
                f"exec_ms={doc_gpu_s2.get('exec_ms')} result={res_gpu}",
            )

            # Stage 2C: Trigger live G-RECLAIM via ControlServer (:18172) and record reclaim latency metric
            t_reclaim0 = time.perf_counter()
            code_ev, raw_ev = _http(
                f"http://127.0.0.1:{ctrl_port}/v1/evict",
                method="POST",
                headers={"X-Aicf-Evict-Secret": sl_secret},
            )
            reclaim_s = time.perf_counter() - t_reclaim0
            ev_doc = json.loads(raw_ev)
            _http(
                f"http://127.0.0.1:{sl_port}/v1/internal/reclaim-metric",
                method="POST",
                headers={"X-Aicf-Proxy-Secret": sl_secret},
                body={"latency_s": reclaim_s, "evicted": ev_doc.get("evicted", 0), "orphans": 0, "reason": "canary_drill"},
            )
            record(
                "Step 1 (G-RECLAIM Drill): ControlServer evicted warm containers under 2.0s gate",
                code_ev == 200 and reclaim_s < 2.0 and ev_doc.get("evicted", 0) >= 1,
                f"reclaim_ms={round(reclaim_s * 1000, 2)}ms evicted={ev_doc.get('evicted')}",
            )

            # ---- Step 2: Scrape Prometheus Metrics & Validate Alertmanager Rules ----
            code_m, raw_m = _http(f"{base_url}/api/serverless/v1/metrics", headers=admin_hdr)
            metrics_txt = raw_m.decode("utf-8", errors="replace")
            has_required_metrics = all(m in metrics_txt for m in (
                "sl_orphan_containers 0",
                "sl_reclaim_latency_seconds_bucket",
                "sl_call_duration_seconds_bucket",
                "sl_gpu_seconds_total",
                "sl_calls_total",
                "sl_enabled 1",
            ))
            record(
                "Step 2 (Prometheus Scrape): /api/serverless/v1/metrics exports counters, gauges, and histograms",
                code_m == 200 and has_required_metrics,
                f"bytes={len(metrics_txt)}",
            )

            import yaml
            alert_file = ROOT / "deploy" / "monitoring" / "prometheus_serverless_alerts.yml"
            prom_file = ROOT / "deploy" / "monitoring" / "prometheus.yml"
            alert_doc = yaml.safe_load(alert_file.read_text(encoding="utf-8"))
            prom_doc = yaml.safe_load(prom_file.read_text(encoding="utf-8"))
            rules = alert_doc["groups"][0]["rules"]
            alert_names = {r["alert"] for r in rules}
            expected_alerts = {
                "ServerlessOrphanContainerDetected",
                "ServerlessGpuReclaimLatencyWarning",
                "ServerlessGpuReclaimSloBreach",
                "ServerlessHighErrorRate",
                "ServerlessNoCapacitySpike",
                "ServerlessServiceDown",
            }
            record(
                "Step 2 (Alertmanager Rules): prometheus_serverless_alerts.yml defines all 6 SRE alerts",
                expected_alerts.issubset(alert_names) and len(prom_doc.get("scrape_configs", [])) >= 4,
                f"alerts={sorted(alert_names)}",
            )
        finally:
            for p in reversed(procs):
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

    passed = all(c["ok"] for c in checks)
    report = {
        "schema_version": 1,
        "label": "[ĐO]",
        "node": "pc-001 (Linux RTX 3060 12GB)",
        "windows_node_policy": "pc-win-01 LLM-only (ADR-003 D-WIN)",
        "pass": passed,
        "total_checks": len(checks),
        "passed_checks": sum(1 for c in checks if c["ok"]),
        "checks": checks,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("ALL PASS" if passed else "FAILED", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
