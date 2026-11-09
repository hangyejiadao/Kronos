# Smoke test for the multi-timeframe patch + combined config.
# Builds the real train/val loaders (prints per-series sample counts), then runs
# N basemodel steps exactly like finetune_base_model.train_model and times them,
# so the full training run can be budgeted. No weights are saved.
import sys
import time

sys.path.insert(0, r"D:\Kronos\finetune_csv")
sys.path.insert(0, r"D:\Kronos")

import torch
from config_loader import CustomFinetuneConfig
from finetune_base_model import create_dataloaders
from model import Kronos, KronosTokenizer

CFG = r"D:\Kronos\finetune_csv\configs\config_XAUUSD_all_timeframes.yaml"
N_STEPS = 20

config = CustomFinetuneConfig(CFG)
print("DATA_PATH:", type(config.data_path).__name__,
      len(config.data_path) if isinstance(config.data_path, list) else config.data_path, flush=True)
print("cap train/val per series:", config.max_samples_per_series,
      config.max_val_samples_per_series, flush=True)

device = torch.device("cuda")
train_loader, val_loader, train_ds, val_ds, _, _ = create_dataloaders(config)
print(f"TRAIN samples={len(train_ds)} batches/epoch={len(train_loader)}", flush=True)
print(f"VAL samples={len(val_ds)} batches={len(val_loader)}", flush=True)

tokenizer = KronosTokenizer.from_pretrained(config.pretrained_tokenizer_path).to(device)
print("TOKENIZER LOADED", flush=True)
model = Kronos.from_pretrained(config.pretrained_predictor_path).to(device)
model.train()
print("MODEL LOADED", flush=True)
opt = torch.optim.AdamW(model.parameters(), lr=config.predictor_learning_rate)

times = []
for i, (bx, bxs) in enumerate(train_loader):
    if i >= N_STEPS:
        break
    t0 = time.time()
    bx = bx.to(device, non_blocking=True)
    bxs = bxs.to(device, non_blocking=True)
    with torch.no_grad():
        s0, s1 = tokenizer.encode(bx, half=True)
    tin = [s0[:, :-1], s1[:, :-1]]
    tout = [s0[:, 1:], s1[:, 1:]]
    logits = model(tin[0], tin[1], bxs[:, :-1, :])
    loss, _, _ = model.head.compute_loss(logits[0], logits[1], tout[0], tout[1])
    opt.zero_grad()
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 3.0)
    opt.step()
    torch.cuda.synchronize()
    dt = time.time() - t0
    if i >= 3:
        times.append(dt)
    print(f"step {i} loss={loss.item():.4f} {dt:.3f}s", flush=True)

avg = sum(times) / len(times)
peak = torch.cuda.max_memory_allocated(device) / 1024 ** 3
print(f"SMOKE DONE avg_step={avg:.3f}s peak_mem={peak:.2f} GiB", flush=True)
print(f"EST train epoch: {len(train_loader)} batches -> {len(train_loader) * avg / 3600:.2f} h/epoch", flush=True)
print(f"EST val pass: {len(val_loader)} batches fwd-only -> ~{len(val_loader) * avg / 2 / 60:.1f} min/pass", flush=True)
print(f"EST total (upper bound, tokenizer steps are cheaper): "
      f"tok {config.tokenizer_epochs} ep + pred {config.basemodel_epochs} ep -> "
      f"~{(config.tokenizer_epochs + config.basemodel_epochs) * len(train_loader) * avg / 3600:.1f} h", flush=True)
