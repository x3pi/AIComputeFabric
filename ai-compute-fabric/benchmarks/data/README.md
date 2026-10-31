# Bộ dữ liệu đánh giá sinh code (Offline Benchmark Datasets)

Các bộ dữ liệu dưới đây được lưu cục bộ để chạy đánh giá offline cho `benchmarks/hardware/code_eval.py` và quy trình Best-of-N (`KE_HOACH_BEST_OF_N_CODE.md` Pha 0).

| Tệp | Số bài | Dung lượng (bytes) | SHA-256 | Giấy phép | Nguồn gốc |
|---|---:|---:|---|---|---|
| `HumanEval.jsonl` | 164 | 214,438 | `1d49078ba3e2b196b9344535bef34a43021f038fad9561d6ee7c53450609a6a2` | MIT (`LICENSE.HumanEval`) | `https://github.com/openai/human-eval/raw/master/data/HumanEval.jsonl.gz` (giải nén) |
| `sanitized-mbpp.json` | 427 | 255,053 | `ca95deaa9a01ef0a6f439f88bcf0dd3db3563d22f22aad6cae04ebb9a8d8c8e9` | Apache-2.0 / CC-BY-4.0 (`LICENSE.MBPP`, `README.MBPP.md`) | `https://raw.githubusercontent.com/google-research/google-research/master/mbpp/sanitized-mbpp.json` |
| `LICENSE.HumanEval` | — | 1,083 | `bcba3de214851cce46ed5af42d6698044616eeace887c3231bc7a20474ab639e` | MIT | `https://raw.githubusercontent.com/openai/human-eval/master/LICENSE` |
| `LICENSE.MBPP` | — | 11,358 | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` | Apache-2.0 | `https://raw.githubusercontent.com/google-research/google-research/master/LICENSE` |
| `README.MBPP.md` | — | 1,422 | `02d63a4ffdad806c3c87b5ea2aa91e946f6eec0c2869643b09831650883f41be` | CC-BY-4.0 / Apache-2.0 | `https://raw.githubusercontent.com/google-research/google-research/master/mbpp/README.md` |

