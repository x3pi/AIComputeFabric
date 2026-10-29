"""Security tests for Pha E (E.2): Task Bus mTLS authentication, per-agent identity binding,
TaskType & data_class authorization, payload limits, task TTL, tenant quotas, and queue depth backpressure.

Satisfies Check Pass (c), (d), and (e)."""
from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agents.task_bus import (
    AgentGrant,
    BusAuthError,
    BusExecutor,
    BusPermissionError,
    BusSecurityPolicy,
    BusTLSConfig,
    BusWorker,
    QueueFullError,
    QuotaExceededError,
    TaskExpiredError,
    TaskTooLargeError,
    TenantQuota,
    generate_bus_pki,
)
from agents.tasks import Task, TaskResult, TaskType


def _find_nats_server() -> str | None:
    for candidate in (
        shutil.which("nats-server"),
        "/tmp/nats-server",
        str(Path(__file__).resolve().parents[3] / ".aicf-work/bin/nats-server"),
    ):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SpyExecutor:
    def __init__(self, worker_id: str, delay_s: float = 0.05):
        self.worker_id = worker_id
        self.delay_s = delay_s
        self.seen_tasks: list[str] = []

    async def execute_async(self, task: Task, attempt: int, deps: dict) -> TaskResult:
        self.seen_tasks.append(task.task_id)
        await asyncio.sleep(self.delay_s)
        if task.type == TaskType.OCR:
            return TaskResult(
                task_id=task.task_id,
                type=task.type,
                output={"text": f"ocr {task.task_id} by {self.worker_id}", "confidence": 0.99},
                worker_id=self.worker_id,
            )
        return TaskResult(
            task_id=task.task_id,
            type=task.type,
            output={"text": f"gen {task.task_id} by {self.worker_id}"},
            worker_id=self.worker_id,
        )


@pytest.fixture()
def secure_bus_env():
    bin_path = _find_nats_server()
    if not bin_path:
        pytest.skip("nats-server binary not found")
    port = _free_port()
    with tempfile.TemporaryDirectory(prefix="aicf_sec_bus_") as tmp:
        root = Path(tmp)
        pki_dir = root / "pki"
        rogue_pki_dir = root / "rogue_pki"
        expired_pki_dir = root / "expired_pki"
        store_dir = root / "js"
        store_dir.mkdir(parents=True, exist_ok=True)

        generate_bus_pki(
            pki_dir,
            identities=[
                "orchestrator",
                "worker-public",
                "worker-restricted",
                "worker-ocr-only",
                "observer-1",
                "unenrolled-agent",
            ],
            valid_s=3600.0,
        )
        generate_bus_pki(rogue_pki_dir, identities=["worker-public"], valid_s=3600.0)
        # Expired certificate: valid_s = -120 -> already expired
        generate_bus_pki(expired_pki_dir, identities=["worker-public"], valid_s=-120.0)

        policy = BusSecurityPolicy(
            grants={
                "orchestrator": AgentGrant(
                    agent_id="orchestrator",
                    role="orchestrator",
                    allowed_types=("*",),
                    allowed_data_classes=("PUBLIC", "INTERNAL", "RESTRICTED"),
                ),
                "worker-public": AgentGrant(
                    agent_id="worker-public",
                    role="worker",
                    allowed_types=(TaskType.TEXT_GENERATE.value,),
                    allowed_data_classes=("PUBLIC", "INTERNAL"),
                ),
                "worker-restricted": AgentGrant(
                    agent_id="worker-restricted",
                    role="worker",
                    allowed_types=(TaskType.TEXT_GENERATE.value,),
                    allowed_data_classes=("PUBLIC", "INTERNAL", "RESTRICTED"),
                ),
                "worker-ocr-only": AgentGrant(
                    agent_id="worker-ocr-only",
                    role="worker",
                    allowed_types=(TaskType.OCR.value,),
                    allowed_data_classes=("PUBLIC", "INTERNAL"),
                ),
                "observer-1": AgentGrant(
                    agent_id="observer-1",
                    role="observer",
                    allowed_types=(),
                    allowed_data_classes=(),
                ),
            },
            require_auth=True,
        )

        conf_path = root / "nats-mtls.conf"
        conf_path.write_text(policy.generate_nats_conf(port=port, store_dir=store_dir, pki_dir=pki_dir))

        proc = subprocess.Popen(
            [bin_path, "-c", str(conf_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"tls://127.0.0.1:{port}"
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        try:
            yield {
                "url": url,
                "pki": pki_dir,
                "rogue_pki": rogue_pki_dir,
                "expired_pki": expired_pki_dir,
                "policy": policy,
            }
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _tls_for(pki_dir: Path, name: str, ca_dir: Path | None = None) -> BusTLSConfig:
    cdir = ca_dir or pki_dir
    return BusTLSConfig(
        ca_file=cdir / "ca.crt",
        cert_file=pki_dir / f"{name}.crt",
        key_file=pki_dir / f"{name}.key",
    )


def test_mtls_rejects_unauthenticated_rogue_expired_and_impersonated_agents(secure_bus_env):
    """Check Pass (c): Agent without valid cert/key, rogue CA, expired cert, unenrolled identity,
    or mismatched CN (impersonation) is rejected with BusAuthError."""
    url = secure_bus_env["url"]
    pki = secure_bus_env["pki"]
    rogue_pki = secure_bus_env["rogue_pki"]
    expired_pki = secure_bus_env["expired_pki"]
    policy = secure_bus_env["policy"]

    # 1. Missing TLS config when policy requires auth -> rejected
    with pytest.raises(BusAuthError):
        w = BusWorker(url, SpyExecutor("worker-public"), name="worker-public", tls_config=None, security_policy=policy)
        w.run()

    # 2. Missing client cert/key files -> rejected
    with pytest.raises(BusAuthError):
        bad_tls = BusTLSConfig(ca_file=pki / "ca.crt", cert_file=None, key_file=None)
        w = BusWorker(url, SpyExecutor("worker-public"), name="worker-public", tls_config=bad_tls, security_policy=policy)
        w.run()

    # 3. Certificate signed by rogue CA -> rejected
    with pytest.raises(BusAuthError, match="not signed by trusted CA"):
        rogue_tls = _tls_for(rogue_pki, "worker-public", ca_dir=pki)
        w = BusWorker(url, SpyExecutor("worker-public"), name="worker-public", tls_config=rogue_tls, security_policy=policy)
        w.run()

    # 4. Expired certificate -> rejected
    with pytest.raises(BusAuthError, match="expired"):
        exp_tls = _tls_for(expired_pki, "worker-public", ca_dir=expired_pki)
        w = BusWorker(url, SpyExecutor("worker-public"), name="worker-public", tls_config=exp_tls, security_policy=policy)
        w.run()

    # 5. Impersonation: valid cert for `worker-ocr-only` claiming `name="worker-restricted"` -> rejected
    with pytest.raises(BusAuthError, match="may not act as agent"):
        imp_tls = _tls_for(pki, "worker-ocr-only")
        w = BusWorker(
            url,
            SpyExecutor("worker-restricted"),
            name="worker-restricted",
            tls_config=imp_tls,
            security_policy=policy,
        )
        w.run()

    # 6. Valid CA-signed cert for `unenrolled-agent` not in policy/NATS users -> rejected
    with pytest.raises(BusAuthError):
        un_tls = _tls_for(pki, "unenrolled-agent")
        w = BusWorker(
            url,
            SpyExecutor("unenrolled-agent"),
            name="unenrolled-agent",
            tls_config=un_tls,
            security_policy=policy,
        )
        w.run()


def test_authorization_blocks_forbidden_task_type_and_restricted_data_class(secure_bus_env):
    """Check Pass (d): Agent without permission cannot pull forbidden TaskType or forbidden data_class
    (`RESTRICTED`), while authorized agents succeed."""
    url = secure_bus_env["url"]
    pki = secure_bus_env["pki"]
    policy = secure_bus_env["policy"]

    # 1. `worker-ocr-only` trying to subscribe to `TEXT_GENERATE` -> BusPermissionError
    with pytest.raises(BusPermissionError, match="not authorized to subscribe to task type"):
        w_bad_type = BusWorker(
            url,
            SpyExecutor("worker-ocr-only"),
            name="worker-ocr-only",
            types=[TaskType.TEXT_GENERATE],
            tls_config=_tls_for(pki, "worker-ocr-only"),
            security_policy=policy,
        )
        w_bad_type.run()

    # 2. `worker-public` trying to subscribe to `RESTRICTED` data_class -> BusPermissionError
    with pytest.raises(BusPermissionError, match="not authorized for data_class"):
        w_bad_dc = BusWorker(
            url,
            SpyExecutor("worker-public"),
            name="worker-public",
            types=[TaskType.TEXT_GENERATE],
            data_classes=["PUBLIC", "INTERNAL", "RESTRICTED"],
            tls_config=_tls_for(pki, "worker-public"),
            security_policy=policy,
        )
        w_bad_dc.run()

    # 3. `observer-1` trying to pull tasks -> BusPermissionError
    with pytest.raises(BusPermissionError, match="cannot pull tasks"):
        w_obs = BusWorker(
            url,
            SpyExecutor("observer-1"),
            name="observer-1",
            types=[TaskType.TEXT_GENERATE],
            tls_config=_tls_for(pki, "observer-1"),
            security_policy=policy,
        )
        w_obs.run()

    # 4. End-to-end: `worker-public` is running online; publish a `RESTRICTED` task.
    #    `worker-public` must NOT pull or execute the `RESTRICTED` task; when `worker-restricted` joins,
    #    `worker-restricted` pulls and executes it!
    async def _e2e_isolation():
        bx = BusExecutor(
            url,
            task_timeout=15.0,
            ack_wait=2.0,
            identity="orchestrator",
            tls_config=_tls_for(pki, "orchestrator"),
            security_policy=policy,
        )
        spy_pub = SpyExecutor("worker-public", delay_s=0.02)
        w_pub = BusWorker(
            url,
            spy_pub,
            name="worker-public",
            types=[TaskType.TEXT_GENERATE],
            data_classes=["PUBLIC", "INTERNAL"],
            tls_config=_tls_for(pki, "worker-public"),
            security_policy=policy,
        )
        t_pub = asyncio.create_task(w_pub.run_async())
        try:
            for _ in range(50):
                if w_pub.ready.is_set():
                    break
                await asyncio.sleep(0.02)
            assert w_pub.ready.is_set()

            # Publish a RESTRICTED task while only `worker-public` is online
            restricted_task = Task(
                task_id="secret-payroll-1",
                tenant_id="acme",
                type=TaskType.TEXT_GENERATE,
                input={"messages": [{"role": "user", "content": "classified"}]},
                data_class="RESTRICTED",
            )
            fut = asyncio.create_task(bx.execute_async(restricted_task))
            await asyncio.sleep(0.4)
            # `worker-public` must NOT have seen `secret-payroll-1`
            assert "secret-payroll-1" not in spy_pub.seen_tasks
            assert not fut.done()

            # Direct unit check on `_validate_worker_capability`: even if delivered, `w_pub` refuses `RESTRICTED`
            with pytest.raises(BusPermissionError, match="RESTRICTED"):
                w_pub._validate_worker_capability(restricted_task)

            # Now bring `worker-restricted` online -> it pulls and completes `secret-payroll-1`
            spy_res = SpyExecutor("worker-restricted", delay_s=0.02)
            w_res = BusWorker(
                url,
                spy_res,
                name="worker-restricted",
                types=[TaskType.TEXT_GENERATE],
                data_classes=["RESTRICTED"],
                tls_config=_tls_for(pki, "worker-restricted"),
                security_policy=policy,
            )
            t_res = asyncio.create_task(w_res.run_async())
            try:
                res = await fut
                assert res.worker_id == "worker-restricted"
                assert spy_pub.seen_tasks == []
                assert spy_res.seen_tasks == ["secret-payroll-1"]
            finally:
                w_res.request_drain()
                await t_res
        finally:
            w_pub.request_drain()
            await t_pub
            bx.close()

    asyncio.run(_e2e_isolation())


def test_payload_limit_ttl_tenant_quota_and_queue_depth_backpressure(secure_bus_env):
    """Check Pass (e) & E.2: Enforces max_payload_bytes, task TTL, per-tenant quotas, and controlled
    rejection (`QueueFullError`) when queue depth exceeds `max_queue_depth`."""
    url = secure_bus_env["url"]
    pki = secure_bus_env["pki"]
    policy = secure_bus_env["policy"]

    async def _scenario():
        bx = BusExecutor(
            url,
            task_timeout=10.0,
            ack_wait=2.0,
            identity="orchestrator",
            tls_config=_tls_for(pki, "orchestrator"),
            security_policy=policy,
            max_payload_bytes=2048,
            default_task_ttl=10.0,
            max_queue_depth=4,
            max_tenant_inflight=3,
            tenant_quotas={
                "small-tenant": TenantQuota(max_inflight=2, max_rate_per_s=0.0),
                "rate-limited": TenantQuota(max_inflight=10, max_rate_per_s=2.0),
            },
        )
        try:
            # 1. Payload exceeding max_payload_bytes (2048 bytes) is rejected immediately
            huge_task = Task(
                task_id="huge-1",
                tenant_id="acme",
                type=TaskType.TEXT_GENERATE,
                input={"messages": [{"role": "user", "content": "X" * 5000}]},
            )
            with pytest.raises(TaskTooLargeError):
                await bx.execute_async(huge_task)
            assert bx.rejected_counts["payload_too_large"] == 1

            # 2. Task TTL expiry: publish a task with ttl_s=0.25 with no worker online, then start worker at 0.35s
            exp_task = Task(
                task_id="ttl-exp-1",
                tenant_id="acme",
                type=TaskType.TEXT_GENERATE,
                input={"messages": [{"role": "user", "content": "short lived"}]},
                ttl_s=0.25,
            )
            with pytest.raises(TaskExpiredError):
                await bx.execute_async(exp_task)
            assert bx.rejected_counts["expired"] >= 1

            # 3. Tenant rate limit quota (`max_rate_per_s=2.0` on `rate-limited`)
            spy = SpyExecutor("worker-public", delay_s=0.35)
            w = BusWorker(
                url,
                spy,
                name="worker-public",
                types=[TaskType.TEXT_GENERATE],
                data_classes=["PUBLIC", "INTERNAL"],
                tls_config=_tls_for(pki, "worker-public"),
                security_policy=policy,
                slots=4,
            )
            tw = asyncio.create_task(w.run_async())
            for _ in range(50):
                if w.ready.is_set():
                    break
                await asyncio.sleep(0.02)

            try:
                rl_futs = [
                    asyncio.create_task(
                        bx.execute_async(
                            Task(
                                task_id=f"rl-{i}",
                                tenant_id="rate-limited",
                                type=TaskType.TEXT_GENERATE,
                                input={"messages": [{"role": "user", "content": "hi"}]},
                            )
                        )
                    )
                    for i in range(4)
                ]
                results = await asyncio.gather(*rl_futs, return_exceptions=True)
                assert any(isinstance(r, QuotaExceededError) for r in results)
                assert bx.rejected_counts["tenant_quota"] >= 1

                # 4. Tenant in-flight quota (`small-tenant` has `max_inflight=2`)
                st_futs = [
                    asyncio.create_task(
                        bx.execute_async(
                            Task(
                                task_id=f"st-{i}",
                                tenant_id="small-tenant",
                                type=TaskType.TEXT_GENERATE,
                                input={"messages": [{"role": "user", "content": "hi"}]},
                            )
                        )
                    )
                    for i in range(4)
                ]
                st_results = await asyncio.gather(*st_futs, return_exceptions=True)
                assert any(isinstance(r, QuotaExceededError) for r in st_results)

                # Wait for in-flight tasks to clear before testing global max_queue_depth
                await asyncio.sleep(0.5)

                # 5. Global queue depth backpressure (`max_queue_depth=4`):
                #    Send 8 concurrent tasks across multiple tenants -> at least 4 must be rejected with QueueFullError
                #    and `bx.inflight_total` must never exceed `max_queue_depth` (4).
                peak_inflight = 0
                qf_futs = []
                for i in range(8):
                    qf_futs.append(
                        asyncio.create_task(
                            bx.execute_async(
                                Task(
                                    task_id=f"qf-{i}",
                                    tenant_id=f"tenant-{i}",
                                    type=TaskType.TEXT_GENERATE,
                                    input={"messages": [{"role": "user", "content": "load"}]},
                                )
                            )
                        )
                    )
                    peak_inflight = max(peak_inflight, bx.inflight_total)
                qf_results = await asyncio.gather(*qf_futs, return_exceptions=True)
                qf_errors = [r for r in qf_results if isinstance(r, QueueFullError)]
                assert len(qf_errors) >= 4, f"expected >=4 QueueFullError, got {qf_results}"
                assert peak_inflight <= 4, f"inflight exceeded max_queue_depth=4: {peak_inflight}"
                assert bx.rejected_counts["queue_full"] >= 4
            finally:
                w.request_drain()
                await tw
        finally:
            bx.close()

    asyncio.run(_scenario())

