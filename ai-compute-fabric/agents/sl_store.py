"""Blob store and 'started' ledger for the serverless runtime (ADR-003 D7/D8).

* Blob store: content-addressed (key = sha256 of the bytes), for bundles/args/results above the 64 KiB inline limit. The
  NATS Object Store backend is capped at 8 MiB per blob (measured: 8 MiB get = 16 MB/s, 512 MiB get = 389 s): anything larger
  belongs on a Volume, not here.
* Started ledger: `mark_started(call_id, attempt)` is an atomic create. The worker calls it right before running user code; a
  second delivery of the same call (JetStream redelivery after the first worker died) finds the key and must NOT run it
  again (at-most-once after start)."""
from __future__ import annotations

import asyncio
import hashlib
import io
import threading

BLOB_BUCKET = "AICF_SL_BLOBS"
STARTED_BUCKET = "AICF_SL_STARTED"
MAX_INLINE_BYTES = 64 * 1024
MAX_BLOB_BYTES = 8 * 1024 * 1024


class BlobError(RuntimeError):
    pass


class MemoryBlobStore:
    def __init__(self):
        self._d: dict = {}

    def put(self, data: bytes) -> str:
        if len(data) > MAX_BLOB_BYTES:
            raise BlobError(f"blob of {len(data)} bytes exceeds {MAX_BLOB_BYTES} (use a Volume for large data)")
        h = hashlib.sha256(data).hexdigest()
        self._d[h] = data
        return h

    def get(self, ref: str) -> bytes:
        data = self._d.get(ref)
        if data is None:
            raise BlobError(f"blob {ref[:12]} not found")
        if hashlib.sha256(data).hexdigest() != ref:
            raise BlobError("blob content does not match its hash")
        return data


class MemoryStartedLedger:
    def __init__(self):
        self._s, self._mu = set(), threading.Lock()

    def mark_started(self, call_id: str, attempt: int) -> bool:
        with self._mu:
            k = f"{call_id}.{attempt}"
            if k in self._s:
                return False
            self._s.add(k)
            return True


class NatsStore:
    """Blob store + started ledger over one NATS connection owned by a private event-loop thread (sync API)."""

    def __init__(self, url: str, identity: str = "sl-store", tls_config=None, security_policy=None, started_ttl_s: float = 3600.0,
                 blob_ttl_s: float = 3600.0, blob_max_bytes: int = 256 * 1024**2):
        from agents.task_bus import _connect_nats
        self.loop = asyncio.new_event_loop()
        self._t = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._t.start()
        self.nc = self._run(_connect_nats(url, identity, tls_config, security_policy))
        js = self.nc.jetstream()
        self._js = js
        self._blobs = self._run(self._ensure_obj(js, blob_ttl_s, blob_max_bytes))
        self._started = self._run(self._ensure_kv(js, started_ttl_s))

    @staticmethod
    async def _ensure_obj(js, ttl, max_bytes):
        from nats.js.api import ObjectStoreConfig
        from nats.js.errors import BucketNotFoundError, NotFoundError
        try:
            return await js.object_store(BLOB_BUCKET)
        except (BucketNotFoundError, NotFoundError):
            return await js.create_object_store(BLOB_BUCKET, config=ObjectStoreConfig(bucket=BLOB_BUCKET, ttl=ttl, max_bytes=max_bytes))

    @staticmethod
    async def _ensure_kv(js, ttl):
        from nats.js.api import KeyValueConfig
        from nats.js.errors import BucketNotFoundError, NotFoundError
        try:
            return await js.key_value(STARTED_BUCKET)
        except (BucketNotFoundError, NotFoundError):
            return await js.create_key_value(config=KeyValueConfig(bucket=STARTED_BUCKET, ttl=ttl))

    def _run(self, coro, timeout: float = 60.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    # blob API
    def put(self, data: bytes) -> str:
        if len(data) > MAX_BLOB_BYTES:
            raise BlobError(f"blob of {len(data)} bytes exceeds {MAX_BLOB_BYTES} (use a Volume for large data)")
        h = hashlib.sha256(data).hexdigest()
        self._run(self._blobs.put(h, data))
        return h

    def get(self, ref: str) -> bytes:
        async def _g():
            buf = io.BytesIO()
            await self._blobs.get(ref, writeinto=buf)
            return buf.getvalue()
        try:
            data = self._run(_g())
        except Exception as e:
            raise BlobError(f"blob {ref[:12]} unavailable: {type(e).__name__}") from e
        if hashlib.sha256(data).hexdigest() != ref:
            raise BlobError("blob content does not match its hash")
        return data

    # ledger API
    def mark_started(self, call_id: str, attempt: int) -> bool:
        from nats.js.errors import KeyWrongLastSequenceError
        try:
            self._run(self._started.create(f"{call_id}.{attempt}", b"1"))
            return True
        except KeyWrongLastSequenceError:
            return False

    def serve_direct(self, subject: str, handler, max_concurrent: int, max_bytes: int = 256 * 1024) -> None:
        """Core NATS request/reply endpoint. `handler(request_dict) -> reply_dict` is blocking and runs in a thread; when
        `max_concurrent` calls are already running the reply is {"ok": False, "error": "busy"} so the caller can use the queue."""
        import json
        gate = threading.BoundedSemaphore(max_concurrent)

        async def on_msg(m):
            if len(m.data) > max_bytes:
                return await m.respond(json.dumps({"ok": False, "error": "TaskTooLargeError: direct payload too large"}).encode())
            if not gate.acquire(blocking=False):
                return await m.respond(b'{"ok":false,"error":"busy"}')
            try:
                reply = await asyncio.get_running_loop().run_in_executor(None, handler, json.loads(m.data))
            except Exception as e:
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            finally:
                gate.release()
            await m.respond(json.dumps(reply).encode())

        self._run(self.nc.subscribe(subject, cb=on_msg))

    def close(self) -> None:
        try:
            self._run(self.nc.close(), 5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
