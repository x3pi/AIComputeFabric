"""HTTP client for /api/serverless/v1 (stdlib only). Config: AICF_GATEWAY_URL, AICF_API_KEY."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from aicf import errors as E


class Client:
    def __init__(self, url: str | None = None, api_key: str | None = None, timeout: float = 700.0):
        self.url = (url or os.environ.get("AICF_GATEWAY_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("AICF_API_KEY", "")
        self.timeout = timeout
        if not self.url:
            raise E.ConfigError("AICF_GATEWAY_URL is not set (example: export AICF_GATEWAY_URL=http://gateway.lan:18080)")
        if not self.api_key:
            raise E.ConfigError("AICF_API_KEY is not set (ask your administrator for a tenant API key: it starts with 'aicf_')")

    def post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + "/api/serverless/v1" + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                err = json.loads(raw)["error"]
                code, msg = err.get("code", ""), err.get("message", "")
            except Exception:
                code, msg = "", raw.decode(errors="replace")[:300]
            retry = int(e.headers.get("Retry-After", "0") or 0)
            raise self._map(e.code, code, msg, retry) from None
        except (urllib.error.URLError, OSError) as e:
            raise E.ConnectionFailed(f"cannot reach {self.url}: {getattr(e, 'reason', e)}. Check AICF_GATEWAY_URL and that the gateway is running "
                                     f"(`aicf doctor`).") from None

    @staticmethod
    def _map(status: int, code: str, msg: str, retry: int) -> Exception:
        if status == 401:
            return E.AuthError("the gateway rejected your API key (missing, wrong, rotated or the tenant is disabled). Check AICF_API_KEY.")
        if code == "serverless_disabled":
            return E.AuthError("serverless is not enabled for your tenant: ask your administrator to enable it.")
        if code in ("gpu_denied", "data_class_denied"):
            return E.QuotaExceeded(code, msg)
        if status == 429:
            return E.QuotaExceeded(code, f"{msg} (retry after {retry}s)" if retry else msg, retry)
        if status == 503:
            return E.NoCapacity(f"{msg}. Retry in {retry or 5}s; if it persists run `aicf doctor`.", retry or 5)
        if status in (400, 413):
            return E.InvalidRequest(msg)
        return E.ServerError(f"gateway returned {status}: {msg}")
