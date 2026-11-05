"""Warm runner: runs INSIDE the sandbox container (python -I -B /aicf/runner.py). The ONLY place that calls cloudpickle.loads.

Protocol (over the container's stdin/stdout, length-prefixed frames, big-endian uint32 length + JSON):
  request  {"op":"ping"} | {"op":"shutdown"} |
           {"op":"call","call_id","sha","bundle_b64"?,"method"?,"args_b64","args_format":"cloudpickle"|"json",
            "result_format":"cloudpickle"|"json","max_output_bytes"?}
  response {"ok":true,"result_b64","format","stdout","stderr","truncated","exec_ms","loaded","enter_ms"}
        or {"ok":false,"error":{"type","message","traceback"},"stdout","stderr", ...}
The protocol channel is a private dup of the original fd 0/1; fd 0/1 are re-pointed at /dev/null so user code (and C
extensions) can neither read requests nor corrupt responses. User print() is captured into the response."""
import base64
import hashlib
import io
import json
import os
import select
import struct
import sys
import threading
import time
import traceback

MAX_FRAME = int(os.environ.get("AICF_MAX_FRAME", str(16 * 1024 * 1024)))
VRAM_MB = int(os.environ.get("AICF_VRAM_MB", "0") or 0)

_in = os.fdopen(os.dup(0), "rb", buffering=0)
_out = os.fdopen(os.dup(1), "wb", buffering=0)
_null_r = os.open(os.devnull, os.O_RDONLY)
_null_w = os.open(os.devnull, os.O_WRONLY)
os.dup2(_null_r, 0)
os.dup2(_null_w, 1)


def _read_exact(n):
    buf = bytearray()
    while len(buf) < n:
        chunk = _in.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _recv():
    h = _read_exact(4)
    if h is None:
        return None
    (n,) = struct.unpack(">I", h)
    if n > MAX_FRAME:
        raise ValueError(f"frame of {n} bytes exceeds {MAX_FRAME}")
    b = _read_exact(n)
    if b is None:
        return None
    return json.loads(b)


def _send(obj):
    b = json.dumps(obj, separators=(",", ":")).encode()
    if len(b) > MAX_FRAME:
        b = json.dumps({"ok": False, "error": {"type": "ResultTooLarge", "message": f"result frame {len(b)} bytes exceeds {MAX_FRAME}", "traceback": ""}}).encode()
    _out.write(struct.pack(">I", len(b)) + b)


class _Capped(io.TextIOBase):
    def __init__(self, limit):
        self.limit, self.n, self.parts, self.truncated = limit, 0, [], False

    def writable(self):
        return True

    def write(self, s):
        if self.n < self.limit:
            take = s[: self.limit - self.n]
            self.parts.append(take)
            self.n += len(take)
            if len(take) < len(s):
                self.truncated = True
        else:
            self.truncated = True
        return len(s)

    def value(self):
        return "".join(self.parts)


_LOADED: dict[str, object] = {}
_VRAM_CAPPED = False

_PTX_SAXPY = b"""
.version 7.0
.target sm_50
.address_size 64

.visible .entry saxpy_kernel(
    .param .f32 param_a,
    .param .u64 param_x,
    .param .u64 param_y,
    .param .u32 param_n
)
{
    .reg .pred %p<2>;
    .reg .f32 %f<4>;
    .reg .b32 %r<5>;
    .reg .b64 %rd<8>;

    ld.param.f32 %f1, [param_a];
    ld.param.u64 %rd1, [param_x];
    ld.param.u64 %rd2, [param_y];
    ld.param.u32 %r1, [param_n];

    mov.u32 %r2, %ctaid.x;
    mov.u32 %r3, %ntid.x;
    mov.u32 %r4, %tid.x;
    mad.lo.s32 %r2, %r2, %r3, %r4;
    setp.ge.u32 %p1, %r2, %r1;
    @%p1 bra DONE;

    mul.wide.u32 %rd3, %r2, 4;
    add.s64 %rd4, %rd1, %rd3;
    add.s64 %rd5, %rd2, %rd3;
    ld.global.f32 %f2, [%rd4];
    ld.global.f32 %f3, [%rd5];
    fma.rn.f32 %f3, %f1, %f2, %f3;
    st.global.f32 [%rd5], %f3;
DONE:
    ret;
}
"""

_PTX_MATMUL = b"""
.version 7.0
.target sm_50
.address_size 64

.visible .entry matmul_kernel(
    .param .u64 param_A,
    .param .u64 param_B,
    .param .u64 param_C,
    .param .u32 param_M,
    .param .u32 param_N,
    .param .u32 param_K
)
{
    .reg .pred %p<3>;
    .reg .f32 %f<5>;
    .reg .b32 %r<14>;
    .reg .b64 %rd<12>;

    ld.param.u64 %rd1, [param_A];
    ld.param.u64 %rd2, [param_B];
    ld.param.u64 %rd3, [param_C];
    ld.param.u32 %r1, [param_M];
    ld.param.u32 %r2, [param_N];
    ld.param.u32 %r3, [param_K];

    mov.u32 %r4, %ctaid.y;
    mov.u32 %r5, %ntid.y;
    mov.u32 %r6, %tid.y;
    mad.lo.s32 %r7, %r4, %r5, %r6;

    mov.u32 %r8, %ctaid.x;
    mov.u32 %r9, %ntid.x;
    mov.u32 %r10, %tid.x;
    mad.lo.s32 %r11, %r8, %r9, %r10;

    setp.ge.u32 %p1, %r7, %r1;
    @%p1 bra EXIT;
    setp.ge.u32 %p1, %r11, %r2;
    @%p1 bra EXIT;

    mov.f32 %f1, 0f00000000;
    mov.u32 %r12, 0;

LOOP:
    setp.ge.u32 %p2, %r12, %r3;
    @%p2 bra STORE;

    mad.lo.u32 %r13, %r7, %r3, %r12;
    mul.wide.u32 %rd4, %r13, 4;
    add.s64 %rd5, %rd1, %rd4;
    ld.global.f32 %f2, [%rd5];

    mad.lo.u32 %r13, %r12, %r2, %r11;
    mul.wide.u32 %rd6, %r13, 4;
    add.s64 %rd7, %rd2, %rd6;
    ld.global.f32 %f3, [%rd7];

    fma.rn.f32 %f1, %f2, %f3, %f1;
    add.u32 %r12, %r12, 1;
    bra LOOP;

STORE:
    mad.lo.u32 %r13, %r7, %r2, %r11;
    mul.wide.u32 %rd8, %r13, 4;
    add.s64 %rd9, %rd3, %rd8;
    st.global.f32 [%rd9], %f1;

EXIT:
    ret;
}
"""


def _install_aicf_gpu_module() -> None:
    """Install `aicf_gpu` helper module backed by real `libcuda.so.1` CUDA Driver API + PTX kernels."""
    import ctypes
    import math
    import subprocess
    import types

    mod = types.ModuleType("aicf_gpu")

    def _open_cuda():
        for libname in ("libcuda.so.1", "libcuda.so"):
            try:
                lib = ctypes.CDLL(libname)
                if lib.cuInit(0) == 0:
                    return lib
            except Exception:
                pass
        return None

    def device_info() -> dict:
        info: dict = {
            "cuda_available": False,
            "device_name": "CPU Sandbox",
            "total_vram_mib": 0,
            "free_vram_mib": 0,
            "used_vram_mib": 0,
            "temperature_c": 0,
            "driver_version": "",
            "lease_vram_cap_mib": VRAM_MB,
            "nvidia_visible_devices": os.environ.get("NVIDIA_VISIBLE_DEVICES", "cpu"),
        }
        try:
            smi = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,temperature.gpu,driver_version",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=3,
            ).strip().splitlines()
            if smi:
                parts = [p.strip() for p in smi[0].split(",")]
                if len(parts) >= 6:
                    info["cuda_available"] = True
                    info["device_name"] = parts[0]
                    info["total_vram_mib"] = int(parts[1])
                    info["used_vram_mib"] = int(parts[2])
                    info["free_vram_mib"] = int(parts[3])
                    info["temperature_c"] = int(parts[4])
                    info["driver_version"] = parts[5]
        except Exception:
            pass
        cuda = _open_cuda()
        if cuda is not None:
            info["cuda_available"] = True
            dev = ctypes.c_int()
            if cuda.cuDeviceGet(ctypes.byref(dev), 0) == 0:
                nbuf = ctypes.create_string_buffer(128)
                if cuda.cuDeviceGetName(nbuf, 128, dev) == 0:
                    info["device_name"] = nbuf.value.decode("utf-8", errors="replace")
                ctx = ctypes.c_void_p()
                if cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev) == 0:
                    fb, tb = ctypes.c_size_t(), ctypes.c_size_t()
                    if cuda.cuMemGetInfo_v2(ctypes.byref(fb), ctypes.byref(tb)) == 0:
                        info["free_vram_mib"] = fb.value // (1024 * 1024)
                        info["total_vram_mib"] = tb.value // (1024 * 1024)
                        info["used_vram_mib"] = info["total_vram_mib"] - info["free_vram_mib"]
                    cuda.cuCtxDestroy_v2(ctx)
        info["vram_free_mb"] = info["free_vram_mib"]
        info["vram_total_mb"] = info["total_vram_mib"]
        return info

    def saxpy(a: float, x: list[float], y: list[float]) -> dict:
        n = min(len(x), len(y))
        if n == 0:
            return {"output": [], "elements": 0, "gpu_kernel_ms": 0.0, "kernel_ms": 0.0, "backend": "empty"}
        cuda = _open_cuda()
        if cuda is not None:
            dev = ctypes.c_int()
            ctx = ctypes.c_void_p()
            if cuda.cuDeviceGet(ctypes.byref(dev), 0) == 0 and cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev) == 0:
                try:
                    cmod, cfn = ctypes.c_void_p(), ctypes.c_void_p()
                    cuda.cuModuleLoadData(ctypes.byref(cmod), _PTX_SAXPY)
                    cuda.cuModuleGetFunction(ctypes.byref(cfn), cmod, b"saxpy_kernel")
                    Floats = ctypes.c_float * n
                    hx, hy = Floats(*[float(v) for v in x[:n]]), Floats(*[float(v) for v in y[:n]])
                    blen = n * 4
                    dx, dy = ctypes.c_uint64(), ctypes.c_uint64()
                    cuda.cuMemAlloc_v2(ctypes.byref(dx), ctypes.c_size_t(blen))
                    cuda.cuMemAlloc_v2(ctypes.byref(dy), ctypes.c_size_t(blen))
                    cuda.cuMemcpyHtoD_v2(dx, ctypes.byref(hx), ctypes.c_size_t(blen))
                    cuda.cuMemcpyHtoD_v2(dy, ctypes.byref(hy), ctypes.c_size_t(blen))
                    aa, ax, ay, an = ctypes.c_float(float(a)), ctypes.c_uint64(dx.value), ctypes.c_uint64(dy.value), ctypes.c_uint32(n)
                    kargs = (ctypes.c_void_p * 4)(
                        ctypes.cast(ctypes.byref(aa), ctypes.c_void_p),
                        ctypes.cast(ctypes.byref(ax), ctypes.c_void_p),
                        ctypes.cast(ctypes.byref(ay), ctypes.c_void_p),
                        ctypes.cast(ctypes.byref(an), ctypes.c_void_p),
                    )
                    t0 = time.perf_counter()
                    threads = 256
                    blocks = (n + threads - 1) // threads
                    cuda.cuLaunchKernel(cfn, blocks, 1, 1, threads, 1, 1, 0, None, kargs, None)
                    cuda.cuCtxSynchronize()
                    gms = round((time.perf_counter() - t0) * 1000.0, 4)
                    cuda.cuMemcpyDtoH_v2(ctypes.byref(hy), dy, ctypes.c_size_t(blen))
                    cuda.cuMemFree_v2(dx)
                    cuda.cuMemFree_v2(dy)
                    cuda.cuModuleUnload(cmod)
                    return {"output": [round(float(v), 5) for v in hy], "elements": n, "gpu_kernel_ms": gms, "kernel_ms": gms, "backend": "cuda-ptx-sm50"}
                finally:
                    cuda.cuCtxDestroy_v2(ctx)
        t0 = time.perf_counter()
        out = [round(float(a) * float(x[i]) + float(y[i]), 5) for i in range(n)]
        gms = round((time.perf_counter() - t0) * 1000.0, 4)
        return {"output": out, "elements": n, "gpu_kernel_ms": gms, "kernel_ms": gms, "backend": "cpu-fallback"}

    def matmul(a_mat: list[list[float]], b_mat: list[list[float]]) -> dict:
        m = len(a_mat)
        k = len(a_mat[0]) if m else 0
        n = len(b_mat[0]) if b_mat else 0
        if m == 0 or k == 0 or n == 0 or len(b_mat) != k:
            raise ValueError(f"invalid matrix dimensions for matmul: ({m}x{k}) * ({len(b_mat)}x{n})")
        flat_a = [float(x) for row in a_mat for x in row]
        flat_b = [float(x) for row in b_mat for x in row]
        cuda = _open_cuda()
        if cuda is not None:
            dev = ctypes.c_int()
            ctx = ctypes.c_void_p()
            if cuda.cuDeviceGet(ctypes.byref(dev), 0) == 0 and cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev) == 0:
                try:
                    cmod, cfn = ctypes.c_void_p(), ctypes.c_void_p()
                    cuda.cuModuleLoadData(ctypes.byref(cmod), _PTX_MATMUL)
                    cuda.cuModuleGetFunction(ctypes.byref(cfn), cmod, b"matmul_kernel")
                    hA = (ctypes.c_float * (m * k))(*flat_a)
                    hB = (ctypes.c_float * (k * n))(*flat_b)
                    hC = (ctypes.c_float * (m * n))()
                    dA, dB, dC = ctypes.c_uint64(), ctypes.c_uint64(), ctypes.c_uint64()
                    cuda.cuMemAlloc_v2(ctypes.byref(dA), ctypes.c_size_t(m * k * 4))
                    cuda.cuMemAlloc_v2(ctypes.byref(dB), ctypes.c_size_t(k * n * 4))
                    cuda.cuMemAlloc_v2(ctypes.byref(dC), ctypes.c_size_t(m * n * 4))
                    cuda.cuMemcpyHtoD_v2(dA, ctypes.byref(hA), ctypes.c_size_t(m * k * 4))
                    cuda.cuMemcpyHtoD_v2(dB, ctypes.byref(hB), ctypes.c_size_t(k * n * 4))
                    aA, aB, aC = ctypes.c_uint64(dA.value), ctypes.c_uint64(dB.value), ctypes.c_uint64(dC.value)
                    aM, aN, aK = ctypes.c_uint32(m), ctypes.c_uint32(n), ctypes.c_uint32(k)
                    kargs = (ctypes.c_void_p * 6)(
                        ctypes.cast(ctypes.byref(aA), ctypes.c_void_p),
                        ctypes.cast(ctypes.byref(aB), ctypes.c_void_p),
                        ctypes.cast(ctypes.byref(aC), ctypes.c_void_p),
                        ctypes.cast(ctypes.byref(aM), ctypes.c_void_p),
                        ctypes.cast(ctypes.byref(aN), ctypes.c_void_p),
                        ctypes.cast(ctypes.byref(aK), ctypes.c_void_p),
                    )
                    t0 = time.perf_counter()
                    bx, by = (n + 15) // 16, (m + 15) // 16
                    cuda.cuLaunchKernel(cfn, bx, by, 1, 16, 16, 1, 0, None, kargs, None)
                    cuda.cuCtxSynchronize()
                    gms = round((time.perf_counter() - t0) * 1000.0, 4)
                    cuda.cuMemcpyDtoH_v2(ctypes.byref(hC), dC, ctypes.c_size_t(m * n * 4))
                    cuda.cuMemFree_v2(dA)
                    cuda.cuMemFree_v2(dB)
                    cuda.cuMemFree_v2(dC)
                    cuda.cuModuleUnload(cmod)
                    out_mat = [[round(float(hC[r * n + c]), 4) for c in range(n)] for r in range(m)]
                    flops = 2.0 * m * n * k
                    gflops = round((flops / max(gms / 1000.0, 1e-9)) / 1e9, 3)
                    return {"matrix": out_mat, "C": out_mat, "shape": [m, n], "gpu_kernel_ms": gms, "kernel_ms": gms, "gflops": gflops, "backend": "cuda-ptx-sm50"}
                finally:
                    cuda.cuCtxDestroy_v2(ctx)
        t0 = time.perf_counter()
        out_mat = [[round(sum(a_mat[r][i] * b_mat[i][c] for i in range(k)), 4) for c in range(n)] for r in range(m)]
        gms = round((time.perf_counter() - t0) * 1000.0, 4)
        return {"matrix": out_mat, "C": out_mat, "shape": [m, n], "gpu_kernel_ms": gms, "kernel_ms": gms, "gflops": 0.0, "backend": "cpu-fallback"}

    def cosine_search(query: list[float], docs: list[list[float]], labels: list[str] | None = None) -> dict:
        if not query or not docs:
            return {"matches": [], "ranked": [], "backend": "empty"}
        qnorm = math.sqrt(sum(float(x) * float(x) for x in query)) or 1.0
        q_unit = [[float(x) / qnorm] for x in query]
        norm_docs = []
        for d in docs:
            dn = math.sqrt(sum(float(x) * float(x) for x in d)) or 1.0
            norm_docs.append([float(x) / dn for x in d])
        mm = matmul(norm_docs, q_unit)
        scores = [row[0] for row in mm["matrix"]]
        ranked = sorted(
            [{"index": i, "label": (labels[i] if labels and i < len(labels) else f"doc_{i}"), "score": round(s, 4)} for i, s in enumerate(scores)],
            key=lambda item: item["score"], reverse=True,
        )
        return {"matches": ranked, "ranked": ranked, "gpu_kernel_ms": mm["gpu_kernel_ms"], "kernel_ms": mm["gpu_kernel_ms"], "backend": mm["backend"]}

    def vram_stress(alloc_mib: int = 64, n_floats: int = 262144) -> dict:
        info = device_info()
        alloc_mib = max(4, min(int(alloc_mib), max(64, VRAM_MB or 1024)))
        n_floats = max(1024, min(int(n_floats), 2 * 1024 * 1024))
        cuda = _open_cuda()
        if cuda is not None:
            dev = ctypes.c_int()
            ctx = ctypes.c_void_p()
            if cuda.cuDeviceGet(ctypes.byref(dev), 0) == 0 and cuda.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev) == 0:
                try:
                    alloc_bytes = alloc_mib * 1024 * 1024
                    dptr = ctypes.c_uint64()
                    t0 = time.perf_counter()
                    rc = cuda.cuMemAlloc_v2(ctypes.byref(dptr), ctypes.c_size_t(alloc_bytes))
                    if rc == 0:
                        cuda.cuMemsetD32_v2(ctypes.c_uint64(dptr.value), ctypes.c_uint32(0x3F800000), ctypes.c_size_t(alloc_bytes // 4))
                        cuda.cuCtxSynchronize()
                        alloc_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                        cuda.cuMemFree_v2(dptr)
                    else:
                        alloc_ms = -1.0
                finally:
                    cuda.cuCtxDestroy_v2(ctx)
                sx = saxpy(2.5, [1.25] * min(n_floats, 65536), [0.75] * min(n_floats, 65536))
                return {
                    "gpu": info,
                    "allocated_vram_mib": alloc_mib,
                    "allocated_mib": alloc_mib,
                    "vram_alloc_memset_ms": alloc_ms,
                    "saxpy_elements": sx["elements"],
                    "saxpy_kernel_ms": sx["gpu_kernel_ms"],
                    "kernel_ms": sx["gpu_kernel_ms"],
                    "sample_result": sx["output"][0] if sx["output"] else None,
                    "backend": sx["backend"],
                }
        sx = saxpy(2.5, [1.25] * 4096, [0.75] * 4096)
        return {
            "gpu": info,
            "allocated_vram_mib": 0,
            "allocated_mib": 0,
            "vram_alloc_memset_ms": 0.0,
            "saxpy_elements": sx["elements"],
            "saxpy_kernel_ms": sx["gpu_kernel_ms"],
            "kernel_ms": sx["gpu_kernel_ms"],
            "sample_result": sx["output"][0] if sx["output"] else None,
            "backend": sx["backend"],
        }

    setattr(mod, "device_info", device_info)
    setattr(mod, "saxpy", saxpy)
    setattr(mod, "matmul", matmul)
    setattr(mod, "cosine_search", cosine_search)
    setattr(mod, "vram_stress", vram_stress)
    sys.modules["aicf_gpu"] = mod



def _cap_vram():
    """Soft layer only (a hostile function can undo it): the worker enforces vram_mb from outside the container."""
    global _VRAM_CAPPED
    if _VRAM_CAPPED or not VRAM_MB or "torch" not in sys.modules:
        return
    try:
        torch = sys.modules["torch"]
        if torch.cuda.is_available():
            total = torch.cuda.get_device_properties(0).total_memory
            torch.cuda.set_per_process_memory_fraction(min(1.0, VRAM_MB * 1024 * 1024 / total), 0)
        _VRAM_CAPPED = True
    except Exception:
        pass


def _err(e):
    return {"type": type(e).__name__, "message": str(e)[:2000], "traceback": traceback.format_exc()[-8000:]}


def _materialize_files(files: dict) -> None:
    if not files or not isinstance(files, dict):
        return
    import posixpath
    import tempfile
    bundle_dir = tempfile.mkdtemp(prefix="aicf_files_")
    if bundle_dir not in sys.path:
        sys.path.insert(0, bundle_dir)
    tmp_base = tempfile.gettempdir()
    compat_root = os.environ.get("AICF_FILES_ROOT") or os.path.join(tmp_base, "aicf_files")
    if os.path.islink(compat_root):
        raise ValueError("refusing symlinked AICF_FILES_ROOT")
    os.makedirs(compat_root, mode=0o700, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for rel, content in files.items():
        if not isinstance(rel, str):
            continue
        raw = base64.b64decode(content) if isinstance(content, str) else bytes(content)
        clean = posixpath.normpath(rel.lstrip("/"))
        if not clean or clean == "." or clean.startswith("..") or "/../" in f"/{clean}/":
            raise ValueError(f"invalid bundled file path: {rel!r}")
        targets = [os.path.join(bundle_dir, clean), os.path.join(compat_root, clean)]
        for dst in targets:
            parent = os.path.dirname(dst)
            if os.path.islink(parent) or os.path.islink(dst):
                raise ValueError(f"refusing symlinked destination: {dst!r}")
            os.makedirs(parent, mode=0o700, exist_ok=True)
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)


def _load(req):
    import cloudpickle

    sha = req["sha"]
    if sha in _LOADED:
        return _LOADED[sha], False, 0.0
    b64 = req.get("bundle_b64")
    if not b64:
        raise LookupError("bundle not loaded and not supplied")
    raw = base64.b64decode(b64)
    if hashlib.sha256(raw).hexdigest() != sha:
        raise ValueError("bundle sha256 mismatch")
    bundle = cloudpickle.loads(raw)
    t = time.perf_counter()
    _materialize_files(bundle.get("files") or {})
    entry = {"kind": bundle["kind"], "obj": bundle["obj"], "inst": None, "exit": list(bundle.get("exit", []))}
    if entry["kind"] == "cls":
        inst = entry["obj"]()
        entry["inst"] = inst
        for name in bundle.get("enter", []):
            getattr(inst, name)()
    _LOADED[sha] = entry
    return entry, True, (time.perf_counter() - t) * 1000


def _call(req):
    import cloudpickle

    limit = int(req.get("max_output_bytes") or 256 * 1024)
    out, err = _Capped(limit), _Capped(limit)
    base = {"stdout": "", "stderr": "", "truncated": False, "loaded": False, "enter_ms": 0.0, "exec_ms": 0.0}
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        try:
            entry, loaded, enter_ms = _load(req)
            base["loaded"], base["enter_ms"] = loaded, round(enter_ms, 3)
            raw = base64.b64decode(req["args_b64"])
            if req.get("args_format", "cloudpickle") == "cloudpickle":
                args, kwargs = cloudpickle.loads(raw)
            else:
                d = json.loads(raw)
                args, kwargs = d.get("args", []), d.get("kwargs", {})
            fn = getattr(entry["inst"], req["method"]) if entry["kind"] == "cls" else entry["obj"]
            _cap_vram()
            t = time.perf_counter()
            res = fn(*args, **kwargs)
            base["exec_ms"] = round((time.perf_counter() - t) * 1000, 3)
            if req.get("result_format", "cloudpickle") == "json":
                payload, fmt = json.dumps(res).encode(), "json"
            else:
                payload, fmt = cloudpickle.dumps(res), "cloudpickle"
            r = {"ok": True, "result_b64": base64.b64encode(payload).decode(), "format": fmt}
        except BaseException as e:   # includes SystemExit from user code: the runner must survive it
            r = {"ok": False, "error": _err(e)}
    finally:
        sys.stdout, sys.stderr = old
    base.update(r)
    base["stdout"], base["stderr"], base["truncated"] = out.value(), err.value(), out.truncated or err.truncated
    return base


def _shutdown():
    for entry in _LOADED.values():
        for name in entry["exit"]:
            try:
                getattr(entry["inst"], name)()
            except BaseException:
                pass


_IN_CALL = False
_CALL_SEQ = 0


def _watch_stdin():
    """If the worker process on the host dies (even SIGKILL), its pipe to `docker run -i` closes and our _in hits EOF/POLLHUP.
    While a call is running on the main thread, nothing reads _in — without this watcher a long GPU call would outlive its dead
    worker and hold VRAM until the function returned on its own."""
    import fcntl
    import termios
    fd = _in.fileno()
    p = select.poll()
    p.register(fd, select.POLLHUP | select.POLLERR | select.POLLIN)
    while True:
        if not _IN_CALL:
            for _, ev in p.poll(100):
                if ev & (select.POLLHUP | select.POLLERR):
                    os._exit(0)
            time.sleep(0.1)
            continue
        seq0 = _CALL_SEQ
        for _, ev in p.poll(100):
            if ev & (select.POLLHUP | select.POLLERR):
                os._exit(0)
            if _IN_CALL and _CALL_SEQ == seq0 and (ev & select.POLLIN):
                try:
                    nbytes = struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\x00\x00\x00\x00"))[0]
                    if nbytes == 0 and _IN_CALL and _CALL_SEQ == seq0:
                        os._exit(0)
                except OSError:
                    os._exit(0)
        time.sleep(0.05)


def main():
    global _IN_CALL, _CALL_SEQ
    _install_aicf_gpu_module()
    threading.Thread(target=_watch_stdin, daemon=True).start()
    _send({"ready": True, "pid": os.getpid(), "python": sys.version.split()[0]})
    while True:
        try:
            req = _recv()
        except Exception as e:
            _send({"ok": False, "error": _err(e)})
            break
        if req is None or req.get("op") == "shutdown":
            break
        op = req.get("op")
        if op == "ping":
            _send({"ok": True})
        elif op == "call":
            _CALL_SEQ += 1
            _IN_CALL = True
            try:
                resp = _call(req)
            finally:
                _IN_CALL = False
            _send(resp)
        else:
            _send({"ok": False, "error": {"type": "BadOp", "message": f"unknown op {op!r}", "traceback": ""}})
    _shutdown()


main()
