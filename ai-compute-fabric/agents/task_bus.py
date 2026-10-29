"""Durable elastic agent task bus on NATS JetStream (plan P2.2 / P7 Pha E).

Provides:
- Durable pull-based work queue (`AICF_TASKS`) with `aicf.task.<type>[.<label>][.dc.<data_class>]`
  backward-compatible subject hierarchy.
- Elastic agent registry in JetStream KV (`AICF_AGENTS`) with TTL, capability declarations
  (`kind`, `task_types`, `labels`, `models`, `slots`, `ctx_per_slot`, `tok_s_ref`, `data_classes`),
  and full agent lifecycle: join -> heartbeat -> graceful drain -> crash recovery.
- Bus security: mTLS (X.509 CA verification + CN-to-agent_id binding) and NKey authentication,
  per-agent publish/subscribe authorization by `TaskType` and `data_class` (`PUBLIC`, `INTERNAL`,
  `RESTRICTED`), max payload size, task TTL expiry, per-tenant in-flight/rate quotas, and
  controlled backpressure (`QueueFullError` + JetStream `DiscardPolicy.NEW`).
- Observability & supervision: Prometheus metrics exposition (`/metrics`, matching `Inference.Metrics`
  in `apps/site-gateway`) and a strictly propose-only `ElasticSupervisor` with anti-oscillation
  hysteresis and cooldown."""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import ssl
import statistics
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import nats
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DiscardPolicy,
    KeyValueConfig,
    RetentionPolicy,
    StreamConfig,
)
from nats.js.errors import APIError, KeyNotFoundError, NoKeysError, NotFoundError
from pydantic import Field

from agents.orchestrator import WorkerLost
from agents.tasks import Strict, Task, TaskResult, TaskType

STREAM = "AICF_TASKS"
SUBJECT = "aicf.task"
AGENT_BUCKET = "AICF_AGENTS"
CONTROL_SUBJECT = "aicf.agent.control"

ALL_DATA_CLASSES = ("PUBLIC", "INTERNAL", "RESTRICTED")
DEFAULT_WORKER_DATA_CLASSES = ("PUBLIC", "INTERNAL")


# ---- Exceptions ---------------------------------------------------------------------------
class RemoteError(Exception):
    """The worker ran the task and it failed in a way a retry will not fix (bad input, denied tool...)."""


class BusAuthError(PermissionError):
    """Client/worker failed TLS/mTLS or NKey authentication or certificate identity binding."""


class BusPermissionError(PermissionError):
    """Authenticated agent is not authorized for the requested TaskType, data_class, or KV key."""


class BusBackpressureError(RuntimeError):
    """The task bus rejected a task cleanly to protect memory and fairness."""


class QueueFullError(BusBackpressureError):
    """Queue depth reached max_queue_depth; task rejected with controlled backpressure."""


class QuotaExceededError(BusBackpressureError):
    """Tenant exceeded its in-flight or rate quota on the task bus."""


class TaskTooLargeError(RemoteError):
    """Serialized task payload exceeds max_payload_bytes."""


class TaskExpiredError(RemoteError):
    """Task TTL expired before execution completed."""


# ---- Subject & Consumer Naming (E.1 & E.2) ------------------------------------------------
def _norm_type(tp: TaskType | str) -> str:
    return tp.value if isinstance(tp, TaskType) else str(tp)


def task_primary_label(task: Task) -> Optional[str]:
    if task.label:
        return task.label
    if task.labels:
        return task.labels[0]
    return None


def task_subject(
    task_or_type: Task | TaskType | str,
    label: Optional[str] = None,
    data_class: Optional[str] = None,
) -> str:
    """Builds the hierarchical subject `aicf.task.<type>[.<label>][.dc.<data_class>]`.

    Backward compatibility:
    - Default `data_class="INTERNAL"` with no label maps to `aicf.task.<type>`.
    - Default `data_class="INTERNAL"` with a label maps to `aicf.task.<type>.<label>`.
    - Non-default `data_class` (`PUBLIC` or `RESTRICTED`) appends `.dc.<data_class>` so workers
      without permission for that data class never subscribe to or pull those messages."""
    if isinstance(task_or_type, Task):
        tp = task_or_type.type.value
        lbl = label if label is not None else task_primary_label(task_or_type)
        dc = data_class if data_class is not None else task_or_type.data_class
    else:
        tp = _norm_type(task_or_type)
        lbl = label
        dc = data_class if data_class is not None else "INTERNAL"

    parts = [SUBJECT, tp]
    if lbl:
        parts.append(lbl)
    if dc and dc != "INTERNAL":
        parts.extend(["dc", dc])
    return ".".join(parts)


def consumer_durable_name(
    task_type: TaskType | str,
    label: Optional[str] = None,
    data_class: str = "INTERNAL",
) -> str:
    """Deterministic JetStream durable consumer name matching `task_subject`."""
    tp = _norm_type(task_type)
    name = f"workers-{tp}"
    if label:
        name += f"-lbl-{label}"
    if data_class and data_class != "INTERNAL":
        name += f"-dc-{data_class}"
    return name


# ---- Agent Registry KV Models & Helpers (E.1) ---------------------------------------------
class AgentProfile(Strict):
    agent_id: str = Field(min_length=1)
    kind: Literal["executor", "compute", "orchestrator"] = "executor"
    state: Literal["active", "draining"] = "active"
    task_types: list[str] = []
    labels: list[str] = []
    models: list[str] = []
    slots: int = Field(default=1, ge=1)
    ctx_per_slot: int = Field(default=4096, ge=0)
    tok_s_ref: float = Field(default=0.0, ge=0.0)
    data_classes: list[str] = ["PUBLIC", "INTERNAL"]
    version: str = "1.0.0"
    started_at: float
    updated_at: float
    ttl_s: float = Field(default=10.0, gt=0)
    executed: int = Field(default=0, ge=0)
    inflight: int = Field(default=0, ge=0)


async def ensure_stream(
    js,
    ack_wait: float = 10.0,
    max_deliver: int = 5,
    max_msgs: int = 10_000,
    max_bytes: int = 64 * 1024 * 1024,
    max_msg_size: int = 256 * 1024,
    max_age: float = 0.0,
    update_if_exists: bool = False,
):
    cfg = StreamConfig(
        name=STREAM,
        subjects=[SUBJECT + ".>"],
        retention=RetentionPolicy.WORK_QUEUE,
        duplicate_window=120,
        max_msgs=max_msgs,
        max_bytes=max_bytes,
        max_msg_size=max_msg_size,
        max_age=max_age if max_age > 0 else None,
        discard=DiscardPolicy.NEW,
    )
    try:
        info = await js.stream_info(STREAM)
        if update_if_exists and (
            info.config.max_msgs != max_msgs
            or info.config.max_bytes != max_bytes
            or info.config.max_msg_size != max_msg_size
            or info.config.discard != DiscardPolicy.NEW
        ):
            await js.update_stream(cfg)
    except NotFoundError:
        try:
            await js.add_stream(cfg)
        except (APIError, nats.errors.TimeoutError):
            pass
    except (APIError, nats.errors.TimeoutError):
        pass


async def ensure_agent_kv(js, ttl: float = 10.0, create_if_missing: bool = True):
    """Ensures the JetStream KV bucket `AICF_AGENTS` exists with the given TTL (seconds)."""
    try:
        return await js.key_value(AGENT_BUCKET)
    except NotFoundError:
        if not create_if_missing:
            raise
        return await js.create_key_value(KeyValueConfig(bucket=AGENT_BUCKET, ttl=ttl))
    except Exception:
        if create_if_missing:
            return await js.create_key_value(KeyValueConfig(bucket=AGENT_BUCKET, ttl=ttl))
        raise


async def list_live_agents(
    js,
    task_type: Optional[TaskType | str] = None,
    label: Optional[str] = None,
    kind: Optional[str] = None,
    data_class: Optional[str] = None,
    include_draining: bool = False,
    now: Optional[float] = None,
) -> list[AgentProfile]:
    """Returns live agents from the `AICF_AGENTS` JetStream KV bucket, filtering expired or draining entries."""
    try:
        kv = await js.key_value(AGENT_BUCKET)
    except NotFoundError:
        return []
    try:
        keys = await kv.keys()
    except (NoKeysError, KeyNotFoundError, NotFoundError):
        return []

    cur_now = now if now is not None else time.time()
    tp_str = _norm_type(task_type) if task_type is not None else None
    out: list[AgentProfile] = []
    for k in keys:
        try:
            entry = await kv.get(k)
        except (KeyNotFoundError, NotFoundError):
            continue
        if not entry or not entry.value:
            continue
        try:
            prof = AgentProfile.model_validate_json(entry.value)
        except Exception:
            continue
        if cur_now - prof.updated_at > prof.ttl_s:
            continue
        if not include_draining and prof.state != "active":
            continue
        if tp_str and prof.task_types and tp_str not in prof.task_types and "*" not in prof.task_types:
            continue
        if label and label not in prof.labels:
            continue
        if kind and prof.kind != kind:
            continue
        if data_class and data_class not in prof.data_classes:
            continue
        out.append(prof)
    out.sort(key=lambda a: a.agent_id)
    return out


# ---- Security: mTLS PKI, Identity Verification & Authorization Policy (E.2) ---------------
@dataclass(frozen=True)
class BusTLSConfig:
    ca_file: str | Path
    cert_file: Optional[str | Path] = None
    key_file: Optional[str | Path] = None
    nkey_seed_file: Optional[str | Path] = None
    verify_cn: bool = True

    def verify_client_certificate(self, expected_identity: str) -> None:
        """Verifies that the client has a valid X.509 certificate signed by `ca_file` with CN == expected_identity."""
        ca_path = Path(self.ca_file)
        if not ca_path.is_file():
            raise BusAuthError(f"CA certificate not found: {ca_path}")
        if not self.cert_file or not self.key_file:
            if self.nkey_seed_file and Path(self.nkey_seed_file).is_file():
                return
            raise BusAuthError(f"missing client certificate/key for agent {expected_identity!r}")
        cert_path, key_path = Path(self.cert_file), Path(self.key_file)
        if not cert_path.is_file() or not key_path.is_file():
            raise BusAuthError(f"client certificate or key file missing for {expected_identity!r}")

        try:
            ca_cert = x509.load_pem_x509_certificate(ca_path.read_bytes())
            cli_cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            priv_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except Exception as e:
            raise BusAuthError(f"failed to load TLS credentials for {expected_identity!r}: {e}") from e

        # 1. Verify private key matches certificate public key
        if priv_key.public_key().public_numbers() != cli_cert.public_key().public_numbers():
            raise BusAuthError(f"private key does not match certificate for {expected_identity!r}")

        # 2. Verify certificate is signed by the trusted CA
        try:
            ca_pub = ca_cert.public_key()
            ca_pub.verify(  # type: ignore[call-arg]
                cli_cert.signature,
                cli_cert.tbs_certificate_bytes,
                ec.ECDSA(cli_cert.signature_hash_algorithm),  # type: ignore[arg-type]
            )
        except Exception as e:
            raise BusAuthError(f"client certificate for {expected_identity!r} is not signed by trusted CA: {e}") from e

        # 3. Verify certificate validity window
        now = datetime.datetime.now(datetime.timezone.utc)
        if now < cli_cert.not_valid_before_utc or now > cli_cert.not_valid_after_utc:
            raise BusAuthError(f"client certificate for {expected_identity!r} is expired or not yet valid")

        # 4. Verify CommonName binding (prevents agent impersonation, matching gateway commit f603ecf)
        if self.verify_cn:
            cn_attrs = cli_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            cn = cn_attrs[0].value if cn_attrs else ""
            if cn != expected_identity:
                raise BusAuthError(
                    f"certificate CN {cn!r} may not act as agent {expected_identity!r}"
                )

    def create_ssl_context(self, expected_identity: str) -> ssl.SSLContext:
        self.verify_client_certificate(expected_identity)
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.ca_file))
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.cert_file and self.key_file:
            ctx.load_cert_chain(certfile=str(self.cert_file), keyfile=str(self.key_file))
        return ctx


@dataclass(frozen=True)
class AgentGrant:
    agent_id: str
    role: Literal["worker", "orchestrator", "observer"] = "worker"
    allowed_types: tuple[str, ...] = ()
    allowed_data_classes: tuple[str, ...] = DEFAULT_WORKER_DATA_CLASSES
    allowed_labels: tuple[str, ...] = ("*",)


@dataclass
class BusSecurityPolicy:
    grants: dict[str, AgentGrant] = field(default_factory=dict)
    require_auth: bool = True

    def authorize_connect(self, identity: str, tls_config: Optional[BusTLSConfig]) -> AgentGrant:
        if not self.require_auth:
            return self.grants.get(
                identity,
                AgentGrant(
                    agent_id=identity,
                    role="worker",
                    allowed_types=("*",),
                    allowed_data_classes=ALL_DATA_CLASSES,
                ),
            )
        if tls_config is None:
            raise BusAuthError(f"TLS/mTLS credentials required for {identity!r}")
        tls_config.verify_client_certificate(identity)
        grant = self.grants.get(identity)
        if grant is None:
            raise BusAuthError(f"identity {identity!r} is not enrolled in bus security policy")
        return grant

    def authorize_worker_subscriptions(
        self,
        agent_id: str,
        requested_types: list[str],
        requested_data_classes: list[str],
        requested_labels: list[str],
    ) -> None:
        if not self.require_auth:
            return
        grant = self.grants.get(agent_id)
        if grant is None:
            raise BusAuthError(f"agent {agent_id!r} is not enrolled")
        if grant.role not in ("worker", "orchestrator"):
            raise BusPermissionError(f"agent {agent_id!r} with role {grant.role!r} cannot pull tasks")
        if "*" not in grant.allowed_types:
            for t in requested_types:
                if t not in grant.allowed_types:
                    raise BusPermissionError(
                        f"agent {agent_id!r} is not authorized to subscribe to task type {t!r}"
                    )
        if "*" not in grant.allowed_data_classes:
            for dc in requested_data_classes:
                if dc not in grant.allowed_data_classes:
                    raise BusPermissionError(
                        f"agent {agent_id!r} is not authorized for data_class {dc!r}"
                    )
        if "*" not in grant.allowed_labels:
            for lbl in requested_labels:
                if lbl not in grant.allowed_labels:
                    raise BusPermissionError(
                        f"agent {agent_id!r} is not authorized for label {lbl!r}"
                    )

    def authorize_task(self, agent_id: str, task: Task) -> None:
        if not self.require_auth:
            return
        grant = self.grants.get(agent_id)
        if grant is None:
            raise BusAuthError(f"agent {agent_id!r} is not enrolled")
        if "*" not in grant.allowed_types and task.type.value not in grant.allowed_types:
            raise BusPermissionError(
                f"agent {agent_id!r} is forbidden from task type {task.type.value!r}"
            )
        if "*" not in grant.allowed_data_classes and task.data_class not in grant.allowed_data_classes:
            raise BusPermissionError(
                f"agent {agent_id!r} is forbidden from data_class {task.data_class!r}"
            )
        lbl = task_primary_label(task)
        if lbl and "*" not in grant.allowed_labels and lbl not in grant.allowed_labels:
            raise BusPermissionError(
                f"agent {agent_id!r} is forbidden from label {lbl!r}"
            )

    def generate_nats_conf(
        self,
        port: int,
        store_dir: str | Path,
        pki_dir: str | Path,
        host: str = "127.0.0.1",
    ) -> str:
        """Generates a `nats-server` configuration enforcing mTLS (`verify_and_map`) and per-agent ACLs."""
        p = Path(pki_dir)
        sdir = Path(store_dir)
        users_hcl: list[str] = []
        for aid, g in self.grants.items():
            if g.role == "orchestrator":
                users_hcl.append(
                    f'    {{ user: "CN={aid}", permissions: {{ publish: {{ allow: [">"] }}, subscribe: {{ allow: [">"] }} }} }}'
                )
            elif g.role == "observer":
                users_hcl.append(
                    f'    {{ user: "CN={aid}", permissions: {{ publish: {{ allow: ["$JS.API.STREAM.INFO.>", "$JS.API.CONSUMER.INFO.>", "$JS.API.CONSUMER.NAMES.>", "$JS.API.DIRECT.GET.KV_{AGENT_BUCKET}.>"] }}, subscribe: {{ allow: ["_INBOX.>"] }} }} }}'
                )
            else:
                types = list(TaskType) if "*" in g.allowed_types else [TaskType(t) for t in g.allowed_types]
                dcs = list(ALL_DATA_CLASSES) if "*" in g.allowed_data_classes else list(g.allowed_data_classes)
                pub_allow = [
                    '"_INBOX.>"',
                    f'"$KV.{AGENT_BUCKET}.{aid}"',
                    '"$JS.API.STREAM.NAMES"',
                    f'"$JS.API.STREAM.INFO.{STREAM}"',
                    f'"$JS.API.STREAM.INFO.KV_{AGENT_BUCKET}"',
                    f'"$JS.API.DIRECT.GET.KV_{AGENT_BUCKET}.>"',
                ]
                sub_allow = [
                    '"_INBOX.>"',
                    f'"{CONTROL_SUBJECT}.{aid}"',
                    f'"$KV.{AGENT_BUCKET}.>"',
                ]
                for tp in types:
                    for dc in dcs:
                        dur = consumer_durable_name(tp.value, None, dc)
                        subj = task_subject(tp.value, None, dc)
                        pub_allow.append(f'"$JS.API.CONSUMER.CREATE.{STREAM}.{dur}.>"')
                        pub_allow.append(f'"$JS.API.CONSUMER.CREATE.{STREAM}.{dur}"')
                        pub_allow.append(f'"$JS.API.CONSUMER.INFO.{STREAM}.{dur}"')
                        pub_allow.append(f'"$JS.API.CONSUMER.MSG.NEXT.{STREAM}.{dur}"')
                        pub_allow.append(f'"$JS.ACK.{STREAM}.{dur}.>"')
                        sub_allow.append(f'"{subj}"')
                        if "*" in g.allowed_labels:
                            pub_allow.append(f'"$JS.API.CONSUMER.CREATE.{STREAM}.workers-{tp.value}-lbl-*.>"')
                            pub_allow.append(f'"$JS.API.CONSUMER.INFO.{STREAM}.workers-{tp.value}-lbl-*"')
                            pub_allow.append(f'"$JS.API.CONSUMER.MSG.NEXT.{STREAM}.workers-{tp.value}-lbl-*"')
                            pub_allow.append(f'"$JS.ACK.{STREAM}.workers-{tp.value}-lbl-*.>"')
                            sub_allow.append(f'"{SUBJECT}.{tp.value}.*"')
                        else:
                            for lbl in g.allowed_labels:
                                ldur = consumer_durable_name(tp.value, lbl, dc)
                                lsubj = task_subject(tp.value, lbl, dc)
                                pub_allow.append(f'"$JS.API.CONSUMER.CREATE.{STREAM}.{ldur}.>"')
                                pub_allow.append(f'"$JS.API.CONSUMER.INFO.{STREAM}.{ldur}"')
                                pub_allow.append(f'"$JS.API.CONSUMER.MSG.NEXT.{STREAM}.{ldur}"')
                                pub_allow.append(f'"$JS.ACK.{STREAM}.{ldur}.>"')
                                sub_allow.append(f'"{lsubj}"')
                users_hcl.append(
                    f'    {{ user: "CN={aid}", permissions: {{ publish: {{ allow: [{", ".join(pub_allow)}] }}, subscribe: {{ allow: [{", ".join(sub_allow)}] }} }} }}'
                )

        users_block = "\n".join(users_hcl)
        return (
            f'host: "{host}"\n'
            f"port: {port}\n"
            f'jetstream {{ store_dir: "{sdir}" }}\n'
            f"tls {{\n"
            f'  cert_file: "{p / "server.crt"}"\n'
            f'  key_file: "{p / "server.key"}"\n'
            f'  ca_file: "{p / "ca.crt"}"\n'
            f"  verify_and_map: true\n"
            f"}}\n"
            f"authorization {{\n"
            f"  users = [\n"
            f"{users_block}\n"
            f"  ]\n"
            f"}}\n"
        )


def generate_bus_pki(
    pki_dir: str | Path,
    identities: list[str],
    valid_s: float = 86400.0,
    hosts: tuple[str, ...] = ("127.0.0.1", "localhost"),
) -> Path:
    """Generates internal CA, NATS server cert, and per-agent client certs (`CN=<agent_id>`)."""
    p = Path(pki_dir)
    p.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    not_after = now + datetime.timedelta(seconds=max(1.0, valid_s))

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AICF Task Bus Internal CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(seconds=max(3600.0, valid_s * 4)))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    (p / "ca.crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (p / "ca.key").write_bytes(
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    def _issue(cn: str, serial: int, is_server: bool) -> None:
        k = ec.generate_private_key(ec.SECP256R1())
        subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        b = (
            x509.CertificateBuilder()
            .subject_name(subj)
            .issuer_name(ca_name)
            .public_key(k.public_key())
            .serial_number(serial)
            .not_valid_before(now)
            .not_valid_after(not_after)
        )
        if is_server:
            sans: list[x509.GeneralName] = []
            for h in hosts:
                try:
                    sans.append(x509.IPAddress(ipaddress.ip_address(h)))
                except ValueError:
                    sans.append(x509.DNSName(h))
            b = b.add_extension(x509.SubjectAlternativeName(sans), critical=False)
            b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        else:
            b = b.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        cert = b.sign(ca_key, hashes.SHA256())
        (p / f"{cn}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (p / f"{cn}.key").write_bytes(
            k.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )

    _issue("server", 2, is_server=True)
    for idx, ident in enumerate(identities):
        _issue(ident, 10 + idx, is_server=False)
    return p


async def _connect_nats(
    url: str,
    identity: str,
    tls_config: Optional[BusTLSConfig] = None,
    security_policy: Optional[BusSecurityPolicy] = None,
):
    if security_policy is not None:
        security_policy.authorize_connect(identity, tls_config)
    elif tls_config is not None:
        tls_config.verify_client_certificate(identity)

    kwargs: dict[str, Any] = {
        "name": identity,
        "allow_reconnect": False,
        "connect_timeout": 5,
    }
    if tls_config is not None:
        kwargs["tls"] = tls_config.create_ssl_context(identity)
        if tls_config.nkey_seed_file:
            kwargs["nkeys_seed"] = str(tls_config.nkey_seed_file)
    try:
        return await nats.connect(url, **kwargs)
    except Exception as e:
        msg = str(e).lower()
        if any(w in msg for w in ("tls", "ssl", "cert", "auth", "permission", "violation", "handshake", "eof")):
            raise BusAuthError(f"NATS connection rejected for {identity!r}: {e}") from e
        raise


# ---- Tenant Quotas (E.2) ------------------------------------------------------------------
@dataclass
class TenantQuota:
    max_inflight: int = 32
    max_rate_per_s: float = 0.0  # 0.0 = unlimited


# ---- BusExecutor --------------------------------------------------------------------------
class BusExecutor:
    def __init__(
        self,
        url: str,
        task_timeout: float = 60.0,
        ack_wait: float = 10.0,
        max_deliver: int = 5,
        *,
        identity: str = "orchestrator",
        tls_config: Optional[BusTLSConfig] = None,
        security_policy: Optional[BusSecurityPolicy] = None,
        max_payload_bytes: int = 256 * 1024,
        default_task_ttl: float = 120.0,
        max_queue_depth: int = 10_000,
        max_stream_bytes: int = 64 * 1024 * 1024,
        max_tenant_inflight: int = 64,
        tenant_quotas: Optional[dict[str, TenantQuota]] = None,
    ):
        self.url = url
        self.task_timeout, self.ack_wait, self.max_deliver = task_timeout, ack_wait, max_deliver
        self.identity = identity
        self.tls_config = tls_config
        self.security_policy = security_policy
        self.max_payload_bytes = max_payload_bytes
        self.default_task_ttl = default_task_ttl
        self.max_queue_depth = max_queue_depth
        self.max_stream_bytes = max_stream_bytes
        self.max_tenant_inflight = max_tenant_inflight
        self.tenant_quotas = dict(tenant_quotas or {})

        self._mu = threading.Lock()
        self.inflight_total = 0
        self.tenant_inflight: dict[str, int] = {}
        self._tenant_timestamps: dict[str, deque[float]] = {}
        self.rejected_counts: dict[str, int] = {
            "queue_full": 0,
            "tenant_quota": 0,
            "payload_too_large": 0,
            "expired": 0,
            "auth_denied": 0,
        }
        self.wait_samples_by_type: dict[str, deque[float]] = {}
        self.completed_total = 0

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.run_id = uuid.uuid4().hex[:8]
        try:
            self.nc = self._run(_connect_nats(url, identity, tls_config, security_policy))
            self.js = self.nc.jetstream()
            self._run(
                ensure_stream(
                    self.js,
                    ack_wait=ack_wait,
                    max_deliver=max_deliver,
                    max_msgs=max_queue_depth,
                    max_bytes=max_stream_bytes,
                    max_msg_size=max_payload_bytes,
                    update_if_exists=True,
                )
            )
            self._run(ensure_agent_kv(self.js, ttl=10.0, create_if_missing=True))
        except Exception:
            self.loop.call_soon_threadsafe(self.loop.stop)
            raise

    def _run(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def _check_and_acquire_admission(self, task: Task, payload_len: int, now: float) -> None:
        if payload_len > self.max_payload_bytes:
            with self._mu:
                self.rejected_counts["payload_too_large"] += 1
            raise TaskTooLargeError(
                f"{task.task_id}: payload {payload_len} bytes exceeds max_payload_bytes={self.max_payload_bytes}"
            )

        with self._mu:
            if self.inflight_total >= self.max_queue_depth:
                self.rejected_counts["queue_full"] += 1
                raise QueueFullError(
                    f"{task.task_id}: bus queue depth {self.inflight_total} reached limit {self.max_queue_depth}"
                )

            quota = self.tenant_quotas.get(task.tenant_id)
            t_max_inflight = quota.max_inflight if quota else self.max_tenant_inflight
            cur_t_inflight = self.tenant_inflight.get(task.tenant_id, 0)
            if cur_t_inflight >= t_max_inflight:
                self.rejected_counts["tenant_quota"] += 1
                raise QuotaExceededError(
                    f"{task.task_id}: tenant {task.tenant_id!r} in-flight {cur_t_inflight} reached quota {t_max_inflight}"
                )

            if quota and quota.max_rate_per_s > 0:
                win = self._tenant_timestamps.setdefault(task.tenant_id, deque())
                while win and now - win[0] > 1.0:
                    win.popleft()
                if len(win) >= quota.max_rate_per_s:
                    self.rejected_counts["tenant_quota"] += 1
                    raise QuotaExceededError(
                        f"{task.task_id}: tenant {task.tenant_id!r} rate exceeded {quota.max_rate_per_s}/s"
                    )
                win.append(now)

            self.inflight_total += 1
            self.tenant_inflight[task.tenant_id] = cur_t_inflight + 1

    def _release_admission(self, tenant_id: str) -> None:
        with self._mu:
            self.inflight_total = max(0, self.inflight_total - 1)
            cur = self.tenant_inflight.get(tenant_id, 0)
            if cur <= 1:
                self.tenant_inflight.pop(tenant_id, None)
            else:
                self.tenant_inflight[tenant_id] = cur - 1

    def _record_wait(self, task_type: str, wait_s: float) -> None:
        with self._mu:
            dq = self.wait_samples_by_type.setdefault(task_type, deque(maxlen=500))
            dq.append(max(0.0, wait_s))
            self.completed_total += 1

    def execute(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        return self._run(self._execute(task, attempt, deps or {}), self.task_timeout + 15)

    async def execute_async(self, task: Task, attempt: int = 1, deps: dict | None = None) -> TaskResult:
        """Async execution method for high-concurrency callers on `self.loop`."""
        fut = asyncio.run_coroutine_threadsafe(self._execute(task, attempt, deps or {}), self.loop)
        return await asyncio.wrap_future(fut)

    async def _execute(self, task: Task, attempt: int, deps: dict) -> TaskResult:
        now = time.time()
        ttl = task.ttl_s if task.ttl_s is not None else self.default_task_ttl
        expires_at = now + ttl
        inbox = self.nc.new_inbox()
        body_dict = {
            "task": task.model_dump(mode="json"),
            "attempt": attempt,
            "deps": deps,
            "reply": inbox,
            "published_at": now,
            "expires_at": expires_at,
        }
        body = json.dumps(body_dict).encode()
        self._check_and_acquire_admission(task, len(body), now)

        got: asyncio.Queue = asyncio.Queue()

        async def on_reply(m):
            got.put_nowait(m.data)

        sub = await self.nc.subscribe(inbox, cb=on_reply)
        try:
            subj = task_subject(task)
            try:
                await self.js.publish(
                    subj,
                    body,
                    headers={"Nats-Msg-Id": f"{self.run_id}/{task.task_id}/{attempt}"},
                )
            except APIError as e:
                with self._mu:
                    self.rejected_counts["queue_full"] += 1
                raise QueueFullError(f"{task.task_id}: stream rejected publish ({e})") from e

            wait_limit = min(self.task_timeout, ttl + 1.0)
            try:
                raw = await asyncio.wait_for(got.get(), wait_limit)
            except asyncio.TimeoutError:
                if time.time() >= expires_at and task.ttl_s is not None:
                    with self._mu:
                        self.rejected_counts["expired"] += 1
                    raise TaskExpiredError(f"{task.task_id}: expired after TTL {ttl}s") from None
                raise WorkerLost(f"{task.task_id}: no reply in {wait_limit}s") from None

            reply = json.loads(raw)
            if "wait_s" in reply:
                self._record_wait(task.type.value, float(reply["wait_s"]))
            else:
                self._record_wait(task.type.value, time.time() - now)

            if not reply["ok"]:
                err_str = reply.get("error", "unknown remote error")
                if err_str.startswith("TaskExpiredError:"):
                    with self._mu:
                        self.rejected_counts["expired"] += 1
                    raise TaskExpiredError(f"{task.task_id}: {err_str}")
                raise RemoteError(f"{task.task_id}: {err_str}")
            return TaskResult.model_validate(reply["result"])
        finally:
            self._release_admission(task.tenant_id)
            try:
                await sub.unsubscribe()  # a second reply (task ran twice) goes nowhere: first one wins
            except Exception:
                pass

    def close(self):
        async def _shutdown():
            try:
                await self.nc.flush(timeout=2.0)
            except Exception:
                pass
            try:
                await self.nc.close()
            except Exception:
                pass

        try:
            self._run(_shutdown(), timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


# ---- BusWorker (E.1 & E.2) ----------------------------------------------------------------
class BusWorker:
    """Pulls tasks matching its capabilities, registers in `AICF_AGENTS` KV, runs with `executor`, and replies."""

    def __init__(
        self,
        url: str,
        executor,
        name: str = "worker",
        ack_wait: float = 10.0,
        max_deliver: int = 5,
        types: list | None = None,
        *,
        kind: Literal["executor", "compute", "orchestrator"] = "executor",
        labels: list[str] | None = None,
        models: list[str] | None = None,
        slots: int = 1,
        ctx_per_slot: int = 4096,
        tok_s_ref: float = 0.0,
        data_classes: list[str] | tuple[str, ...] | None = None,
        version: str = "1.0.0",
        agent_ttl: float = 10.0,
        tls_config: Optional[BusTLSConfig] = None,
        security_policy: Optional[BusSecurityPolicy] = None,
        max_payload_bytes: int = 256 * 1024,
        labeled_only: bool = False,
    ):
        self.url, self.executor, self.name = url, executor, name
        self.ack_wait, self.max_deliver, self.types = ack_wait, max_deliver, types
        self.kind = kind
        self.labels = list(labels or [])
        self.models = list(models or [])
        self.slots = max(1, int(slots))
        self.ctx_per_slot = int(ctx_per_slot)
        self.tok_s_ref = float(tok_s_ref)
        self.version = version
        self.agent_ttl = float(agent_ttl)
        self.tls_config = tls_config
        self.security_policy = security_policy
        self.max_payload_bytes = max_payload_bytes
        self.labeled_only = labeled_only

        self._explicit_data_classes = data_classes is not None
        self.data_classes = list(data_classes) if data_classes is not None else list(DEFAULT_WORKER_DATA_CLASSES)
        self.allowed_data_classes = set(self.data_classes)
        self.allowed_types = (
            {_norm_type(t) for t in types}
            if types is not None
            else {t.value for t in TaskType}
        )

        self.executed = 0
        self.inflight = 0
        self.started_at = time.time()
        self.stop = threading.Event()
        self.draining = threading.Event()
        self.ready = threading.Event()

    def request_drain(self) -> None:
        """Signals the worker to stop pulling new tasks, finish in-flight tasks, remove KV entry, and exit."""
        self.draining.set()

    def _build_profile(self) -> AgentProfile:
        now = time.time()
        return AgentProfile(
            agent_id=self.name,
            kind=self.kind,
            state="draining" if self.draining.is_set() else "active",
            task_types=sorted(self.allowed_types) if self.types is not None else ["*"],
            labels=list(self.labels),
            models=list(self.models),
            slots=self.slots,
            ctx_per_slot=self.ctx_per_slot,
            tok_s_ref=self.tok_s_ref,
            data_classes=list(self.data_classes),
            version=self.version,
            started_at=self.started_at,
            updated_at=now,
            ttl_s=self.agent_ttl,
            executed=self.executed,
            inflight=self.inflight,
        )

    def run(self):
        asyncio.run(self._main())

    async def run_async(self):
        """Coroutine entry point so K=200 workers can run in a single event loop with minimal RAM."""
        await self._main()

    async def _setup_subscriptions(self, js) -> list:
        if self.security_policy is not None:
            self.security_policy.authorize_worker_subscriptions(
                self.name,
                sorted(self.allowed_types),
                self.data_classes,
                self.labels,
            )

        subs = []
        cfg = ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=self.ack_wait,
            max_deliver=self.max_deliver,
        )

        # Legacy wildcard subscription when no specific types, labels, or data_classes were restricted
        if self.types is None and not self.labels and not self._explicit_data_classes and self.security_policy is None:
            subs.append(await js.pull_subscribe(f"{SUBJECT}.>", durable="workers", stream=STREAM, config=cfg))
            return subs

        target_types = [_norm_type(t) for t in self.types] if self.types is not None else [t.value for t in TaskType]
        label_choices: list[Optional[str]] = []
        if not self.labeled_only:
            label_choices.append(None)
        for lbl in self.labels:
            if lbl not in label_choices:
                label_choices.append(lbl)

        # If data_classes was not explicitly passed and equals DEFAULT_WORKER_DATA_CLASSES,
        # subscribe to INTERNAL (default `aicf.task.<type>[.<label>]`) and PUBLIC (`...dc.PUBLIC`)
        for tp in target_types:
            for lbl in label_choices:
                for dc in self.data_classes:
                    subj = task_subject(tp, label=lbl, data_class=dc)
                    dur = consumer_durable_name(tp, label=lbl, data_class=dc)
                    try:
                        sub = await js.pull_subscribe(subj, durable=dur, stream=STREAM, config=cfg)
                        subs.append(sub)
                    except (APIError, nats.errors.TimeoutError) as e:
                        raise BusPermissionError(
                            f"NATS server denied subscription for agent {self.name!r} on {subj!r} ({dur}): {e}"
                        ) from e
        return subs

    async def _main(self):
        self.started_at = time.time()
        if self.security_policy is not None:
            self.security_policy.authorize_connect(self.name, self.tls_config)
            self.security_policy.authorize_worker_subscriptions(
                self.name,
                sorted(self.allowed_types),
                self.data_classes,
                self.labels,
            )
        nc = await _connect_nats(self.url, self.name, self.tls_config, self.security_policy)
        try:
            js = nc.jetstream()
            if self.security_policy is None:
                await ensure_stream(
                    js,
                    self.ack_wait,
                    self.max_deliver,
                    max_msg_size=self.max_payload_bytes,
                    update_if_exists=False,
                )
            kv = None
            try:
                kv = await ensure_agent_kv(
                    js,
                    ttl=self.agent_ttl,
                    create_if_missing=(self.security_policy is None),
                )
                await kv.put(self.name, self._build_profile().model_dump_json().encode())
            except (APIError, nats.errors.TimeoutError) as e:
                if self.security_policy is not None:
                    raise BusPermissionError(
                        f"NATS server denied KV registration for agent {self.name!r}: {e}"
                    ) from e
                kv = None
            except Exception:
                kv = None

            # Remote control subscription for graceful drain command
            ctrl_sub = None
            try:
                async def _on_ctrl(m):
                    if m.data == b"drain":
                        self.request_drain()
                ctrl_sub = await nc.subscribe(f"{CONTROL_SUBJECT}.{self.name}", cb=_on_ctrl)
            except Exception:
                ctrl_sub = None

            subs = await self._setup_subscriptions(js)
            self.ready.set()

            async def _kv_heartbeat():
                interval = max(0.15, self.agent_ttl / 3.0)
                while not self.stop.is_set() and not self.draining.is_set():
                    await asyncio.sleep(interval)
                    if kv is not None and not self.stop.is_set():
                        try:
                            await kv.put(self.name, self._build_profile().model_dump_json().encode())
                        except Exception:
                            pass

            hb_task = asyncio.create_task(_kv_heartbeat())
            fetch_timeout = 0.25 if len(subs) == 1 else 0.05

            async def _slot_worker():
                while not self.stop.is_set() and not self.draining.is_set():
                    got_any = False
                    for sub in subs:
                        if self.stop.is_set() or self.draining.is_set():
                            break
                        try:
                            msgs = await sub.fetch(1, timeout=fetch_timeout)
                        except (asyncio.TimeoutError, nats.errors.TimeoutError):
                            continue
                        except APIError as e:
                            raise BusPermissionError(
                                f"NATS server denied fetch for agent {self.name!r}: {e}"
                            ) from e
                        except Exception:
                            await asyncio.sleep(0.05)
                            continue
                        for m in msgs:
                            got_any = True
                            try:
                                await self._handle(nc, m)
                            except Exception as e:
                                print(
                                    f"[{self.name}] dropping message: {type(e).__name__}: {e}",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                try:
                                    await m.term()
                                except Exception:
                                    pass
                    if not got_any and len(subs) > 1 and not self.stop.is_set() and not self.draining.is_set():
                        await asyncio.sleep(0.02)

            slot_tasks = [asyncio.create_task(_slot_worker()) for _ in range(self.slots)]
            try:
                await asyncio.gather(*slot_tasks)
            finally:
                hb_task.cancel()
                if ctrl_sub is not None:
                    try:
                        await ctrl_sub.unsubscribe()
                    except Exception:
                        pass
                if kv is not None:
                    try:
                        await kv.delete(self.name)
                    except Exception:
                        pass
        finally:
            try:
                await nc.flush(timeout=2.0)
            except Exception:
                pass
            try:
                await nc.close()
            except Exception:
                pass

    def _validate_worker_capability(self, task: Task) -> None:
        if self.security_policy is not None:
            self.security_policy.authorize_task(self.name, task)
        if task.type.value not in self.allowed_types:
            raise BusPermissionError(
                f"worker {self.name!r} does not accept task type {task.type.value!r}"
            )
        if task.data_class not in self.allowed_data_classes:
            raise BusPermissionError(
                f"worker {self.name!r} does not accept data_class {task.data_class!r}"
            )
        req_lbl = task_primary_label(task)
        if req_lbl and req_lbl not in self.labels:
            raise BusPermissionError(
                f"worker {self.name!r} does not have required label {req_lbl!r}"
            )
        if task.model and self.models and task.model not in self.models:
            raise BusPermissionError(
                f"worker {self.name!r} does not serve model {task.model!r}"
            )
        if task.min_ctx and self.ctx_per_slot and self.ctx_per_slot < task.min_ctx:
            raise BusPermissionError(
                f"worker {self.name!r} ctx_per_slot={self.ctx_per_slot} < min_ctx={task.min_ctx}"
            )

    async def _handle(self, nc, m):
        if len(m.data) > self.max_payload_bytes:
            await m.term()
            return

        req = json.loads(m.data)
        task = Task.model_validate(req["task"])

        # Enforce task TTL before running
        now = time.time()
        expires_at = req.get("expires_at")
        if expires_at is not None and now > float(expires_at):
            await nc.publish(
                req["reply"],
                json.dumps(
                    {
                        "ok": False,
                        "error": f"TaskExpiredError: task {task.task_id} expired before execution",
                        "wait_s": max(0.0, now - float(req.get("published_at", now))),
                    }
                ).encode(),
            )
            await m.term()
            return

        # Enforce worker capability and security policy (do not execute unauthorized tasks; nak so an authorized worker gets it)
        try:
            self._validate_worker_capability(task)
        except (BusPermissionError, BusAuthError):
            await m.nak(delay=0.2)
            return

        wait_s = max(0.0, now - float(req.get("published_at", now)))

        async def beat():  # keeps JetStream from redelivering a task that is merely slow
            while True:
                await asyncio.sleep(self.ack_wait / 3)
                try:
                    await m.in_progress()
                except Exception:
                    pass

        hb = asyncio.create_task(beat())
        self.inflight += 1
        try:
            if hasattr(self.executor, "execute_async"):
                res = await self.executor.execute_async(task, req["attempt"], req["deps"])
            else:
                res = await asyncio.to_thread(self.executor.execute, task, req["attempt"], req["deps"])
            self.executed += 1
            await nc.publish(
                req["reply"],
                json.dumps(
                    {
                        "ok": True,
                        "result": res.model_dump(mode="json"),
                        "wait_s": wait_s,
                    }
                ).encode(),
            )
            await m.ack()
        except WorkerLost:  # this machine lost its GPU: let another one take it
            await m.nak(delay=0.5)
            await asyncio.sleep(0.6)
        except Exception as e:  # a retry would fail the same way: tell the caller and stop redelivery
            await nc.publish(
                req["reply"],
                json.dumps(
                    {
                        "ok": False,
                        "error": f"{type(e).__name__}: {e}"[:300],
                        "wait_s": wait_s,
                    }
                ).encode(),
            )
            await m.term()
        finally:
            self.inflight = max(0, self.inflight - 1)
            hb.cancel()


# ---- Observability: Prometheus Metrics & Propose-Only Supervisor (E.3) --------------------
@dataclass
class BusMetricsSnapshot:
    queue_depth_by_type: dict[str, int]
    inflight_by_type: dict[str, int]
    redelivered_by_type: dict[str, int]
    live_agents_by_type: dict[str, int]
    live_agents_total: int
    median_wait_s_by_type: dict[str, float]
    rejected_counts: dict[str, int]
    completed_total: int


class BusMetricsCollector:
    """Collects queue depth (`num_pending`), live agents (`AICF_AGENTS` KV), and median wait time,
    rendering Prometheus text exposition in the same style as `Inference.Metrics` in `apps/site-gateway`."""

    def __init__(
        self,
        url: str,
        executor: Optional[BusExecutor] = None,
        tls_config: Optional[BusTLSConfig] = None,
        security_policy: Optional[BusSecurityPolicy] = None,
        identity: str = "orchestrator",
    ):
        self.url = url
        self.executor = executor
        self.tls_config = tls_config
        self.security_policy = security_policy
        self.identity = identity
        self._extra_wait_samples: dict[str, deque[float]] = {}

    def record_wait_sample(self, task_type: TaskType | str, wait_s: float) -> None:
        tp = _norm_type(task_type)
        dq = self._extra_wait_samples.setdefault(tp, deque(maxlen=500))
        dq.append(max(0.0, float(wait_s)))

    async def collect_async(self, js=None) -> BusMetricsSnapshot:
        if js is None and self.executor is not None:
            cur_loop = asyncio.get_running_loop()
            if cur_loop is not self.executor.loop:
                fut = asyncio.run_coroutine_threadsafe(self._collect_on_js(self.executor.js), self.executor.loop)
                return await asyncio.wrap_future(fut)
            return await self._collect_on_js(self.executor.js)
        if js is not None:
            if self.executor is not None and js is self.executor.js and asyncio.get_running_loop() is not self.executor.loop:
                fut = asyncio.run_coroutine_threadsafe(self._collect_on_js(self.executor.js), self.executor.loop)
                return await asyncio.wrap_future(fut)
            return await self._collect_on_js(js)
        own_nc = await _connect_nats(self.url, self.identity, self.tls_config, self.security_policy)
        try:
            return await self._collect_on_js(own_nc.jetstream())
        finally:
            await own_nc.close()

    async def _collect_on_js(self, js) -> BusMetricsSnapshot:
        agents = await list_live_agents(js)
        live_by_type: dict[str, int] = {t.value: 0 for t in TaskType}
        for a in agents:
            if "*" in a.task_types or not a.task_types:
                for t in TaskType:
                    live_by_type[t.value] += 1
            else:
                for tp in a.task_types:
                    live_by_type[tp] = live_by_type.get(tp, 0) + 1

        q_depth: dict[str, int] = {t.value: 0 for t in TaskType}
        inflight: dict[str, int] = {t.value: 0 for t in TaskType}
        redeliv: dict[str, int] = {t.value: 0 for t in TaskType}

        try:
            consumers = await js.consumers_info(STREAM)
        except Exception:
            consumers = []

        for cinfo in consumers:
            cname = cinfo.name or ""
            matched_types: list[str] = []
            if cname == "workers":
                matched_types = [TaskType.TEXT_GENERATE.value]
            elif cname.startswith("workers-"):
                rest = cname[len("workers-") :]
                for t in TaskType:
                    if rest == t.value or rest.startswith(t.value + "-"):
                        matched_types = [t.value]
                        break
            for tp in matched_types:
                q_depth[tp] = q_depth.get(tp, 0) + int(cinfo.num_pending or 0)
                inflight[tp] = inflight.get(tp, 0) + int(cinfo.num_ack_pending or 0)
                redeliv[tp] = redeliv.get(tp, 0) + int(cinfo.num_redelivered or 0)

        median_wait: dict[str, float] = {}
        for t in TaskType:
            tp = t.value
            samples: list[float] = []
            if self.executor is not None:
                with self.executor._mu:
                    samples.extend(self.executor.wait_samples_by_type.get(tp, ()))
            if tp in self._extra_wait_samples:
                samples.extend(self._extra_wait_samples[tp])
            median_wait[tp] = float(statistics.median(samples)) if samples else 0.0

        rejected = {
            "queue_full": 0,
            "tenant_quota": 0,
            "payload_too_large": 0,
            "expired": 0,
            "auth_denied": 0,
        }
        completed = 0
        if self.executor is not None:
            with self.executor._mu:
                rejected.update(self.executor.rejected_counts)
                completed = self.executor.completed_total

        return BusMetricsSnapshot(
            queue_depth_by_type=q_depth,
            inflight_by_type=inflight,
            redelivered_by_type=redeliv,
            live_agents_by_type=live_by_type,
            live_agents_total=len(agents),
            median_wait_s_by_type=median_wait,
            rejected_counts=rejected,
            completed_total=completed,
        )

    def collect(self) -> BusMetricsSnapshot:
        if self.executor is not None:
            return self.executor._run(self._collect_on_js(self.executor.js), timeout=10)
        return asyncio.run(self.collect_async())

    def render_prometheus(self, snap: Optional[BusMetricsSnapshot] = None) -> str:
        if snap is None:
            snap = self.collect()
        lines: list[str] = []

        def header(name: str, typ: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")

        header("aicf_bus_live_agents_total", "gauge", "Total live agents registered in AICF_AGENTS KV")
        lines.append(f"aicf_bus_live_agents_total {snap.live_agents_total}")

        header("aicf_bus_completed_tasks_total", "counter", "Total tasks completed across the task bus")
        lines.append(f"aicf_bus_completed_tasks_total {snap.completed_total}")

        header("aicf_bus_queue_depth", "gauge", "Pending tasks waiting in JetStream per task type")
        for tp, val in sorted(snap.queue_depth_by_type.items()):
            lines.append(f'aicf_bus_queue_depth{{task_type="{tp}"}} {val}')

        header("aicf_bus_inflight_tasks", "gauge", "Tasks currently pulled and executing per task type")
        for tp, val in sorted(snap.inflight_by_type.items()):
            lines.append(f'aicf_bus_inflight_tasks{{task_type="{tp}"}} {val}')

        header("aicf_bus_live_agents", "gauge", "Live agents registered in AICF_AGENTS KV per task type")
        for tp, val in sorted(snap.live_agents_by_type.items()):
            lines.append(f'aicf_bus_live_agents{{task_type="{tp}"}} {val}')

        header("aicf_bus_queue_wait_median_seconds", "gauge", "Median queue wait time in seconds per task type")
        for tp, val in sorted(snap.median_wait_s_by_type.items()):
            lines.append(f'aicf_bus_queue_wait_median_seconds{{task_type="{tp}"}} {val:.6f}')

        header("aicf_bus_redeliveries_total", "counter", "Tasks redelivered after worker timeout or loss")
        for tp, val in sorted(snap.redelivered_by_type.items()):
            lines.append(f'aicf_bus_redeliveries_total{{task_type="{tp}"}} {val}')

        header("aicf_bus_rejected_total", "counter", "Tasks rejected by bus admission control")
        for reason, val in sorted(snap.rejected_counts.items()):
            lines.append(f'aicf_bus_rejected_total{{reason="{reason}"}} {val}')

        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class ScaleRecommendation:
    task_type: str
    action: Literal["scale_up", "scale_down", "noop"]
    current_agents: int
    recommended_agents: int
    queue_depth: int
    median_wait_s: float
    reason: str
    suggested_command: str
    requires_human_approval: bool = True
    applied: bool = False


class ElasticSupervisor:
    """Evaluates bus metrics and outputs scaling recommendations ONLY (D-FAB-4 / Rule 0).

    Never mutates cluster topology or spawns/kills processes automatically. Uses separate high/low
    watermarks (hysteresis) and a minimum cooldown interval between recommendations to prevent
    oscillation."""

    def __init__(
        self,
        *,
        propose_only: bool = True,
        scale_up_queue_depth: int = 8,
        scale_down_queue_depth: int = 0,
        scale_up_wait_s: float = 2.0,
        scale_down_wait_s: float = 0.2,
        tasks_per_agent_target: int = 4,
        min_agents_per_type: int = 1,
        max_agents_per_type: int = 50,
        cooldown_s: float = 60.0,
    ):
        if not propose_only:
            raise PermissionError(
                "ElasticSupervisor must run with propose_only=True (D-FAB-4 / Rule 0: automatic cluster changes are forbidden without human approval)"
            )
        if scale_down_queue_depth >= scale_up_queue_depth:
            raise ValueError("scale_down_queue_depth must be strictly less than scale_up_queue_depth for hysteresis")
        self.propose_only = True
        self.scale_up_queue_depth = scale_up_queue_depth
        self.scale_down_queue_depth = scale_down_queue_depth
        self.scale_up_wait_s = scale_up_wait_s
        self.scale_down_wait_s = scale_down_wait_s
        self.tasks_per_agent_target = max(1, tasks_per_agent_target)
        self.min_agents_per_type = max(0, min_agents_per_type)
        self.max_agents_per_type = max(1, max_agents_per_type)
        self.cooldown_s = max(0.0, cooldown_s)
        self._last_change_at: dict[str, float] = {}

    def evaluate(
        self,
        snap: BusMetricsSnapshot,
        task_types: Optional[list[TaskType | str]] = None,
        now: Optional[float] = None,
    ) -> list[ScaleRecommendation]:
        cur_now = now if now is not None else time.time()
        targets = [_norm_type(t) for t in task_types] if task_types else sorted(snap.queue_depth_by_type.keys())
        out: list[ScaleRecommendation] = []

        for tp in targets:
            q = snap.queue_depth_by_type.get(tp, 0)
            live = snap.live_agents_by_type.get(tp, 0)
            wait_s = snap.median_wait_s_by_type.get(tp, 0.0)
            last_ts = self._last_change_at.get(tp)
            in_cooldown = last_ts is not None and (cur_now - last_ts) < self.cooldown_s

            if not in_cooldown and (q >= self.scale_up_queue_depth or (q > 0 and wait_s >= self.scale_up_wait_s) or (q > 0 and live == 0)):
                needed = max(live + 1, (q + self.tasks_per_agent_target - 1) // self.tasks_per_agent_target)
                rec_agents = min(self.max_agents_per_type, max(1, needed))
                if rec_agents > live:
                    self._last_change_at[tp] = cur_now
                    delta = rec_agents - live
                    cmd = (
                        f"# [PROPOSE ONLY] Start {delta} additional BusWorker instance(s) for {tp}"
                        if tp != TaskType.TEXT_GENERATE.value
                        else f"# [PROPOSE ONLY] Start {delta} BusWorker(s) for {tp} or review POST /api/cluster/topology mode A (split) for higher aggregate tok/s"
                    )
                    out.append(
                        ScaleRecommendation(
                            task_type=tp,
                            action="scale_up",
                            current_agents=live,
                            recommended_agents=rec_agents,
                            queue_depth=q,
                            median_wait_s=wait_s,
                            reason=f"queue_depth={q} (>= {self.scale_up_queue_depth}) or median_wait_s={wait_s:.2f}s (>= {self.scale_up_wait_s:.2f}s) with live_agents={live}",
                            suggested_command=cmd,
                            requires_human_approval=True,
                            applied=False,
                        )
                    )
                    continue

            if not in_cooldown and q <= self.scale_down_queue_depth and wait_s <= self.scale_down_wait_s and live > max(1, self.min_agents_per_type):
                rec_agents = max(self.min_agents_per_type, live - 1)
                self._last_change_at[tp] = cur_now
                out.append(
                    ScaleRecommendation(
                        task_type=tp,
                        action="scale_down",
                        current_agents=live,
                        recommended_agents=rec_agents,
                        queue_depth=q,
                        median_wait_s=wait_s,
                        reason=f"queue_depth={q} (<= {self.scale_down_queue_depth}) and median_wait_s={wait_s:.2f}s (<= {self.scale_down_wait_s:.2f}s) with live_agents={live}",
                        suggested_command=f"# [PROPOSE ONLY] Drain 1 idle BusWorker for {tp} via NATS subject {CONTROL_SUBJECT}.<agent_id>",
                        requires_human_approval=True,
                        applied=False,
                    )
                )
                continue

            reason = "cooldown active" if in_cooldown else "within hysteresis band"
            out.append(
                ScaleRecommendation(
                    task_type=tp,
                    action="noop",
                    current_agents=live,
                    recommended_agents=live,
                    queue_depth=q,
                    median_wait_s=wait_s,
                    reason=reason,
                    suggested_command="",
                    requires_human_approval=True,
                    applied=False,
                )
            )
        return out
