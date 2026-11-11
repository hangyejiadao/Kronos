#!/usr/bin/env python3
# Kronos-gold EA 的回测重放（只用 CPU，很快）。
# 读取价格 CSV（格式与 D:\\Kronos\\finetune_csv\\data\\XAUUSD_<TF>.csv 相同：
#   timestamps,open,high,low,close,volume[,amount])
# 再读取 generate_signals.py 生成的信号缓存 CSV：
#   window_time,signal,pred_move_pct,pred_close_12,last_close
#   （signal：+1 做多 / -1 做空 / 0 空仓；window_time = 窗口里最后一根上下文 K 线的时间）
# 然后逐根 K 线重放：同一时间只持一仓，按 ATR 设止损/止盈，按固定风险算仓位，
# 开仓和平仓都要扣点差 + 滑点。出现反向信号时，平掉当前持仓并在同一根 K 线反手
#（EA 的行为：InpCloseOnOpposite=true）。止损/止盈距离用实盘同一套 ATR：
# 14 根 K 线的 high-low 均值（“proxy”，即 /api/ea_signal 发来的那个数），
# 除非传了 --atr-mode true。
#
#
# 用法：
#   python backtest.py --prices XAUUSD_H1.csv --signals signals_H1.csv \
#       [--config ea_config.yaml] [--threshold-pct 0.0015] [--sl-mult 1.5] [--tp-mult 3.0]
# PyYAML 是可选的：没装的话就用下面的默认值，命令行参数可以覆盖。
import argparse
import math
import sys

import numpy as np
import pandas as pd

DEFAULTS = dict(
    threshold_pct=0.001, atr_period=14, atr_mode="proxy",
    sl_atr_mult=1.5, tp_atr_mult=3.0,
    risk_per_trade_pct=0.01, max_lot=1.0, min_lot=0.01, lot_step=0.01,
    spread_usd=0.35, slippage_usd=0.10, initial_equity_usd=10000.0,
    contract_size=100.0,  # XAUUSD：1 手 = 100 盎司；价格动 1 美元 = 每手 100 美元
)


def load_config(path):
    cfg = dict(DEFAULTS)
    if not path:
        return cfg
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg.update({k: v for k, v in (yaml.safe_load(f) or {}).items() if k in cfg})
    except ImportError:
        print("PyYAML not installed; using defaults + CLI flags", file=sys.stderr)
    return cfg


def atr(df, period, mode="proxy"):
    """用于止损/止盈的 ATR 距离。
    mode 'proxy'（默认）= `period` 根 K 线的（最高价-最低价）均值——与实盘
    8077 的 /api/ea_signal 发来的 `atr`（见 patch_ea_signal.py）完全一致，
    也是 WS 版 EA 算 sl_dist/tp_dist 用的数。黄金 H1 的跳空很小，
    所以近似 ATR 和真实 ATR 很接近，但回测必须用实盘那个数，
    否则止损/止盈距离就和 EA 对不上了。
    mode 'true' = 经典的 Wilder 真实波幅 ATR（留作对照）。"""
    if mode == "proxy":
        return (df["high"] - df["low"]).rolling(period, min_periods=1).mean()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=1).mean()


def round_lot(lot, step, lo, hi):
    lot = math.floor(lot / step) * step
    return max(lo, min(hi, round(lot, 2)))


def run(prices, signals, cfg):
    """重放。信号按 window_time（最后一根上下文 K 线）索引；信号在下一根 K 线
    的开盘价生效。出场在 K 线内检查，先看止损、再看止盈（同一根 K 线同时触及
    止损和止盈时，按止损算，偏保守）。"""
    df = prices.copy()
    df["timestamps"] = pd.to_datetime(df["timestamps"])
    df = df.sort_values("timestamps").reset_index(drop=True)
    df["atr"] = atr(df, cfg["atr_period"], cfg.get("atr_mode", "proxy"))
    sig = signals.copy()
    sig["window_time"] = pd.to_datetime(sig["window_time"])
    sig = sig.set_index("window_time")["signal"].to_dict()

    equity = cfg["initial_equity_usd"]
    peak = equity
    max_dd = 0.0
    pos = None  # 持仓字典：side、entry、sl、tp、lot、entry_idx
    trades = []
    equity_curve = []

    for i, row in df.iterrows():
        # 检查当前这根 K 线是否出场（用这根 K 线的最高价/最低价）
        if pos is not None:
            hit_sl = (row["low"] <= pos["sl"]) if pos["side"] == 1 else (row["high"] >= pos["sl"])
            hit_tp = (row["high"] >= pos["tp"]) if pos["side"] == 1 else (row["low"] <= pos["tp"])
            exit_price, reason = None, None
            if hit_sl:
                exit_price, reason = pos["sl"], "sl"
            elif hit_tp:
                exit_price, reason = pos["tp"], "tp"
            if exit_price is not None:
                # 出场成本：按不利于持仓的方向扣点差 + 滑点
                adj = exit_price - pos["side"] * (cfg["spread_usd"] + cfg["slippage_usd"])
                pnl = pos["side"] * (adj - pos["entry"]) * cfg["contract_size"] * pos["lot"]
                equity += pnl
                trades.append({**pos, "exit": adj, "exit_idx": i, "reason": reason, "pnl": pnl})
                pos = None
        # 开仓 / 反手：用上一根收盘 K 线的信号。
        # 持仓时收到同向信号：什么都不做（EA 也是继续持有）。
        # 反向信号：在这根 K 线的开盘价平仓并反手——与 EA 一致
        #（InpCloseOnOpposite=true）。
        # FLAT（0）：不开新仓，持有到止损/止盈。
        if i > 0:
            s = sig.get(df.loc[i - 1, "timestamps"], 0)
            if s in (1, -1):
                if pos is not None and pos["side"] != s:
                    adj = float(row["open"]) - pos["side"] * (cfg["spread_usd"] + cfg["slippage_usd"])
                    pnl = pos["side"] * (adj - pos["entry"]) * cfg["contract_size"] * pos["lot"]
                    equity += pnl
                    trades.append({**pos, "exit": adj, "exit_idx": i, "reason": "reverse", "pnl": pnl})
                    pos = None
                if pos is None:
                    a = float(row["atr"]) if not np.isnan(row["atr"]) else 0.0
                    if a > 0:
                        entry = float(row["open"]) + s * (cfg["spread_usd"] + cfg["slippage_usd"])
                        sl_dist = cfg["sl_atr_mult"] * a
                        tp_dist = cfg["tp_atr_mult"] * a
                        risk_usd = equity * cfg["risk_per_trade_pct"]
                        lot = round_lot(risk_usd / (sl_dist * cfg["contract_size"]),
                                        cfg["lot_step"], cfg["min_lot"], cfg["max_lot"])
                        pos = dict(side=int(s), entry=entry,
                                   sl=entry - s * sl_dist, tp=entry + s * tp_dist,
                                   lot=lot, entry_idx=i)
        # 按收盘价算市值，画资金曲线
        mtm = equity
        if pos is not None:
            mtm += pos["side"] * (float(row["close"]) - pos["entry"]) \
                * cfg["contract_size"] * pos["lot"]
        peak = max(peak, mtm)
        max_dd = max(max_dd, (peak - mtm) / peak if peak else 0.0)
        equity_curve.append(mtm)
    if pos is not None:  # 在最后一根 K 线的收盘价平掉所有未平仓位
        adj = float(df.iloc[-1]["close"]) - pos["side"] * (cfg["spread_usd"] + cfg["slippage_usd"])
        pnl = pos["side"] * (adj - pos["entry"]) * cfg["contract_size"] * pos["lot"]
        equity += pnl
        trades.append({**pos, "exit": adj, "exit_idx": len(df) - 1, "reason": "eod", "pnl": pnl})

    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["pnl"] > 0] if len(tdf) else tdf
    losses = tdf[tdf["pnl"] <= 0] if len(tdf) else tdf
    curve = pd.Series(equity_curve)
    rets = curve.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * math.sqrt(252 * 24)) if len(rets) > 1 and rets.std() else 0.0
    return {
        "trades": int(len(tdf)),
        "win_rate": round(float(len(wins) / len(tdf)), 4) if len(tdf) else 0.0,
        "profit_factor": round(float(wins["pnl"].sum() / abs(losses["pnl"].sum())), 4)
        if len(tdf) and len(losses) and losses["pnl"].sum() != 0 else float("inf") if len(wins) else 0.0,
        "total_pnl_usd": round(float(tdf["pnl"].sum()), 2) if len(tdf) else 0.0,
        "total_return_pct": round(float((equity / cfg["initial_equity_usd"] - 1) * 100), 2),
        "final_equity_usd": round(float(equity), 2),
        "max_drawdown_pct": round(float(max_dd * 100), 2),
        "sharpe_annualised_h1": round(sharpe, 2),
        "avg_win_usd": round(float(wins["pnl"].mean()), 2) if len(wins) else 0.0,
        "avg_loss_usd": round(float(losses["pnl"].mean()), 2) if len(losses) else 0.0,
    }, tdf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", required=True)
    ap.add_argument("--signals", required=True)
    ap.add_argument("--config", default=None)
    ap.add_argument("--threshold-pct", type=float, default=None)
    ap.add_argument("--sl-mult", type=float, default=None)
    ap.add_argument("--tp-mult", type=float, default=None)
    ap.add_argument("--spread-usd", type=float, default=None)
    ap.add_argument("--atr-mode", choices=["proxy", "true"], default=None)
    ap.add_argument("--out-trades", default=None, help="optional CSV path for the trade list")
    args = ap.parse_args()

    cfg = load_config(args.config)
    for k, v in (("threshold_pct", args.threshold_pct), ("sl_atr_mult", args.sl_mult),
                 ("tp_atr_mult", args.tp_mult), ("spread_usd", args.spread_usd),
                 ("atr_mode", args.atr_mode)):
        if v is not None:
            cfg[k] = v

    prices = pd.read_csv(args.prices)
    signals = pd.read_csv(args.signals)
    # 阈值在这里应用（缓存里存的是原始 pred_move_pct，所以一份缓存
    # 可以供所有阈值扫参用）
    signals["signal"] = np.where(signals["pred_move_pct"] > cfg["threshold_pct"], 1,
                                 np.where(signals["pred_move_pct"] < -cfg["threshold_pct"], -1, 0))
    metrics, tdf = run(prices, signals, cfg)
    print("config:", {k: cfg[k] for k in
                      ("threshold_pct", "atr_mode", "sl_atr_mult", "tp_atr_mult", "spread_usd",
                       "risk_per_trade_pct", "initial_equity_usd")})
    for k, v in metrics.items():
        print(f"{k}: {v}")
    if args.out_trades and len(tdf):
        tdf.to_csv(args.out_trades, index=False)
        print(f"wrote {args.out_trades} ({len(tdf)} trades)")


if __name__ == "__main__":
    main()
