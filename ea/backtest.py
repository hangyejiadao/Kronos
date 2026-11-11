#!/usr/bin/env python3
# Backtest replay for the Kronos-gold EA (CPU-only, fast).
# Reads a price CSV (same format as D:\\Kronos\\finetune_csv\\data\\XAUUSD_<TF>.csv:
#   timestamps,open,high,low,close,volume[,amount])
# plus a signals cache CSV produced by generate_signals.py:
#   window_time,signal,pred_move_pct,pred_close_12,last_close
#   (signal: +1 long / -1 short / 0 flat; window_time = last context bar time)
# and replays bar by bar: one position at a time, ATR-based SL/TP, fixed-risk
# sizing, spread + slippage charged on entry and exit. An opposite signal
# closes the open position and reverses in the same bar (EA behaviour:
# InpCloseOnOpposite=true). SL/TP distances use the same ATR the live
# system uses: mean(high-low) over 14 bars ("proxy", the /api/ea_signal
# number), unless --atr-mode true is passed.
#
# Usage:
#   python backtest.py --prices XAUUSD_H1.csv --signals signals_H1.csv \
#       [--config ea_config.yaml] [--threshold-pct 0.0015] [--sl-mult 1.5] [--tp-mult 3.0]
# PyYAML is optional: without it, defaults below are used and CLI flags override.
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
    contract_size=100.0,  # XAUUSD: 1 lot = 100 oz; $1 move = $100 per lot
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
    """ATR distance used for SL/TP.
    mode 'proxy' (default) = mean(high-low) over `period` bars -- exactly
    what the live 8077 /api/ea_signal sends as `atr` (patch_ea_signal.py),
    and what the WS EA uses for sl_dist/tp_dist. Gold H1 gaps are small,
    so proxy and true ATR are close, but the backtest must use the live
    number or its SL/TP distances will not match the EA's.
    mode 'true' = classic Wilder true-range ATR (kept for comparison)."""
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
    """Replay. Signals are indexed by window_time (last context bar); a signal
    takes effect at the OPEN of the next bar. Exits are checked intrabar in
    order SL first, then TP (conservative when both are hit in one bar)."""
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
    pos = None  # dict(side, entry, sl, tp, lot, entry_idx)
    trades = []
    equity_curve = []

    for i, row in df.iterrows():
        # exit check on the current bar (uses this bar's high/low)
        if pos is not None:
            hit_sl = (row["low"] <= pos["sl"]) if pos["side"] == 1 else (row["high"] >= pos["sl"])
            hit_tp = (row["high"] >= pos["tp"]) if pos["side"] == 1 else (row["low"] <= pos["tp"])
            exit_price, reason = None, None
            if hit_sl:
                exit_price, reason = pos["sl"], "sl"
            elif hit_tp:
                exit_price, reason = pos["tp"], "tp"
            if exit_price is not None:
                # exit cost: spread + slippage against the position
                adj = exit_price - pos["side"] * (cfg["spread_usd"] + cfg["slippage_usd"])
                pnl = pos["side"] * (adj - pos["entry"]) * cfg["contract_size"] * pos["lot"]
                equity += pnl
                trades.append({**pos, "exit": adj, "exit_idx": i, "reason": reason, "pnl": pnl})
                pos = None
        # entry / reversal: signal from the previous closed bar.
        # Same-direction signal while holding: no-op (EA holds too).
        # Opposite signal: close at this bar's open and reverse in the
        # same bar -- matches the EA (InpCloseOnOpposite=true).
        # FLAT (0): no new trade, hold until SL/TP.
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
        # mark-to-market at close for the equity curve
        mtm = equity
        if pos is not None:
            mtm += pos["side"] * (float(row["close"]) - pos["entry"]) \
                * cfg["contract_size"] * pos["lot"]
        peak = max(peak, mtm)
        max_dd = max(max_dd, (peak - mtm) / peak if peak else 0.0)
        equity_curve.append(mtm)
    if pos is not None:  # close any open position at the last close
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
    # threshold is applied here (cache stores raw pred_move_pct, so one cache
    # serves every threshold sweep)
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
