#!/usr/bin/env python3
# 只在 Windows 主机上生成 EA 信号（GPU、数据和模型都在主机上）。
# 与 evaluate_gold_vs_base.py 完全一致：同样的按周期时间切分
#（验证集 = CSV 的后 10%），同样的 lookback/pred_len，同样的采样设置，
# 与 8077 界面一致（T=1.0，top_p=0.9，sample_count=1）。
#
# 与评估脚本的区别：评估脚本是每个周期取 50 个步长采样的窗口算准确率，
# 本脚本是每隔 `signal_step_bars` 根 K 线走一遍验证集，
# 记录每个采样窗口原始的 12 根预测涨跌幅，
# 这样 backtest.py 扫阈值时就不用重新跑模型。
#
# 参数与实盘信号链路完全一致（2026-11-11，WebSocket 版 EA）：
# lookback 400、pred_len 12 就是 ws_signal_bridge.py 向 8077 的
# /api/ea_signal 请求的参数，所以回测信号就是 EA 本来会用来交易的那些预测。
#（评估脚本用的是 512/48，跑同样第 12 根信号要多花约 3 倍 GPU 时间。）
#
#
# 开销：H1 验证集约 5,830 根 K 线 / 步长 6 -> 约 970 次预测 × 约 2 秒 ≈ 35 分钟。
# 先停掉 KronosWebUI 任务（8077 在同一块 8GB 显卡上占着黄金模型）；
# 长时间运行用任务计划挂着（SSH 会话断开时，子进程会被杀掉）。在 D:\Kronos 下运行，
# 这样 `from model import ...` 才能找到模块。
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
LOOKBACK = 400  # 与实盘一致：/api/ea_signal 默认 lookback（8077 界面也是 400）
PRED_LEN = 12    # 与实盘一致：桥请求 pred_len=12，信号用第 12 根
HORIZON = 12  # 信号用第 12 根预测 K 线


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
    # 窗口起点 s：上下文 = val[s:s+400]；未来 = val[s+400:s+412]。
    # 最后一根上下文 K 线是验证集里的 K 线；未来 12 根也全是验证集里的 K 线。
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
