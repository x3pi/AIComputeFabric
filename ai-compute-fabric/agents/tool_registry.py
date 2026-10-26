"""Signed tool registry (plan P5.2).

An agent can only call a tool that is (1) listed in a manifest signed by the administrator's
Ed25519 key, (2) in scope for the calling tenant, (3) called with arguments that satisfy the tool's
JSON Schema, and (4) run inside a sandbox. There is no tool that runs arbitrary commands, and the
registry refuses to register one even if a signed manifest asks for it."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agents.sandbox import Sandbox, SandboxError, SandboxLimits

# Names (or name fragments) of capabilities that must never be offered to a model.
FORBIDDEN = re.compile(r"(shell|bash|\bsh\b|zsh|cmd|powershell|exec|eval|system|subprocess|popen|spawn|terminal|command|run_code|python_repl)", re.I)


class ToolDenied(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    description: str
    input_schema: dict
    output_schema: dict
    tenant_scope: tuple            # tenant ids allowed to call it; ("*",) = every tenant
    entry: str                     # file name inside the tools directory
    entry_sha256: str
    timeout_s: float = 10.0
    memory_mb: int = 128

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["tenant_scope"] = list(self.tenant_scope)
        return d


def canonical(manifest: list) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_manifest(specs: list, private_key: Ed25519PrivateKey) -> tuple[list, bytes]:
    m = [s.to_dict() for s in specs]
    return m, private_key.sign(canonical(m))


def file_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@dataclass
class ToolRegistry:
    tools_dir: Path
    admin_public_key: Ed25519PublicKey
    tools: dict = field(default_factory=dict)
    audit: list = field(default_factory=list)

    def load(self, manifest: list, signature: bytes) -> None:
        try:
            self.admin_public_key.verify(signature, canonical(manifest))
        except InvalidSignature:
            raise ToolDenied("manifest_signature_invalid") from None
        tools = {}
        for d in manifest:
            spec = ToolSpec(**{**d, "tenant_scope": tuple(d["tenant_scope"])})
            if FORBIDDEN.search(spec.name) or FORBIDDEN.search(spec.description.split(".")[0]):
                raise ToolDenied("forbidden_tool", f"{spec.name!r} looks like a command-execution capability")
            if "/" in spec.entry or "\\" in spec.entry or spec.entry.startswith("."):
                raise ToolDenied("bad_entry_path", spec.entry)
            p = self.tools_dir / spec.entry
            if not p.is_file() or file_sha256(p) != spec.entry_sha256:
                raise ToolDenied("tool_file_tampered", spec.name)
            for sch in (spec.input_schema, spec.output_schema):
                jsonschema.Draft202012Validator.check_schema(sch)
            if spec.input_schema.get("additionalProperties") is not False:
                raise ToolDenied("input_schema_must_forbid_extra_properties", spec.name)
            if spec.name in tools:
                raise ToolDenied("duplicate_tool", spec.name)
            tools[spec.name] = spec
        self.tools = tools

    def describe(self, tenant: str) -> list:
        """What a model is told it may call: only tools in the tenant's scope."""
        return [{"name": s.name, "description": s.description, "parameters": s.input_schema}
                for s in self.tools.values() if self._in_scope(s, tenant)]

    @staticmethod
    def _in_scope(spec: ToolSpec, tenant: str) -> bool:
        return "*" in spec.tenant_scope or tenant in spec.tenant_scope

    def invoke(self, tenant: str, name: str, args: dict, sandbox: Sandbox) -> dict:
        try:
            return self._invoke(tenant, name, args, sandbox)
        except ToolDenied as e:
            self.audit.append({"tenant": tenant, "tool": name, "outcome": "denied", "reason": e.reason})
            raise
        except SandboxError as e:
            self.audit.append({"tenant": tenant, "tool": name, "outcome": "sandbox_error", "reason": str(e)[:120]})
            raise

    def _invoke(self, tenant: str, name: str, args: dict, sandbox: Sandbox) -> dict:
        spec = self.tools.get(name) if isinstance(name, str) else None
        if spec is None:
            raise ToolDenied("unknown_tool", str(name)[:60])
        if not self._in_scope(spec, tenant):
            raise ToolDenied("tenant_not_in_scope", f"{tenant!r} may not call {name!r}")
        if not isinstance(args, dict):
            raise ToolDenied("bad_arguments", "arguments must be an object")
        try:
            jsonschema.validate(args, spec.input_schema)
        except jsonschema.ValidationError as e:
            raise ToolDenied("arguments_violate_schema", e.message[:120]) from None
        script = self.tools_dir / spec.entry
        if file_sha256(script) != spec.entry_sha256:   # re-check at call time: the file may change after load
            raise ToolDenied("tool_file_tampered", name)
        out = sandbox.run(script, args, SandboxLimits(timeout_s=spec.timeout_s, memory_mb=spec.memory_mb))
        try:
            jsonschema.validate(out, spec.output_schema)
        except jsonschema.ValidationError as e:
            raise ToolDenied("tool_output_violates_schema", e.message[:120]) from None
        self.audit.append({"tenant": tenant, "tool": name, "outcome": "ok"})
        return out
