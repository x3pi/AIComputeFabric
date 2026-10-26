"""python -m studio --data-dir D --dsn DSN [--admin-dsn DSN --setup] [--port 8098] ...  (binds to loopback only)"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from pathlib import Path

from studio.app import build_app
from studio.config import Settings
from studio.db import setup_schema
from studio.web import serve


def main():
    ap = argparse.ArgumentParser(prog="studio")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dsn", required=True, help="unprivileged application role")
    ap.add_argument("--admin-dsn", default="", help="with --setup: create tables, policies and grants first")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--embed-url", default="")
    ap.add_argument("--rerank-url", default="")
    ap.add_argument("--llm-url", default="")
    ap.add_argument("--train-python", default="")
    ap.add_argument("--llama-cpp-dir", default="", help="llama.cpp checkout: enables GGUF adapter export")
    ap.add_argument("--worker-url", default="", help="worker-guard API of this PC (enables the model/adapter switch)")
    ap.add_argument("--worker-id", default="", help="gateway worker id of the PC behind --worker-url")
    ap.add_argument("--adapters-dir", default="", help="worker-guard --adapters-dir: exported adapters are copied here")
    ap.add_argument("--asr-model", default="", help="local HF directory for Qwen3-ASR-0.6B")
    ap.add_argument("--tts-model", default="", help="local HF directory for facebook/mms-tts-vie")
    ap.add_argument("--base-models", default="{}", help='JSON {"name": "/path/to/hf/dir"}')
    ap.add_argument("--gpu-total-mib", type=int, default=12288)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if a.setup:
        if not a.admin_dsn:
            ap.error("--setup needs --admin-dsn")
        setup_schema(a.admin_dsn)
    s = Settings(data_dir=Path(a.data_dir), app_dsn=a.dsn, embed_url=a.embed_url, rerank_url=a.rerank_url, llm_url=a.llm_url,
                 base_models=json.loads(a.base_models), train_python=a.train_python, llama_cpp_dir=a.llama_cpp_dir, worker_url=a.worker_url, adapters_dir=a.adapters_dir, worker_id=a.worker_id,
                 asr_model=a.asr_model, tts_model=a.tts_model, gpu_total_mib=a.gpu_total_mib)
    app = build_app(s)
    srv = serve(app.router, "127.0.0.1", a.port, s.max_body)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    logging.getLogger("studio").info("listening on 127.0.0.1:%d", a.port)
    stop.wait()
    srv.shutdown()
    app.close()


if __name__ == "__main__":
    main()
