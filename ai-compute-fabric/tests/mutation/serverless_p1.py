"""Phase 1 mutations: sandbox hardening, tenant isolation, requirement validation, runner channel isolation."""
from tests.mutation.harness import Mutation

REPORT = "p_serverless_1_mutations.json"
PY = ".venv/bin/python"
ALL = [PY, "-m", "pytest", "tests/serverless", "-q", "-x", "-p", "no:cacheprovider"]
RUNNER = [PY, "-m", "pytest", "tests/serverless/test_runner_unit.py", "-q", "-x", "-p", "no:cacheprovider"]
S, E, R = "agents/sandbox.py", "agents/env_cache.py", "agents/_warm_runner.py"
NET = '"--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", str(int(limits.pids))'

MUTATIONS = [
    Mutation("P1-M01 warm key ignores tenant", S, "class WarmKey:\n    tenant_id: str", "class WarmKey:\n    tenant_id: str = field(compare=False)", ALL,
             "two tenants must never share a container (T4)"),
    Mutation("P1-M02 no --network none", S, NET, NET.replace('"--network", "none", ', ""), ALL, "sandbox must have no network (T1)"),
    Mutation("P1-M03 no --cap-drop ALL", S, '"--cap-drop", "ALL", "--pids-limit"', '"--pids-limit"', ALL, "capabilities dropped (T1)"),
    Mutation("P1-M04 no --read-only", S, '"--read-only", "--cap-drop", "ALL", "--pids-limit"', '"--cap-drop", "ALL", "--pids-limit"', ALL, "read-only root (T1)"),
    Mutation("P1-M05 run as root", S, '"--cpus", str(float(limits.cpus)), "--user", "65534:65534",', '"--cpus", str(float(limits.cpus)),', ALL, "non-root user (T1)"),
    Mutation("P1-M06 no memory limit", S, '"--memory", mem, "--memory-swap", mem, ', "", ALL, "memory_mb enforced (T6)"),
    Mutation("P1-M07 no pids limit", S, '"--pids-limit", str(int(limits.pids)),\n                "--memory"', '"--memory"', ALL, "fork bomb limited (T6)"),
    Mutation("P1-M08 timeout does not kill", S, "            self.kill()\n            raise SandboxError(f\"timed out after", "            raise SandboxError(f\"timed out after", ALL,
             "timed-out container must die, no orphan"),
    Mutation("P1-M09 reuse across different shapes", S, "c.limits.shape() == limits.shape() and c.lock", "c.lock", ALL, "GPU container never reused for a CPU shape"),
    Mutation("P1-M10 env image check removed", S, "        self._check_env_image(env_dir)\n", "", ALL, "env/runtime ABI must match"),
    Mutation("P1-M11 env deny list emptied", S, 'ENV_DENY_PREFIXES = ("NVIDIA_", "AICF_", "LD_", "PYTHON", "DOCKER_", "HOME", "PATH", "TMP", "CUDA_VISIBLE")',
             "ENV_DENY_PREFIXES = ()", ALL, "tenant cannot set NVIDIA_VISIBLE_DEVICES etc. (T1/T8)"),
    Mutation("P1-M12 requirement validation off", E, "if not isinstance(req, str) or not _REQ.match(req):", "if False:", ALL, "no URLs/paths/options in requirements (T9)"),
    Mutation("P1-M13 sdist allowed in builder", E, "-q --generate-hashes --only-binary :all: --python", "-q --generate-hashes --python", ALL, "wheels only (D4)"),
    Mutation("P1-M14 hashes not required", E, "--only-binary :all: --require-hashes -r", "--only-binary :all: -r", ALL, "hash pinned install (D4)"),
    Mutation("P1-M15 disk guard off", E, "if free < self.min_free_bytes:", "if False:", ALL, "do not build on a full disk"),
    Mutation("P1-M16 runner fd1 not redirected", R, "os.dup2(_null_w, 1)\n", "", RUNNER, "user C-level writes must not corrupt the channel"),
    Mutation("P1-M17 runner fd0 not redirected", R, "os.dup2(_null_r, 0)\n", "", RUNNER, "user code must not read requests", ),
    Mutation("P1-M18 bundle sha not verified", R, "    if hashlib.sha256(raw).hexdigest() != sha:\n        raise ValueError(\"bundle sha256 mismatch\")\n", "", RUNNER, "bundle integrity"),
    Mutation("P1-M19 SystemExit kills runner", R, "        except BaseException as e:   # includes SystemExit", "        except Exception as e:   # includes SystemExit", RUNNER, "runner survives user SystemExit"),
    Mutation("P1-M20 output not capped", R, "            self.truncated = True\n        return len(s)", "        return len(s)\n        self.truncated = True", RUNNER, "stdout cap"),
]
