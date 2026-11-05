"""Phase 4 mutations: @app.cls enter-once invariant, Image requirements hash & local file size bounds, .map() backpressure window, Secret sanitization in audit log, and FunctionCall tenant isolation."""
from tests.mutation.harness import Mutation

REPORT = "p_serverless_4_mutations.json"
PY = ".venv/bin/python"
PT = [PY, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]

RUNNER = "agents/_warm_runner.py"
IMAGE = "sdk/python/aicf/image.py"
APP = "sdk/python/aicf/app.py"
LEDGER = "serverless/ledger.py"
SVC = "serverless/service.py"

MUTATIONS = [
    Mutation("P4-M01 @app.cls re-runs @aicf.enter() on every call instead of once per container", RUNNER,
             "    if sha in _LOADED:\n        return _LOADED[sha], False, 0.0",
             "    if False:\n        return _LOADED[sha], False, 0.0",
             PT + ["tests/serverless/test_runner_unit.py", "-k", "test_class_enter_runs_once_and_state_persists"],
             "@aicf.enter() must execute exactly once per warm container"),
    Mutation("P4-M02 Image.pip_install_from_requirements accepts unhashed requirements", IMAGE,
             "if require_hashes and not _HASH_RE.search(entry):",
             "if False:",
             PT + ["tests/serverless/test_p4_sdk_cli.py", "-k", "test_image_local_files_and_requirements_hash_enforcement"],
             "pip_install_from_requirements must require --hash=sha256: by default (D4/T9)"),
    Mutation("P4-M03 Image.add_local_file bypasses MAX_LOCAL_FILES_BYTES limit", IMAGE,
             "if total > MAX_LOCAL_FILES_BYTES:",
             "if False:",
             PT + ["tests/serverless/test_p4_sdk_cli.py", "-k", "test_image_local_files_and_requirements_hash_enforcement"],
             "local file bundling must enforce MAX_LOCAL_FILES_BYTES"),
    Mutation("P4-M04 .map() backpressure window unbounded (loads all items at once)", APP,
             "window = max(1, max_concurrency * 2)",
             "window = 1000000",
             PT + ["tests/serverless/test_p4_sdk_cli.py", "-k", "test_map_10000_backpressure_keeps_memory_flat"],
             ".map() must bound in-flight futures by 2 * max_concurrency"),
    Mutation("P4-M05 ServerlessAudit sanitizer disabled (leaks secrets/env_vars)", LEDGER,
             'if k in ("env_vars", "secret", "secrets", "args", "args_b64", "bundle_b64", "result_b64", "api_key"):',
             "if False:",
             PT + ["tests/serverless/test_p4_sdk_cli.py", "-k", "test_secret_injected_into_container_and_never_logged_or_audited"],
             "audit log must never persist secret values, env_vars, args, or bundles"),
    Mutation("P4-M06 FunctionCall status/logs cross-tenant isolation check disabled", SVC,
             'if role == "tenant" and rec["tenant_id"] != tenant:\n            raise ServiceError(403, "forbidden", "cannot access another tenant\'s call")',
             'if False:\n            raise ServiceError(403, "forbidden", "cannot access another tenant\'s call")',
             PT + ["tests/serverless/test_p4_sdk_cli.py", "-k", "test_spawn_function_call_logs_and_cancel"],
             "a tenant must never read or cancel another tenant's spawned call_id"),
]
