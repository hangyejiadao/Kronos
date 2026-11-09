# 补导 M1/M5/M15/M30/H1（第一轮导出失败的 5 个周期）
# 原因：单次 copy_rates_range 返回超过约 10 万根 K 线时终端报
# (-2, 'Terminal: Invalid params')。改按月分段拉取再拼接。
# H4/D1/W1/MN1 第一轮已导好，不重复导。
import os
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd

MT5_PATH = r"D:\mt5\terminal64.exe"
OUT_DIR = r"D:\Kronos\finetune_csv\data"
SYMBOL = "XAUUSDm"

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
}

START_YEAR, START_MONTH = 2014, 1  # H4/D1 数据最早到 2014-01-14

ok = mt5.initialize(path=MT5_PATH, timeout=120000)
if not ok:
    raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
acct = mt5.account_info()
if acct is None or "Exness" not in (acct.server or ""):
    raise SystemExit(f"未登录 Exness 账号: {acct}")
print("ACCOUNT", acct.server, acct.login, flush=True)
print("symbol_select:", mt5.symbol_select(SYMBOL, True), flush=True)

os.makedirs(OUT_DIR, exist_ok=True)
now = datetime.now(timezone.utc)

def months():
    y, m = START_YEAR, START_MONTH
    while (y, m) <= (now.year, now.month):
        dfrom = datetime(y, m, 1, tzinfo=timezone.utc)
        if m == 12:
            dto = datetime(y + 1, 1, 1, tzinfo=timezone.utc)
        else:
            dto = datetime(y, m + 1, 1, tzinfo=timezone.utc)
        yield dfrom, min(dto, now)
        m += 1
        if m == 13:
            m, y = 1, y + 1

for name, tf in TIMEFRAMES.items():
    frames = []
    for dfrom, dto in months():
        rates = mt5.copy_rates_range(SYMBOL, tf, dfrom, dto)
        if rates is None or len(rates) == 0:
            continue
        frames.append(pd.DataFrame(rates))
    if not frames:
        print(name, "no data at all", flush=True)
        continue
    df = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "timestamps": pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(None),
        "open": df["open"],
        "high": df["high"],
        "low": df["low"],
        "close": df["close"],
        "volume": df["tick_volume"],
        "amount": 0,
    })
    out = out.sort_values("timestamps").drop_duplicates("timestamps")
    path = os.path.join(OUT_DIR, f"XAUUSD_{name}.csv")
    out.to_csv(path, index=False)
    print(name, len(out), out["timestamps"].min(), "->", out["timestamps"].max(), path, flush=True)

mt5.shutdown()
print("CHUNKED EXPORT DONE", flush=True)
