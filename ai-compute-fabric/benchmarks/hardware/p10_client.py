#!/usr/bin/env python3
"""Unified streaming benchmark client for P10 (vLLM vs llama.cpp).

Implements Section 2 of KE_HOACH_THU_NGHIEM_VLLM_WSL2.md:
  1. Single streaming client (`POST /v1/chat/completions`, `stream: True`,
     `stream_options: {"include_usage": True}`) measuring client-side:
       - TTFT (ms): request start -> first non-empty content/reasoning token chunk
       - Inter-token latency (ITL P50/P95 ms)
       - Decode speed (`decode_tok_s`): `(completion_tokens - 1) / decode_duration_s`
       - Prefill speed (`prefill_tok_s`): `prompt_tokens / ttft_s`
  2. Prefix-cache & slot-LCP cache defeat:
       - Prepends `[RunID=<cfg>_rep<r>_<uuid>]` at the very start of every prompt
       - Verifies `usage.prompt_tokens` and (for llama.cpp) `timings.prompt_n`
  3. Replications & statistics:
       - 1 warm-up burn-in run (discarded)
       - >= 7 decode repetitions, >= 3 prefill repetitions (2k, 8k, 24k tokens)
       - Reports median / min / max
  4. Multi-user concurrency (`1, 4, 8, 16`) via `asyncio` over raw HTTP/1.1 streaming sockets:
       - Reports aggregate throughput (`tok/s` across all streams), min/P5 per-user `tok/s`,
         TTFT P50/P95, error count, and output uniqueness check.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Deterministic synthetic technical corpus used to build exact-length prompts (2k, 8k, 24k)
_TECH_SENTENCES = [
    "In distributed systems, consensus protocols maintain state machine replication across unreliable network links.",
    "PagedAttention partitions the KV cache into fixed-size blocks so non-contiguous physical memory can store sequences.",
    "Roofline analysis relates arithmetic intensity in FLOPs per byte to peak memory bandwidth and compute throughput.",
    "Quantization maps floating-point weights into compact integer representations with group-wise scale factors.",
    "Mixture-of-Experts layers route each token to a sparse subset of feed-forward experts using a learned gating network.",
    "Ring buffers and zero-copy DMA transfers minimize kernel-space context switches on high-throughput NIC interfaces.",
    "FlashAttention tiles query, key, and value matrices in SRAM to avoid materializing the quadratic attention matrix.",
    "Speculative decoding uses a lightweight draft model to propose multiple tokens verified in parallel by the target model.",
]


def build_prompt_with_nonce(
    cfg_id: str,
    rep_idx: int,
    target_tokens: int = 128,
    task_suffix: str = "Write a concise technical explanation of how hash tables handle collisions using open addressing and linear probing, including complexity analysis.",
) -> str:
    """Build a prompt starting with a unique anti-cache nonce and padded to approximately `target_tokens` tokens."""
    nonce = f"[RunID={cfg_id}_rep{rep_idx}_{uuid.uuid4().hex[:12]}]"
    if target_tokens <= 160:
        return f"{nonce} {task_suffix} /no_think"

    # Each sentence + index tag is ~31 tokens (~22 words) on Qwen3 tokenizer.
    words_needed = max(10, int((target_tokens - 64) * 0.50))
    chunks: list[str] = [nonce, "Reference Context Blocks:"]
    word_count = 4
    blk = 0
    while word_count < words_needed:
        s = _TECH_SENTENCES[blk % len(_TECH_SENTENCES)]
        line = f"[Block-{blk:04d}-{ (blk * 2654435761) & 0xFFFF:04x}] {s}"
        chunks.append(line)
        word_count += len(line.split())
        blk += 1
    chunks.append(f"\nBased on the context above and your systems knowledge ({nonce}): {task_suffix} /no_think")
    return "\n".join(chunks)


@dataclass
class StreamSampleMetrics:
    ok: bool
    status_code: int
    cfg_id: str
    rep_idx: int
    nonce: str
    ttft_ms: float = 0.0
    total_ms: float = 0.0
    decode_ms: float = 0.0
    prompt_tokens: int = 0
    llama_prompt_n: int = -1
    completion_tokens: int = 0
    chunk_count: int = 0
    decode_tok_s: float = 0.0
    prefill_tok_s: float = 0.0
    itl_p50_ms: float = 0.0
    itl_p95_ms: float = 0.0
    output_sha256: str = ""
    output_preview: str = ""
    error: str = ""


def percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(math.ceil(p * len(s))) - 1))
    return round(s[idx], 2)


def summary_stats(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {"median": 0.0, "min": 0.0, "max": 0.0, "mean": 0.0, "stdev": 0.0}
    return {
        "median": round(statistics.median(vals), 2),
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "mean": round(statistics.mean(vals), 2),
        "stdev": round(statistics.stdev(vals), 2) if len(vals) > 1 else 0.0,
    }


async def stream_one_chat_completion(
    base_url: str,
    model: str,
    prompt: str,
    cfg_id: str,
    rep_idx: int,
    max_tokens: int = 256,
    temperature: float = 0.0,
    seed: int = 42,
    timeout_s: float = 300.0,
) -> StreamSampleMetrics:
    """Perform a single streaming `/v1/chat/completions` request using asyncio sockets for sub-millisecond timing."""
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = (parsed.path.rstrip("/") or "") + "/v1/chat/completions"

    nonce_end = prompt.find("]")
    nonce = prompt[: nonce_end + 1] if nonce_end != -1 else cfg_id

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "seed": seed,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    req_header = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Accept: text/event-stream\r\n"
        f"Connection: close\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"\r\n"
    ).encode("ascii")

    t0 = time.perf_counter()
    first_token_t: float | None = None
    last_token_t: float | None = None
    token_timestamps: list[float] = []
    text_parts: list[str] = []
    usage_prompt_tokens = 0
    usage_completion_tokens = 0
    llama_prompt_n = -1
    status_code = 0

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=15.0)
        writer.write(req_header + body_bytes)
        await writer.drain()

        # Read HTTP status line
        status_line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        if not status_line:
            writer.close()
            return StreamSampleMetrics(
                ok=False, status_code=0, cfg_id=cfg_id, rep_idx=rep_idx, nonce=nonce, error="Empty HTTP status line"
            )
        parts = status_line.decode("latin-1", errors="replace").strip().split(" ", 2)
        status_code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0

        # Read headers
        while True:
            hdr_line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
            if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                break

        if status_code != 200:
            err_raw = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            writer.close()
            return StreamSampleMetrics(
                ok=False,
                status_code=status_code,
                cfg_id=cfg_id,
                rep_idx=rep_idx,
                nonce=nonce,
                error=err_raw.decode("utf-8", errors="replace")[:400],
            )

        # Read SSE lines
        while True:
            raw_line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                evt = json.loads(data_str)
            except ValueError:
                continue

            now_t = time.perf_counter()
            if isinstance(evt.get("usage"), dict):
                u = evt["usage"]
                if u.get("prompt_tokens"):
                    usage_prompt_tokens = int(u["prompt_tokens"])
                if u.get("completion_tokens"):
                    usage_completion_tokens = int(u["completion_tokens"])

            if isinstance(evt.get("timings"), dict):
                tm = evt["timings"]
                if "prompt_n" in tm and tm["prompt_n"] is not None:
                    llama_prompt_n = int(tm["prompt_n"])
                if not usage_completion_tokens and tm.get("predicted_n"):
                    usage_completion_tokens = int(tm["predicted_n"])

            choices = evt.get("choices")
            if isinstance(choices, list) and choices:
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or delta.get("reasoning_content") or ""
                if piece:
                    if first_token_t is None:
                        first_token_t = now_t
                    last_token_t = now_t
                    token_timestamps.append(now_t)
                    text_parts.append(piece)

        writer.close()
    except Exception as exc:
        return StreamSampleMetrics(
            ok=False,
            status_code=status_code,
            cfg_id=cfg_id,
            rep_idx=rep_idx,
            nonce=nonce,
            error=f"{type(exc).__name__}: {exc}",
        )

    t_end = time.perf_counter()
    if first_token_t is None:
        return StreamSampleMetrics(
            ok=False,
            status_code=status_code,
            cfg_id=cfg_id,
            rep_idx=rep_idx,
            nonce=nonce,
            error="No token chunks received in stream",
        )

    ttft_s = first_token_t - t0
    last_t = last_token_t if last_token_t is not None else t_end
    decode_s = max(1e-6, last_t - first_token_t)
    total_s = t_end - t0

    chunk_count = len(token_timestamps)
    comp_tokens = usage_completion_tokens if usage_completion_tokens > 0 else chunk_count
    effective_prompt_tokens = llama_prompt_n if llama_prompt_n > 0 else usage_prompt_tokens

    decode_tokens_after_first = max(1, comp_tokens - 1) if comp_tokens > 1 else 1
    decode_tok_s = decode_tokens_after_first / decode_s if comp_tokens > 1 else 0.0
    prefill_tok_s = effective_prompt_tokens / max(1e-6, ttft_s) if effective_prompt_tokens > 0 else 0.0

    itls_ms = [
        (token_timestamps[i] - token_timestamps[i - 1]) * 1000.0
        for i in range(1, len(token_timestamps))
    ]
    full_text = "".join(text_parts)
    out_sha = hashlib.sha256(full_text.encode("utf-8")).hexdigest()[:16]

    return StreamSampleMetrics(
        ok=True,
        status_code=status_code,
        cfg_id=cfg_id,
        rep_idx=rep_idx,
        nonce=nonce,
        ttft_ms=round(ttft_s * 1000.0, 2),
        total_ms=round(total_s * 1000.0, 2),
        decode_ms=round(decode_s * 1000.0, 2),
        prompt_tokens=usage_prompt_tokens,
        llama_prompt_n=llama_prompt_n,
        completion_tokens=comp_tokens,
        chunk_count=chunk_count,
        decode_tok_s=round(decode_tok_s, 2),
        prefill_tok_s=round(prefill_tok_s, 2),
        itl_p50_ms=percentile(itls_ms, 0.50),
        itl_p95_ms=percentile(itls_ms, 0.95),
        output_sha256=out_sha,
        output_preview=full_text[:120].replace("\n", " "),
    )


def run_decode_series(
    base_url: str,
    model: str,
    cfg_id: str,
    reps: int = 7,
    burn_in: int = 1,
    max_tokens: int = 256,
    seed: int = 42,
) -> dict[str, Any]:
    """Run 1 warm-up burn-in + `reps` (>=7) single-user decode measurements."""
    burn_samples: list[dict[str, Any]] = []
    for b in range(burn_in):
        prompt = build_prompt_with_nonce(f"{cfg_id}_burn", b, target_tokens=128)
        m = asyncio.run(
            stream_one_chat_completion(
                base_url=base_url, model=model, prompt=prompt, cfg_id=f"{cfg_id}_burn", rep_idx=b, max_tokens=max_tokens, seed=seed
            )
        )
        burn_samples.append(asdict(m))

    samples: list[StreamSampleMetrics] = []
    for r in range(1, reps + 1):
        prompt = build_prompt_with_nonce(cfg_id, r, target_tokens=128)
        m = asyncio.run(
            stream_one_chat_completion(
                base_url=base_url, model=model, prompt=prompt, cfg_id=cfg_id, rep_idx=r, max_tokens=max_tokens, seed=seed
            )
        )
        samples.append(m)

    ok_samples = [s for s in samples if s.ok]
    return {
        "kind": "decode_1user",
        "cfg_id": cfg_id,
        "base_url": base_url,
        "model": model,
        "reps_requested": reps,
        "reps_ok": len(ok_samples),
        "decode_tok_s": summary_stats([s.decode_tok_s for s in ok_samples]),
        "ttft_ms": summary_stats([s.ttft_ms for s in ok_samples]),
        "itl_p50_ms": summary_stats([s.itl_p50_ms for s in ok_samples]),
        "prompt_tokens_min": min((s.prompt_tokens for s in ok_samples), default=0),
        "llama_prompt_n_min": min((s.llama_prompt_n for s in ok_samples if s.llama_prompt_n >= 0), default=-1),
        "burn_in": burn_samples,
        "samples": [asdict(s) for s in samples],
    }


def run_prefill_series(
    base_url: str,
    model: str,
    cfg_id: str,
    target_prompt_tokens: int = 2048,
    reps: int = 3,
    burn_in: int = 1,
    max_tokens: int = 16,
    seed: int = 42,
) -> dict[str, Any]:
    """Run 1 warm-up + `reps` (>=3) prefill/TTFT measurements at `target_prompt_tokens`."""
    for b in range(burn_in):
        prompt = build_prompt_with_nonce(f"{cfg_id}_pf{target_prompt_tokens}_burn", b, target_tokens=target_prompt_tokens)
        _ = asyncio.run(
            stream_one_chat_completion(
                base_url=base_url, model=model, prompt=prompt, cfg_id=f"{cfg_id}_burn", rep_idx=b, max_tokens=max_tokens, seed=seed
            )
        )

    samples: list[StreamSampleMetrics] = []
    for r in range(1, reps + 1):
        prompt = build_prompt_with_nonce(f"{cfg_id}_pf{target_prompt_tokens}", r, target_tokens=target_prompt_tokens)
        m = asyncio.run(
            stream_one_chat_completion(
                base_url=base_url,
                model=model,
                prompt=prompt,
                cfg_id=f"{cfg_id}_pf{target_prompt_tokens}",
                rep_idx=r,
                max_tokens=max_tokens,
                seed=seed,
            )
        )
        samples.append(m)

    ok_samples = [s for s in samples if s.ok]
    return {
        "kind": f"prefill_{target_prompt_tokens}",
        "cfg_id": cfg_id,
        "target_prompt_tokens": target_prompt_tokens,
        "reps_requested": reps,
        "reps_ok": len(ok_samples),
        "actual_prompt_tokens": summary_stats([float(s.prompt_tokens) for s in ok_samples]),
        "llama_prompt_n": [s.llama_prompt_n for s in ok_samples],
        "ttft_ms": summary_stats([s.ttft_ms for s in ok_samples]),
        "prefill_tok_s": summary_stats([s.prefill_tok_s for s in ok_samples]),
        "samples": [asdict(s) for s in samples],
    }


async def _multiuser_batch_async(
    base_url: str,
    model: str,
    cfg_id: str,
    concurrency: int,
    prompt_tokens: int = 1024,
    max_tokens: int = 256,
    seed: int = 42,
) -> dict[str, Any]:
    tasks_topics = [
        f"Explain algorithm #{i + 1}: B-tree node splitting, write-ahead logging, and MVCC snapshot isolation in detail."
        for i in range(concurrency)
    ]
    prompts = [
        build_prompt_with_nonce(
            f"{cfg_id}_c{concurrency}_u{i}",
            i,
            target_tokens=prompt_tokens,
            task_suffix=tasks_topics[i],
        )
        for i in range(concurrency)
    ]
    t0 = time.perf_counter()
    results = await asyncio.gather(
        *[
            stream_one_chat_completion(
                base_url=base_url,
                model=model,
                prompt=prompts[i],
                cfg_id=f"{cfg_id}_c{concurrency}",
                rep_idx=i,
                max_tokens=max_tokens,
                seed=seed + i,
            )
            for i in range(concurrency)
        ]
    )
    wall_s = max(1e-6, time.perf_counter() - t0)
    ok_res = [r for r in results if r.ok]
    err_res = [r for r in results if not r.ok]

    total_comp_tokens = sum(r.completion_tokens for r in ok_res)
    per_user_tok_s = [r.decode_tok_s for r in ok_res]
    ttfts = [r.ttft_ms for r in ok_res]
    unique_shas = len({r.output_sha256 for r in ok_res if r.output_sha256})

    return {
        "kind": f"multiuser_c{concurrency}",
        "cfg_id": cfg_id,
        "concurrency": concurrency,
        "wall_s": round(wall_s, 3),
        "ok_count": len(ok_res),
        "error_count": len(err_res),
        "total_completion_tokens": total_comp_tokens,
        "aggregate_wall_tok_s": round(total_comp_tokens / wall_s, 2),
        "sum_user_decode_tok_s": round(sum(per_user_tok_s), 2),
        "min_user_tok_s": round(min(per_user_tok_s), 2) if per_user_tok_s else 0.0,
        "p5_user_tok_s": percentile(per_user_tok_s, 0.05),
        "median_user_tok_s": round(statistics.median(per_user_tok_s), 2) if per_user_tok_s else 0.0,
        "ttft_p50_ms": percentile(ttfts, 0.50),
        "ttft_p95_ms": percentile(ttfts, 0.95),
        "unique_outputs_count": unique_shas,
        "errors": [r.error for r in err_res[:4]],
        "samples": [asdict(r) for r in results],
    }


def run_multiuser_batch(
    base_url: str,
    model: str,
    cfg_id: str,
    concurrency: int,
    prompt_tokens: int = 1024,
    max_tokens: int = 256,
    seed: int = 42,
) -> dict[str, Any]:
    return asyncio.run(
        _multiuser_batch_async(
            base_url=base_url,
            model=model,
            cfg_id=cfg_id,
            concurrency=concurrency,
            prompt_tokens=prompt_tokens,
            max_tokens=max_tokens,
            seed=seed,
        )
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Unified P10 streaming benchmark client (vLLM & llama.cpp)")
    ap.add_argument("--url", default="http://127.0.0.1:18090", help="Base URL of OpenAI-compatible engine")
    ap.add_argument("--model", default="aicf", help="Model name to pass in payload")
    ap.add_argument("--cfg-id", required=True, help="Configuration identifier")
    ap.add_argument("--mode", choices=["decode", "prefill", "multiuser"], default="decode")
    ap.add_argument("--reps", type=int, default=7)
    ap.add_argument("--prompt-tokens", type=int, default=2048)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--out", default="", help="Optional path to append JSONL record")
    args = ap.parse_args()

    if args.mode == "decode":
        res = run_decode_series(
            base_url=args.url, model=args.model, cfg_id=args.cfg_id, reps=args.reps, max_tokens=args.max_tokens
        )
    elif args.mode == "prefill":
        res = run_prefill_series(
            base_url=args.url,
            model=args.model,
            cfg_id=args.cfg_id,
            target_prompt_tokens=args.prompt_tokens,
            reps=args.reps,
        )
    else:
        res = run_multiuser_batch(
            base_url=args.url,
            model=args.model,
            cfg_id=args.cfg_id,
            concurrency=args.concurrency,
            prompt_tokens=args.prompt_tokens,
            max_tokens=args.max_tokens,
        )

    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with out_p.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(res, ensure_ascii=False) + "\n")

    print(json.dumps({k: v for k, v in res.items() if k != "samples"}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

