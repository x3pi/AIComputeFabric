"""Phase 3 mutations: GPU admission headroom/reservations, per-container VRAM limit, worker-guard D10 eviction order & docker kill fallback, orphan cleanup, and LeaseManager bridge."""
from tests.mutation.harness import Mutation

REPORT = "p_serverless_3_mutations.json"
PY = ".venv/bin/python"
PT = [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
UNIT_GPU = PT + ["tests/serverless/test_gpu_unit.py", "tests/serverless/test_service_unit.py"]
REAL_SIGKILL = PT + ["tests/serverless/test_gpu_real.py", "-k", "test_sigkill_of_the_worker_leaves_no_vram_behind"]
GO_GUARD = ["go", "test", "-C", "apps/worker-guard", "-count=1", "."]
GO_LEASE = ["go", "test", "-C", "apps/site-gateway", "-count=1", "-run", "TestLease", "."]

GPU, RUNNER, SVC = "agents/sl_gpu.py", "agents/_warm_runner.py", "serverless/service.py"
GUARD, EVICT, LEASE = "apps/worker-guard/guard.go", "apps/worker-guard/evict.go", "apps/site-gateway/leases.go"

MUTATIONS = [
    Mutation("P3-M01 admission headroom inequality off", GPU,
             "if left < self.headroom:",
             "if False:",
             UNIT_GPU, "VRAM admission must never dip below owner headroom (2.5 / ADR-003)"),
    Mutation("P3-M02 admission uncovered reservations ignored", GPU,
             "uncovered = sum(max(0, r - self._usage.get(n, 0)) for n, (g, r) in self._res.items() if g == gpu)",
             "uncovered = 0",
             UNIT_GPU, "concurrent cold starts must count unallocated reservations (T7)"),
    Mutation("P3-M03 GPU reservation allows vram_mb <= 0", GPU,
             "if vram_mib <= 0:",
             "if False:",
             UNIT_GPU, "GPU functions must declare positive VRAM reservation"),
    Mutation("P3-M04 per-container VRAM limit enforcement off", GPU,
             "if limit is not None and mib > limit + self.adm.tol:",
             "if False:",
             UNIT_GPU, "container exceeding declared vram_mb + tolerance must be killed (T7)"),
    Mutation("P3-M05 control server evict secret check off", GPU,
             'if hmac.compare_digest(self.headers.get("X-Aicf-Evict-Secret", "").encode(), outer.secret):',
             "if True:",
             UNIT_GPU, "local /v1/evict and /v1/status require shared secret"),
    Mutation("P3-M06 control server accepts short secret", GPU,
             "if len(secret) < 16:",
             "if False:",
             UNIT_GPU, "weak control secret refused"),
    Mutation("P3-M07 runner stdin watchdog disabled on worker SIGKILL", RUNNER,
             "threading.Thread(target=_watch_stdin, daemon=True).start()",
             "pass",
             REAL_SIGKILL, "SIGKILL of host worker during CUDA sleep must self-terminate container and free VRAM"),
    Mutation("P3-M08 worker-guard D10 eviction order bypassed (stops LLM first)", GUARD,
             '\t\t\tif g.evictServerless("free VRAM below headroom", s) { // D10: guests first, the engine only if that was not enough',
             '\t\t\tif false { // D10 bypassed',
             GO_GUARD, "Serverless guests must be evicted before stopping the LLM engine (D10)"),
    Mutation("P3-M09 worker-guard Pause skips Serverless eviction", GUARD,
             '\tif g.cfg.Evictor != nil {\n\t\tg.evictServerless("operator pause", g.last) // guests leave first; the engine\'s own stop below is unchanged\n\t}',
             '\tif false {\n\t\tg.evictServerless("operator pause", g.last)\n\t}',
             GO_GUARD, "operator Pause must evict Serverless guests first (D10)"),
    Mutation("P3-M10 worker-guard fallback docker kill disabled when /v1/evict fails", EVICT,
             "\t\tn, kerr = ev.Kill()",
             "\t\tn, kerr = 0, err",
             GO_GUARD, "when /v1/evict hangs or fails, guard must fall back to docker kill by label"),
    Mutation("P3-M11 worker-guard docker kill container ID regex check off", EVICT,
             "\t\tif !containerID.MatchString(id) {",
             "\t\tif false {",
             GO_GUARD, "docker kill fallback validates hex container IDs"),
    Mutation("P3-M12 LeaseHTTPHandler shared secret check off", LEASE,
             "\t\tif len(secretBytes) < 16 || !hmac.Equal(got, secretBytes) {",
             "\t\tif false {",
             GO_LEASE, "internal lease HTTP bridge requires X-Aicf-Proxy-Secret (D-LEASE-HTTP)"),
    Mutation("P3-M13 LeaseManager GPU constraint matching off", LEASE,
             "\t\tif w.State != \"running\" || !w.Online || w.Buckets == nil || (req.Model != \"\" && w.Model != req.Model) || !matchesJobConstraint(w, req.Job) {",
             "\t\tif w.State != \"running\" || !w.Online || w.Buckets == nil || (req.Model != \"\" && w.Model != req.Model) {",
             GO_LEASE, "GPU/worker constraint must filter candidate workers"),
    Mutation("P3-M14 cold GPU call skips LeaseManager request", SVC,
             "if pinned is None and need_label and self.leases is not None and lease_id is None:",
             "if False:",
             UNIT_GPU, "cold GPU path must acquire a container lease from LeaseManager (D3)"),
    Mutation("P3-M15 tick_leases renews lease even when worker heartbeat is gone", SVC,
             "if e.expires <= now or e.worker_id not in live_ids:",
             "if e.expires <= now:",
             UNIT_GPU, "leases must only be renewed while worker is alive in AICF_AGENTS"),
]

