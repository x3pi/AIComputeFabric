"""Phase 5 mutations: Volume tenant isolation, path traversal, symlink escape, volume quota, ModelCache read-only for tenants, web endpoint JSON-only & auth, Go RBAC on /api/serverless/*, and Go-compatible UsageLedger hash chain."""
from tests.mutation.harness import Mutation

REPORT = "p_serverless_5_mutations.json"
PY = ".venv/bin/python"
PT = [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
P5_TEST = PT + ["tests/serverless/test_p5_http_volumes_metering.py"]
GO_SL = ["go", "test", "-C", "apps/site-gateway", "-count=1", "-run", "TestServerless", "."]

VOL = "serverless/volumes.py"
SVC = "serverless/service.py"
APP_HTTP = "serverless/app.py"
LEDGER = "serverless/ledger.py"
RBAC = "apps/site-gateway/rbac.go"

MUTATIONS = [
    Mutation("P5-M01 VolumeStore path traversal (..) check disabled", VOL,
             'if rel_path.startswith("/") or ".." in raw_parts:\n            self._deny(tenant, vol, rel_path, "relative path without \'..\' is required")',
             'if False:\n            self._deny(tenant, vol, rel_path, "relative path without \'..\' is required")',
             P5_TEST + ["-k", "test_volume_tenant_isolation_traversal_symlink_and_quota"],
             "VolumeStore must reject relative parent traversal (D7)"),
    Mutation("P5-M02 VolumeStore symlink escape check disabled", VOL,
             "    def _check_no_symlinks(self, tenant: str, vol: str, rel_path: str, base: Path, target: Path) -> None:\n        cur = base",
             "    def _check_no_symlinks(self, tenant: str, vol: str, rel_path: str, base: Path, target: Path) -> None:\n        return\n        cur = base",
             P5_TEST + ["-k", "test_volume_tenant_isolation_traversal_symlink_and_quota"],
             "VolumeStore must reject symlinks on every path component (D7)"),
    Mutation("P5-M03 VolumeStore per-tenant max_volume_bytes quota disabled", VOL,
             "if used - old_size + len(raw) > max_volume_bytes:",
             "if False:",
             P5_TEST + ["-k", "test_volume_tenant_isolation_traversal_symlink_and_quota"],
             "VolumeStore must enforce max_volume_bytes per tenant"),
    Mutation("P5-M04 ModelCacheStore allows non-admin tenants to write", VOL,
             'if role != "admin":',
             "if False:",
             P5_TEST + ["-k", "test_model_cache_readonly_for_tenants_and_writable_by_admin"],
             "ModelCache must be read-only for tenants and writable only by admin (D7)"),
    Mutation("P5-M05 Web endpoint tenant authentication check disabled", SVC,
             'if caller_role != "tenant" or caller_tenant != target_tenant:',
             "if False:",
             P5_TEST + ["-k", "test_deploy_v1_v2_rollback_web_endpoint_and_inflight_not_interrupted"],
             "web endpoints with requires_auth=True must enforce matching tenant API key"),
    Mutation("P5-M06 Web endpoint JSON-only media type check disabled (T10)", APP_HTTP,
             'if "application/json" not in ctype.lower():',
             "if False:",
             P5_TEST + ["-k", "test_deploy_v1_v2_rollback_web_endpoint_and_inflight_not_interrupted"],
             "web endpoints must reject non-JSON payloads with 415 (T10)"),
    Mutation("P5-M07 Gateway RBAC allows viewer/operator arbitrary writes on /api/serverless/", RBAC,
             '\tif p.Role == RoleOperator && ((r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/api/serverless/v1/containers/")) ||\n\t\t(r.Method == http.MethodPost && r.URL.Path == "/api/serverless/v1/admin/state")) {',
             '\tif strings.HasPrefix(r.URL.Path, "/api/serverless/") {\n\t\treturn true\n\t}\n\tif false {',
             GO_SL,
             "viewer must remain GET-only and operator may only evict containers or toggle drain"),
    Mutation("P5-M08 UsageLedger SHA-256 hash chain broken", LEDGER,
             "return hashlib.sha256(raw).hexdigest()",
             'return "0" * 64',
             P5_TEST + ["-k", "test_metering_1000_calls_plus_idle_leases_reconciliation_and_go_verify_ledger"],
             "UsageLedger hash chain must be verified by Go gcp.VerifyLedgerFile"),
]
