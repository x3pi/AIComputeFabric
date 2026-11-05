"""Signing and replay protection for SERVERLESS_EXEC (ADR-003 T3).

HMAC-SHA256 over the canonical JSON of EVERYTHING that influences execution (identity, function, env, resources, limits,
data hashes, nonce, expiry). It proves the call was produced by the serverless service and was not modified or replayed on the
bus. It does NOT make the payload safe: safety comes from the sandbox. The key file (0600, >= 32 bytes per line) may hold
several lines: the first signs, all verify (rotation without downtime)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from agents.tasks import ServerlessExecIn, ServerlessSig

SIGNED_EXCLUDE = {"sig", "bundle_b64", "bundle_ref", "args_b64", "args_ref"}


class SignatureError(PermissionError):
    """The call is not authentic, was modified, expired, or replayed."""


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_keys(path: str | Path) -> list:
    p = Path(path)
    mode = p.stat().st_mode & 0o077
    if mode:
        raise SignatureError(f"{p}: key file must not be readable by group/others (chmod 600)")
    keys = [ln.strip().encode() for ln in p.read_text().splitlines() if ln.strip()]
    if not keys or any(len(k) < 32 for k in keys):
        raise SignatureError(f"{p}: needs at least one key of >= 32 characters")
    return keys


def new_key_file(path: str | Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.urlsafe_b64encode(os.urandom(48)).decode() + "\n")


def _data_sha(inline_b64: Optional[str], ref: Optional[str]) -> str:
    if ref:
        return ref
    return sha256_hex(base64.b64decode(inline_b64 or ""))


def canonical(inp: ServerlessExecIn, attempt: int) -> bytes:
    d = inp.model_dump(mode="json", exclude=SIGNED_EXCLUDE)
    d["_args_sha256"] = _data_sha(inp.args_b64, inp.args_ref)
    d["_attempt"] = attempt
    d["_nonce"], d["_exp"] = inp.sig.nonce, inp.sig.exp
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()


def sign(keys: list, inp_without_sig: dict, attempt: int = 1, ttl_s: float = 120.0, now: Optional[float] = None) -> ServerlessExecIn:
    """Takes the call as a dict WITHOUT `sig` and returns the validated, signed model."""
    now = time.time() if now is None else now
    sig = ServerlessSig(nonce=uuid.uuid4().hex, exp=now + ttl_s, mac="0" * 64)
    inp = ServerlessExecIn.model_validate({**inp_without_sig, "sig": sig.model_dump()})
    mac = hmac.new(keys[0], canonical(inp, attempt), hashlib.sha256).hexdigest()
    return inp.model_copy(update={"sig": sig.model_copy(update={"mac": mac})})


class NonceCache:
    def __init__(self, max_entries: int = 100_000):
        self.max = max_entries
        self._d: OrderedDict[str, float] = OrderedDict()
        self._mu = threading.Lock()

    def discard(self, nonce: str) -> None:
        with self._mu:
            self._d.pop(nonce, None)

    def check_and_add(self, nonce: str, exp: float, now: float) -> bool:
        with self._mu:
            while self._d and (len(self._d) >= self.max or next(iter(self._d.values())) < now):
                self._d.popitem(last=False)
            if nonce in self._d:
                return False
            self._d[nonce] = exp
            return True


class Verifier:
    def __init__(self, keys: list, clock=time.time, skew_s: float = 5.0):
        self.keys, self.clock, self.skew = keys, clock, skew_s
        self.nonces = NonceCache()

    def release(self, inp: ServerlessExecIn) -> None:
        """Forget the nonce of a call that was verified but did NOT run (the message will be redelivered to a worker)."""
        self.nonces.discard(inp.sig.nonce)

    def verify(self, inp: ServerlessExecIn, attempt: int = 1) -> None:
        now = self.clock()
        body = canonical(inp, attempt)
        ok = False
        for k in self.keys:                       # no early exit: constant work per key
            ok |= hmac.compare_digest(hmac.new(k, body, hashlib.sha256).hexdigest(), inp.sig.mac)
        if not ok:
            raise SignatureError("bad signature")
        if now > inp.sig.exp + self.skew:
            raise SignatureError("call expired")
        if not self.nonces.check_and_add(inp.sig.nonce, inp.sig.exp, now):
            raise SignatureError("replayed nonce")
