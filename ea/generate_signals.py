#!/usr/bin/env python3
# Generate EA signals on the Windows host ONLY (GPU + data + models live there).
# Mirrors evaluate_gold_vs_base.py exactly: same per-series time split
# (validation = last 10% of the CSV), same lookback/pred_len, same sampling
# settings as the 8077 UI (T=1.0, top_p=0.9, sample_count=1).
#
# Difference vs the evaluator: instead of 50 stride-sampled windows per TF for
# accuracy metrics, this walks the validation slice every `signal_step_bars`
# (default 6) and records the raw predicted 12-bar move for EVERY sampled
# window, so backtest.py can sweep thresholds without re-running the model.
#
# Parameters match the LIVE signal path exactly (2026-11-11, WebSocket EA):
# lookback 400 and pred_len 12 are what ws_signal_bridge.py asks 8077
# /api/ea_signal for, so backtest signals are the same predictions the EA
# would have traded on. (The evaluator used 512/48; that costs ~3x the GPU
# time for the same 12th-bar signal.)
#
# Cost: H1 val slice ~5,830 bars / step 6 -> ~970 predicts x ~2 s ~= 35 min.
# Stop the KronosWebUI task first (8077 holds the gold model on the same 8GB
# GPU); use the Task Scheduler pattern for long runs (SSH children die when
# the session drops). Run from D:\\Kronos so `from model import ...` resolves.
#
#   D:\\Kronos\\.venv-gold\\Scripts\\python.exe D:\\Kronos\\finetune_csv\\generate_signals.py --timeframe H1
#   (log: D:\\Kronos\\finetune_csv\\signals\\signals_H1.csv)
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "\\..")
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from model import Kronos, KronosTokenizer, KronosPredictor  # noqa: E402

BASE_DIR = r"D:\Kronos"
DATA_DIR = os.path.join(BASE_DIR, "finetune_csv", "data")
OUT_DIR = os.path.join(BASE_DIR, "finetune_csv", "signals")
GOLD = {
    "model_id": os.path.join(BASE_DIR, "finetune_csv", "finetuned",
                             "XAUUSD_all_timeframes_base", "basemodel", "best_model"),
    "tokenizer_id": os.path.join(BASE_DIR, "finetune_csv", "finetuned",
                                 "XAUUSD_all_timeframes_base", "tokenizer", "best_model"),
}
LOOKBACK = 400  # matches live: /api/ea_signal default lookback (8077 UI also 400)
PRED_LEN = 12    # matches live: bridge asks pred_len=12, signal uses 12th bar
HORIZON = 12  # signal uses the 12th predicted bar


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--step", type=int, default=6, help="signal every N bars in val slice")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--top-p", type=float, default=0.9)
    ap.add_argument("--sample-count", type=int, default=1)
    args = ap.parse_args()

    path = os.path.join(DATA_DIR, f"XAUUSD_{args.timeframe}.csv")
    df = pd.read_csv(path)
    df["timestamps"] = pd.to_datetime(df["timestamps"])
    df = df.sort_values("timestamps").reset_index(drop=True)
    n = len(df)
    val = df.iloc[int(n * 0.9):].reset_index(drop=True)
    log(f"{args.timeframe}: {n} bars, val {len(val)} "
        f"({val['timestamps'].min()} -> {val['timestamps'].max()})")

    tokenizer = KronosTokenizer.from_pretrained(GOLD["tokenizer_id"])
    model = Kronos.from_pretrained(GOLD["model_id"])
    predictor = KronosPredictor(model, tokenizer, device="cuda", max_context=512)
    log("gold model loaded on cuda")

    cols = ["open", "high", "low", "close"] + (["volume"] if "volume" in val.columns else [])
    rows, t0 = [], time.time()
    # Window start s: context = val[s:s+400]; future = val[s+400:s+412].
    # The last context bar is a val bar; the 12 future bars are all val bars.
    last_start = len(val) - (LOOKBACK + PRED_LEN)
    for j, s in enumerate(range(0, last_start + 1, args.step)):
        x_df = val.iloc[s:s + LOOKBACK][cols].reset_index(drop=True)
        x_ts = val.iloc[s:s + LOOKBACK]["timestamps"].reset_index(drop=True)
        y_ts = val.iloc[s + LOOKBACK:s + LOOKBACK + PRED_LEN]["timestamps"].reset_index(drop=True)
        last_close = float(x_df["close"].iloc[-1])
        try:
            pred_df = predictor.predict(df=x_df, x_timestamp=x_ts, y_timestamp=y_ts,
                                        pred_len=PRED_LEN, T=args.temperature,
                                        top_p=args.top_p, sample_count=args.sample_count)
        except Exception as exc:
            log(f"window {j} (start {s}): PREDICT FAILED: {exc}")
            continue
        pred_close_12 = float(pred_df["close"].iloc[HORIZON - 1])
        rows.append({
            "window_time": str(x_ts.iloc[-1]),
            "pred_close_12": pred_close_12,
            "last_close": last_close,
            "pred_move_pct": (pred_close_12 - last_close) / last_close,
        })
        if (j + 1) % 50 == 0:
            log(f"{j + 1} windows, {(time.time() - t0) / 60:.1f} min elapsed")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"signals_{args.timeframe}.csv")
    pd.DataFrame(rows).to_csv(out, index=False)
    log(f"wrote {out} ({len(rows)} signals) in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
