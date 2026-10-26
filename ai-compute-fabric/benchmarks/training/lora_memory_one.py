"""Measures GPU memory of a LoRA fine-tuning step for ONE configuration (run in its own process so the
allocator starts clean). Prints one JSON line.
usage: lora_memory_one.py MODEL_DIR BATCH SEQ RANK CKPT(0|1) OPT(adamw|adamw8bit)"""
import json
import sys
import time

import torch
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM

model_dir, batch, seq, rank, ckpt, opt_name = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), bool(int(sys.argv[5])), sys.argv[6]
out = {"model": model_dir.rstrip("/").split("/")[-1], "batch": batch, "seq": seq, "rank": rank, "ckpt": ckpt, "opt": opt_name}
try:
    torch.manual_seed(0)
    free0, total = torch.cuda.mem_get_info()
    m = AutoModelForCausalLM.from_pretrained(model_dir, dtype=torch.bfloat16).cuda()
    if ckpt:
        m.gradient_checkpointing_enable()
        m.enable_input_require_grads()
    m = get_peft_model(m, LoraConfig(r=rank, lora_alpha=2 * rank, target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"], task_type="CAUSAL_LM"))
    params = [p for p in m.parameters() if p.requires_grad]
    out["trainable_params"] = sum(p.numel() for p in params)
    out["adapter_dtype"] = str(params[0].dtype)
    if opt_name == "adamw8bit":
        import bitsandbytes as bnb
        opt = bnb.optim.AdamW8bit(params, lr=1e-4)
    else:
        opt = torch.optim.AdamW(params, lr=1e-4)
    out["after_load_used_mib"] = round((total - torch.cuda.mem_get_info()[0]) / 2**20)
    m.train()
    ids = torch.randint(0, m.config.vocab_size, (batch, seq), device="cuda")
    peak_alloc = peak_used = 0
    t0 = time.time()
    for step in range(3):
        with torch.autocast("cuda", dtype=torch.bfloat16):
            loss = m(input_ids=ids, labels=ids).loss
        loss.backward()
        peak_used = max(peak_used, total - torch.cuda.mem_get_info()[0])
        opt.step()
        opt.zero_grad(set_to_none=True)
        peak_used = max(peak_used, total - torch.cuda.mem_get_info()[0])
        torch.cuda.synchronize()
    out.update(ok=True, step_s=round((time.time() - t0) / 3, 3), loss=round(float(loss), 3),
               peak_allocated_mib=round(torch.cuda.max_memory_allocated() / 2**20), peak_reserved_mib=round(torch.cuda.max_memory_reserved() / 2**20),
               peak_used_mib=round(peak_used / 2**20))
except torch.cuda.OutOfMemoryError:
    out.update(ok=False, error="out_of_memory")
except Exception as e:
    out.update(ok=False, error=f"{type(e).__name__}: {str(e)[:200]}")
print("RESULT " + json.dumps(out))
