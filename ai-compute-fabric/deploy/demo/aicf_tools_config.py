"""Runtime settings of the AICF tools router, shared by the router (reads) and the admin settings service (reads + writes).
File: AICF_TOOLS_CONFIG (default /mnt/Data/aicf-tools/config.json). Re-read when its mtime changes, so a change applies to the next chat request, no restart.
Python standard library only."""
from __future__ import annotations

import json
import os
import tempfile
import time

PATH = os.environ.get("AICF_TOOLS_CONFIG", "/mnt/Data/aicf-tools/config.json")

TOOL_INFO = {  # name -> (label, description, uses_langflow)
    "datetime": ("Ngày giờ", "Cho mô hình biết hôm nay là ngày nào (tự thêm khi có câu hỏi về thời gian hoặc khi công cụ khác chạy).", False),
    "weather": ("Thời tiết", "Open-Meteo: hiện tại và dự báo vài ngày theo địa danh.", False),
    "rates": ("Tỷ giá, tiền điện tử, vàng thế giới", "Vietcombank, open.er-api.com, CoinGecko, gold-api.com.", False),
    "calc": ("Tính toán và đổi đơn vị", "Tính chính xác bằng máy tính hệ thống thay vì để mô hình tự nhẩm.", False),
    "url": ("Đọc đường link", "Tải trang web công khai người dùng dán vào (chặn địa chỉ nội bộ).", False),
    "news": ("Tin tức (gọi sang Langflow)", "Gọi flow Langflow → Google News. Tắt mục này là KHÔNG gọi Langflow nữa; cũng áp dụng cho giá xăng, chứng khoán và tiền tố /tin.", True),
    "web": ("Tra cứu Wikipedia", "Chỉ chạy khi người dùng nói \"tra cứu/tìm kiếm\" hoặc dùng /web.", False),
}
DEFAULTS = {"enabled": True, "footer": True, "tools": {n: True for n in TOOL_INFO}}

_cache: tuple[float, dict] | None = None


def _normalise(raw) -> dict:
    out = {"enabled": True, "footer": True, "tools": dict(DEFAULTS["tools"])}
    if isinstance(raw, dict):
        for k in ("enabled", "footer"):
            if isinstance(raw.get(k), bool):
                out[k] = raw[k]
        if isinstance(raw.get("tools"), dict):
            for n in TOOL_INFO:
                if isinstance(raw["tools"].get(n), bool):
                    out["tools"][n] = raw["tools"][n]
        for k in ("updated_at", "updated_by"):
            if isinstance(raw.get(k), str):
                out[k] = raw[k][:80]
    return out


def load() -> dict:
    """Current settings; any problem with the file yields the defaults (everything on), never an exception."""
    global _cache
    try:
        m = os.stat(PATH).st_mtime
    except OSError:
        return _normalise(None)
    if _cache and _cache[0] == m:
        return _cache[1]
    try:
        with open(PATH, encoding="utf-8") as f:
            cfg = _normalise(json.load(f))
    except (OSError, ValueError):
        return _normalise(None)
    _cache = (m, cfg)
    return cfg


def save(new: dict, who: str) -> dict:
    """Validate (unknown keys are dropped, non-booleans ignored) and write atomically. Returns what was stored."""
    global _cache
    cfg = _normalise(new)
    cfg["updated_at"], cfg["updated_by"] = time.strftime("%H:%M %d/%m/%Y"), who[:80]
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PATH), prefix=".cfg-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, PATH)
    _cache = None
    return cfg


def tool_on(name: str) -> bool:
    c = load()
    return c["enabled"] and c["tools"].get(name, True)
