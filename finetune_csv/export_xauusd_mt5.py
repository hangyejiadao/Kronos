# 草稿，未在主机上运行 — MT5 导出 XAUUSDm 全部历史，9 个周期
# 2026-11-07 17:20 更新：从 demo 的 plain XAUUSD 切回 Exness 的 XAUUSDm。
# 原因：demo 历史 30 分钟 0 字节已确认卡死；用户 16:45 切 Exness-MT5Trial5
# 失败（账号 277700821 Invalid account），终端现未登录任何账号。
# 运行前：先在 MT5 界面登上正确的 Exness 账号（Real5 或 Trial5），脚本会先
# 检查登录状态和服务器名，未登录 / 非 Exness 服务器时直接退出，不白跑。
# 注意：主机上的副本 D:\Kronos\finetune_csv\export_xauusd_mt5.py 还是旧版
# （XAUUSD），用之前须把本文件同步过去覆盖。
# 输出：D:\Kronos\finetune_csv\data\XAUUSD_M1.csv 等，列为
#   timestamps, open, high, low, close, volume, amount
# volume 用 MT5 的 tick_volume，amount 填 0（仓库 README 允许 volume/amount 全 0）

import os
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd

MT5_PATH = r"D:\mt5\terminal64.exe"
OUT_DIR = r"D:\Kronos\finetune_csv\data"

# 如 MT5 已登录，下面三项留空即可；否则填入账号/密码/服务器
LOGIN = 0
PASSWORD = ""
SERVER = ""

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
    "W1": mt5.TIMEFRAME_W1,
    "MN1": mt5.TIMEFRAME_MN1,
}

if LOGIN:
    ok = mt5.initialize(path=MT5_PATH, login=LOGIN, password=PASSWORD, server=SERVER)
else:
    # 不传 login/password/server，直接连已经在运行的终端（用它当前登录的账号）
    ok = mt5.initialize(path=MT5_PATH)
if not ok:
    raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")

print("MT5 version:", mt5.version())
acct = mt5.account_info()
if acct is None:
    raise SystemExit("MT5 未登录任何账号：请先在 MT5 界面登录 Exness 账号再跑本脚本")
print("Account:", acct)
if "Exness" not in (acct.server or ""):
    raise SystemExit(f"当前登录的是 {acct.server}，不是 Exness 服务器，XAUUSDm 不存在，退出")

os.makedirs(OUT_DIR, exist_ok=True)
utc_from = datetime(2000, 1, 1, tzinfo=timezone.utc)
utc_to = datetime.now(timezone.utc)

SYMBOL = "XAUUSDm"  # Exness 品种（带 m 后缀）；文件名仍用 XAUUSD_ 前缀，与多周期补丁配置一致

print("symbol_select:", mt5.symbol_select(SYMBOL, True), flush=True)
print("symbol_info:", mt5.symbol_info(SYMBOL), flush=True)

for name, tf in TIMEFRAMES.items():
    print(f"fetching {name} ...", flush=True)
    rates = mt5.copy_rates_range(SYMBOL, tf, utc_from, utc_to)
    if rates is None or len(rates) == 0:
        print(name, "no data:", mt5.last_error())
        continue
    df = pd.DataFrame(rates)
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
    print(name, len(out), out["timestamps"].min(), "->", out["timestamps"].max(), path)

mt5.shutdown()
