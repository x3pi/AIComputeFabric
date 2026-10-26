"""AUDIO_TTS (MMS-TTS-vie) -> AUDIO_ASR (Qwen3-ASR-0.6B) on a real GPU. Needs AICF_ASR_MODEL and AICF_TTS_MODEL (local dirs).
A round trip proves the two executors work together and that the synthesized speech is intelligible to an independent
recogniser; it is NOT a measure of TTS naturalness (no listening test was done)."""
import json
import os
from pathlib import Path

import pytest

from agents.tasks import Task, TaskType
from agents.validator import validate_result
from audio.evaluation.vn_normalizer import wer
from audio.executors import AudioExecutor

ASR, TTS = os.environ.get("AICF_ASR_MODEL"), os.environ.get("AICF_TTS_MODEL")
pytestmark = pytest.mark.skipif(not (ASR and TTS), reason="set AICF_ASR_MODEL and AICF_TTS_MODEL")
SENT = ["xin chào quý khách", "hôm nay trời nắng đẹp", "công ty chúng tôi xin cảm ơn quý khách hàng", "hóa đơn được thanh toán vào ngày mười lăm tháng ba",
        "cuộc họp sẽ bắt đầu lúc chín giờ sáng"]


def test_synthesized_vietnamese_is_recognised_by_an_independent_asr(tmp_path):
    ex = AudioExecutor(tmp_path, asr_model=ASR, tts_model=TTS)
    rows = []
    for i, s in enumerate(SENT):
        t = Task(task_id=f"s{i}", tenant_id="acme", type=TaskType.AUDIO_TTS, input={"text": s})
        tts = ex.execute(t)
        assert validate_result(t, tts).ok and tts.output["duration_s"] > 0.5
        a = Task(task_id=f"a{i}", tenant_id="acme", type=TaskType.AUDIO_ASR, input={"audio_ref": tts.output["audio_ref"], "lang": "vi"})
        res = ex.execute(a)
        assert validate_result(a, res).ok
        rows.append({"text": s, "asr": res.output["text"], "wer": round(wer(s, res.output["text"]), 3), "duration_s": tts.output["duration_s"]})
    mean = sum(r["wer"] for r in rows) / len(rows)
    Path(os.environ.get("AICF_TTS_REPORT", os.devnull)).write_text(json.dumps(
        {"engines": "facebook/mms-tts-vie -> Qwen3-ASR-0.6B", "rows": rows, "mean_wer": round(mean, 4),
         "caveat": "5 sentences; intelligibility to one ASR model only; MMS-TTS licence is CC-BY-NC 4.0 (not cleared for commercial use); no MOS/listening test"}, ensure_ascii=False, indent=1))
    assert mean < 0.35, rows
