#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 D:\\Kronos\\webui\\app.py 加上 /api/ea_signal（在 Windows 主机上运行）。

本补丁加了这些内容：
  * load_mt5_recent(symbol, timeframe, count)——用 mt5.copy_rates_from_pos
    取最近 N 根 K 线，这样 EA 不用发送 OHLC 数据，也不用算日期范围，
    只发品种和周期就行。
  * POST /api/ea_signal——用已加载的 kronos-gold 预测器对最近 400 根 K 线
    做预测，在 Python 里解析预测 K 线，返回一个扁平的信号 JSON，
    MQL5 EA 用 StringFind 就能解析：
        {"signal":"LONG"|"SHORT"|"FLAT", "change_pct":0.42,
         "last_close":..., "pred_close":..., "up_ratio":0.83,
         "atr":..., "sl_dist":..., "tp_dist":...,
         "model":"kronos-gold", "timeframe":"H1", "horizon":12, ...}
    信号规则：第 `horizon` 根的 change_pct >= +threshold_pct -> LONG，
               <= -threshold_pct -> SHORT，否则 FLAT。
    sl_dist = ATR × sl_atr_mult，tp_dist = ATR × tp_atr_mult。

用法（在主机上，用 PowerShell）：
    cd D:\\Kronos\\ea
    D:\\Kronos\\.venv-gold\\Scripts\\python.exe patch_ea_signal.py
    # 然后重启 KronosWebUI 计划任务，让 Flask 重新加载 app.py

重要：不要把本文件复制到 D:\\Kronos\\webui\\——Flask 的重载器会监听
那个文件夹，新增 .py 文件就会重启服务。放在 D:\\Kronos\\ea\\。
"""

import datetime
import pathlib
import py_compile
import sys

APP = pathlib.Path(r"D:\Kronos\webui\app.py")

EA_CODE = r'''
def load_mt5_recent(symbol, timeframe, count=400):
    # 直接从本机 MT5 终端取最近 `count` 根 K 线。不需要日期范围，EA 只报品种名就行。
    connected, error = ensure_mt5_connection()
    if not connected:
        return None, error
    if timeframe not in MT5_TIMEFRAMES:
        return None, f'Unsupported timeframe: {timeframe}'
    if not mt5.symbol_select(symbol, True):
        code, message = mt5.last_error()
        return None, f'Unable to select symbol {symbol} ({code}): {message}'
    rates = mt5.copy_rates_from_pos(symbol, MT5_TIMEFRAMES[timeframe], 0, int(count))
    if rates is None:
        code, message = mt5.last_error()
        return None, f'Failed to fetch MT5 rates ({code}): {message}'
    if len(rates) == 0:
        return None, 'MT5 returned no bars for this symbol and time range'
    df = pd.DataFrame(rates)
    df['timestamps'] = pd.to_datetime(df['time'], unit='s', utc=True).dt.tz_localize(None)
    df['volume'] = pd.to_numeric(df['tick_volume'], errors='coerce').fillna(0)
    df = df[['timestamps', 'open', 'high', 'low', 'close', 'volume']]
    df = df.sort_values('timestamps').drop_duplicates('timestamps').dropna().reset_index(drop=True)
    return df, None


@app.route('/api/ea_signal', methods=['POST'])
def ea_signal():
    # EA 专用接口：Python 运行模型并把预测结果解析成一个扁平信号，
    # MQL5 EA 只需要读其中几个字段。
    try:
        data = request.get_json(force=True, silent=True) or {}
        symbol = data.get('symbol', 'XAUUSDm')
        timeframe = data.get('timeframe', 'H1')
        lookback = int(data.get('lookback', 400))
        pred_len = int(data.get('pred_len', 12))
        horizon = int(data.get('horizon', pred_len))
        horizon = max(1, min(horizon, pred_len))
        threshold_pct = float(data.get('threshold_pct', 0.10))
        sl_mult = float(data.get('sl_atr_mult', 1.5))
        tp_mult = float(data.get('tp_atr_mult', 3.0))
        temperature = float(data.get('temperature', 1.0))
        top_p = float(data.get('top_p', 0.9))
        sample_count = int(data.get('sample_count', 1))
    except (TypeError, ValueError) as exc:
        return jsonify({'signal': 'FLAT', 'error': f'Bad ea_signal parameters: {exc}'}), 400

    if not MODEL_AVAILABLE or predictor is None:
        return jsonify({'signal': 'FLAT', 'error': 'Model not loaded'}), 503
    if timeframe not in MT5_TIMEFRAMES:
        return jsonify({'signal': 'FLAT', 'error': f'Unsupported timeframe: {timeframe}'}), 400

    df, error = load_mt5_recent(symbol, timeframe, lookback)
    if error:
        return jsonify({'signal': 'FLAT', 'error': error}), 400
    if len(df) < lookback:
        return jsonify({'signal': 'FLAT', 'error': f'Not enough bars: {len(df)} < {lookback}'}), 400

    required_cols = ['open', 'high', 'low', 'close']
    if 'volume' in df.columns:
        required_cols.append('volume')
    x_df = df.iloc[-lookback:][required_cols].reset_index(drop=True)
    x_timestamp = df.iloc[-lookback:]['timestamps'].reset_index(drop=True)
    try:
        future_timestamps = generate_future_timestamps(
            x_timestamp.iloc[-1], timeframe, pred_len, symbol)
    except ValueError as exc:
        return jsonify({'signal': 'FLAT', 'error': str(exc)}), 400
    y_timestamp = pd.Series(future_timestamps, name='timestamps')

    try:
        pred_df = predictor.predict(
            df=x_df, x_timestamp=x_timestamp, y_timestamp=y_timestamp,
            pred_len=pred_len, T=temperature, top_p=top_p, sample_count=sample_count)
    except Exception as exc:
        return jsonify({'signal': 'FLAT', 'error': f'Model prediction failed: {exc}'}), 500

    # --- Python 把模型输出解析成交易信号 ---
    last_close = float(x_df['close'].iloc[-1])
    pred_closes = [float(c) for c in pred_df['close'].tolist()]
    pred_close_h = pred_closes[horizon - 1]
    change_pct = (pred_close_h - last_close) / last_close * 100.0 if last_close else 0.0
    up_ratio = sum(1 for c in pred_closes if c > last_close) / max(1, len(pred_closes))
    # ATR 近似值：最近 14 根 K 线的（最高价-最低价）均值，近似真实波幅。
    hl = (df['high'] - df['low']).tail(14)
    atr = float(hl.mean()) if len(hl) else 0.0
    sl_dist = atr * sl_mult
    tp_dist = atr * tp_mult
    if change_pct >= threshold_pct:
        signal = 'LONG'
    elif change_pct <= -threshold_pct:
        signal = 'SHORT'
    else:
        signal = 'FLAT'

    print(f"[ea_signal] {symbol} {timeframe} last={last_close:.2f} "
          f"pred_h{horizon}={pred_close_h:.2f} change={change_pct:.3f}% "
          f"up_ratio={up_ratio:.2f} atr={atr:.2f} -> {signal}", flush=True)
    return jsonify({
        'signal': signal,
        'change_pct': round(change_pct, 4),
        'last_close': round(last_close, 4),
        'pred_close': round(pred_close_h, 4),
        'pred_close_1': round(pred_closes[0], 4) if pred_closes else None,
        'up_ratio': round(up_ratio, 4),
        'atr': round(atr, 4),
        'sl_dist': round(sl_dist, 4),
        'tp_dist': round(tp_dist, 4),
        'model': current_model_key or 'kronos-gold',
        'symbol': symbol,
        'timeframe': timeframe,
        'horizon': horizon,
        'pred_len': pred_len,
        'threshold_pct': threshold_pct,
        'bar_time': x_timestamp.iloc[-1].isoformat(),
        'server_time': datetime.datetime.now().isoformat(),
    })

'''

ANCHOR = "@app.route('/')\ndef index():"

def main():
    if not APP.exists():
        print(f"app.py not found: {APP}")
        return 1
    text = APP.read_text(encoding="utf-8")
    if "/api/ea_signal" in text:
        print("ea_signal already present, nothing to do.")
        return 0
    if ANCHOR not in text:
        print("Anchor not found in app.py, aborting (app.py changed?).")
        return 1
    backup = APP.with_suffix(".py.bak." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    backup.write_text(text, encoding="utf-8")
    print(f"Backup: {backup}")
    text = text.replace(ANCHOR, EA_CODE + ANCHOR, 1)
    APP.write_text(text, encoding="utf-8")
    py_compile.compile(str(APP), doraise=True)
    print("Patched OK, syntax check passed. Restart the KronosWebUI task to load it.")
    print("Test: Invoke-RestMethod -Uri http://127.0.0.1:8077/api/ea_signal "
          "-Method Post -ContentType 'application/json' "
          "-Body '{\"symbol\":\"XAUUSDm\",\"timeframe\":\"H1\",\"pred_len\":12,\"horizon\":12}'")
    return 0

if __name__ == "__main__":
    sys.exit(main())
