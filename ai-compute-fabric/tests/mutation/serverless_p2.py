"""Phase 2 mutations: signature/replay, tenant binding, policy, warm-pool isolation, at-most-once, gateway trust boundary."""
from tests.mutation.harness import Mutation

REPORT = "p_serverless_2_mutations.json"
PY = ".venv/bin/python"
PT = [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
UNIT = PT + ["tests/serverless/test_security_unit.py", "tests/serverless/test_service_unit.py", "tests/serverless/test_worker_unit.py",
             "tests/serverless/test_dispatch_unit.py"]
E2E = PT + ["tests/serverless/test_e2e.py"]
GO = ["go", "test", "-C", "apps/site-gateway", "-count=1", "-run", "Serverless", "."]
W, SEC, Q, SVC, APP, DSP = "agents/serverless_worker.py", "agents/sl_security.py", "serverless/quota.py", "serverless/service.py", "serverless/app.py", "serverless/dispatch.py"
TB, GSL, GAD = "agents/task_bus.py", "apps/site-gateway/serverless.go", "apps/site-gateway/admin.go"

MUTATIONS = [
    Mutation("P2-M01 signature not verified", W, "            self.verifier.verify(inp, attempt)\n        except SignatureError as e:",
             "            pass\n        except SignatureError as e:", UNIT, "forged/tampered bus messages must not run (T3)"),
    Mutation("P2-M02 envelope tenant not bound to signed tenant", W, "if inp.tenant_id != task.tenant_id or inp.call_id != task.task_id:", "if False:", UNIT,
             "envelope tenant must equal signed tenant (T3/T4)"),
    Mutation("P2-M03 worker data_class check off", W, "if task.data_class not in self.allowed_data_classes:", "if False:", UNIT, "RESTRICTED never on shared GPU (D6)"),
    Mutation("P2-M04 env_hash not recomputed", W, "if spec.env_hash() != inp.env_hash:", "if False:", UNIT, "env hash must match spec (cache poisoning, T9)"),
    Mutation("P2-M05 GPU gate off in worker", W, "if want_gpu and not (self.gpu_enabled and self.gpu_index is not None):", "if False:", UNIT, "GPU stays behind its flag"),
    Mutation("P2-M06 bundle hash not checked", W, "if sha256_hex(base64.b64decode(b64)) != inp.bundle_sha256:", "if False:", UNIT, "bundle integrity (T3)"),
    Mutation("P2-M07 started ledger bypassed", W, "before_run=lambda: self.ledger.mark_started(inp.call_id, attempt))", "before_run=None)", UNIT,
             "at-most-once after start (D8)"),
    Mutation("P2-M08 nonce replay allowed", SEC, "if not self.nonces.check_and_add(inp.sig.nonce, inp.sig.exp, now):", "if False:", UNIT, "replay protection (T3)"),
    Mutation("P2-M09 expiry not enforced", SEC, "if now > inp.sig.exp + self.skew:", "if False:", UNIT, "expired calls refused (T3)"),
    Mutation("P2-M10 env_vars not covered by MAC", SEC, 'SIGNED_EXCLUDE = {"sig", "bundle_b64", "bundle_ref", "args_b64", "args_ref"}',
             'SIGNED_EXCLUDE = {"sig", "bundle_b64", "bundle_ref", "args_b64", "args_ref", "env_vars"}', UNIT, "every execution-relevant field is signed"),
    Mutation("P2-M11 attempt not covered by MAC", SEC, '    d["_attempt"] = attempt\n', "", UNIT, "attempt bound into signature"),
    Mutation("P2-M12 args not covered by MAC", SEC, '    d["_args_sha256"] = _data_sha(inp.args_b64, inp.args_ref)\n', "", UNIT, "args bytes bound into signature"),
    Mutation("P2-M13 constant-time compare replaced by accept-any", SEC, "ok |= hmac.compare_digest(hmac.new(k, body, hashlib.sha256).hexdigest(), inp.sig.mac)",
             "ok = True", UNIT, "MAC must be checked"),
    Mutation("P2-M14 RESTRICTED allowed by policy", Q, ' or req["data_class"] == "RESTRICTED"', "", UNIT, "RESTRICTED never runs on shared GPU (D6)"),
    Mutation("P2-M15 tenants enabled by default", Q, "serverless_enabled: bool = False", "serverless_enabled: bool = True", UNIT, "default deny"),
    Mutation("P2-M16 GPU policy off", Q, "if wants_gpu and not p.gpu_allowed:", "if False:", UNIT, "GPU needs per-tenant opt-in"),
    Mutation("P2-M17 concurrency limit off", Q, "if n >= p.max_concurrency:", "if False:", UNIT, "per-tenant concurrency"),
    Mutation("P2-M18 resource limits off", Q, "if req.get(field_, 0) > limit:", "if False:", UNIT, "per-tenant resource ceilings"),
    Mutation("P2-M19 warm key ignores tenant", SVC, "return (tenant, env_hash, fn_id,", 'return ("", env_hash, fn_id,', UNIT, "no cross-tenant warm reuse (T4)"),
    Mutation("P2-M20 user errors retried", SVC, 'INFRA_FAILURES = {"worker_lost", "duplicate", "evicted"}', 'INFRA_FAILURES = {"worker_lost", "duplicate", "evicted", "error", "timeout"}', UNIT,
             "only infrastructure loss is retried (D8)"),
    Mutation("P2-M21 gateway secret not checked", APP, "return hmac.compare_digest(got, secret_b)", "return True", E2E, "service trusts only the gateway (D2)"),
    Mutation("P2-M22 role check dropped", APP, 'if self.headers.get("X-Aicf-Role") != "tenant" or not tenant:', "if not tenant:", E2E, "admin token cannot invoke"),
    Mutation("P2-M23 direct-path timeout falls back to queue", DSP, 'raise WorkerLost(f"{task.task_id}: no reply from {task.label} in {timeout:.0f}s") from None',
             "return None", UNIT, "a possibly-started call is never re-run silently (D8)"),
    Mutation("P2-M24 generic workers pull serverless", TB, "DEFAULT_TASK_TYPES = [t for t in TaskType if t != TaskType.SERVERLESS_EXEC]", "DEFAULT_TASK_TYPES = list(TaskType)", E2E,
             "SERVERLESS_EXEC is opt-in only"),
    Mutation("P2-M25 serverless shares the legacy subject root", TB, "    return SERVERLESS_SUBJECT if _norm_type(task_type) == TaskType.SERVERLESS_EXEC.value else SUBJECT",
             "    return SUBJECT", E2E, "separate subject root keeps legacy wildcard workers out"),
    Mutation("P2-M26 proxy secret not set (go)", GSL, '			pr.Out.Header.Set("X-Aicf-Proxy-Secret", secret)\n', "", GO, "gateway authenticates itself to the service"),
    Mutation("P2-M27 client identity headers survive (go)", GAD, '		r.Header.Del("X-Aicf-Tenant")\n		r.Header.Del("X-Aicf-Proxy-Secret")\n', "", GO,
             "client cannot choose its tenant (D2)"),
    Mutation("P2-M28 serverless routes unauthenticated path (go)", GAD, ' || strings.HasPrefix(r.URL.Path, serverlessPrefix)', "", GO, "/api/serverless always authenticated"),
    Mutation("P2-M29 raw credential forwarded (go)", GSL, '			pr.Out.Header.Del("Authorization") // never forward raw credentials downstream\n', "", GO, "API key never leaves the gateway"),
    Mutation("P2-M30 body limit off (go)", GSL, "		if r.ContentLength > maxBody {", "		if false {", GO, "bounded request size"),
    Mutation("P2-M31 short shared secret accepted (go)", GSL, "	if len(secret) < 16 {\n		return nil, fmt.Errorf(\"--serverless-secret-file is required", "	if false {\n		return nil, fmt.Errorf(\"--serverless-secret-file is required", GO,
             "weak shared secret refused"),
]
