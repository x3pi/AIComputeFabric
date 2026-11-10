# Conversation compaction (API)

`POST /v1/chat/completions` on the gateway (:18080) and on the Ollama-compatible port (`/v1/chat/completions`).

| Mode | Meaning |
|---|---|
| `overflow` (default) | Only when the engine answers `exceed_context_size_error`: summarise old turns, retry once (and once more with the last 2 messages if still too long). A request that fits is never touched. |
| `auto[:T]` | Also compact before sending when the estimated prompt passes T x context (default 0.7, range 0.3-0.95). |
| `off` | Never touch the request; the engine's 400 is returned unchanged. |

Per request: header `X-AICF-Compact: off|overflow|auto:0.8`, `X-AICF-Compact-Keep: N` (2-40, default 4), or body field
`"aicf_compact": "auto:0.8"` / `{"mode":"auto","threshold":0.8,"keep":6}` (removed before forwarding).
Gateway default: flag `-compact-default`.

Kept verbatim: leading system messages and the last N messages (cut moved back to a user message so tool calls stay
paired with results). The rest becomes one memory note appended to the system message. Notes are cached by history
prefix (1 h, 512 entries, per tenant), so a client resending a growing history pays only for the new part.

Response headers: `X-AICF-Compacted: <n> messages -> memory note (~<t> tokens), trigger=<overflow|auto>`,
`X-AICF-Compact-Cache: hit`, `X-AICF-Compact-Failed: <reason>` (original 400 returned).
Metrics: `aicf_compactions_total{trigger}`, `aicf_compactions_failed_total`, `aicf_compaction_cache_hits_total`.

Limits: the summary is lossy (exact numbers/ids are asked to be kept verbatim, not guaranteed); a compacting request
costs an extra summarisation pass (measured: 83.7k-token history -> 117 s on the 27B merged setup); the cache is in
memory and lost on gateway restart; `/v1/responses` and `/api/chat` (Ollama native) are not compacted.
