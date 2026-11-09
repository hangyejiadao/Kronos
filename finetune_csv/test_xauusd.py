# 快速测试：MetaQuotes-Demo 上的 XAUUSD 是否可用、历史有多远
# 日志同时写到 D:\Kronos\finetune_csv\test_xauusd.log，SSH 断了也能看
import sys
import MetaTrader5 as mt5

LOG = open(r"D:\Kronos\finetune_csv\test_xauusd.log", "w", encoding="utf-8")

def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.write(s + "\n")
    LOG.flush()

log("init...")
ok = mt5.initialize(path=r"D:\mt5\terminal64.exe", timeout=120000)
log("initialize:", ok, mt5.last_error())
if not ok:
    sys.exit(1)
log("version:", mt5.version())
ai = mt5.account_info()
log("account:", ai.server, ai.login)
info = mt5.symbol_info("XAUUSD")
log("symbol_info XAUUSD:", info)
if info is not None:
    log("select:", mt5.symbol_select("XAUUSD", True))
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H1, 0, 5)
    log("H1 last 5:", rates)
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M1, 0, 5)
    log("M1 last 5:", rates)
    # 最早的一根 H1：从 0 往前取一个很大的位置，看能取到多远
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_D1, 0, 3)
    log("D1 last 3:", rates)
mt5.shutdown()
log("done")
