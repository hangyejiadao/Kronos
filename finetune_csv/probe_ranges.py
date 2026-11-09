import MetaTrader5 as mt5
from datetime import datetime, timezone

ok = mt5.initialize(path=r"D:\mt5\terminal64.exe", timeout=120000)
print("init", ok, mt5.last_error(), flush=True)
acct = mt5.account_info()
print("ACCOUNT", acct.server, acct.login, flush=True)

SYMBOL = "XAUUSDm"
tests = [
    ("H1", mt5.TIMEFRAME_H1, datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)),
    ("H1", mt5.TIMEFRAME_H1, datetime(2014, 1, 1, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)),
    ("M1", mt5.TIMEFRAME_M1, datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)),
    ("M1", mt5.TIMEFRAME_M1, datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)),
    ("M5", mt5.TIMEFRAME_M5, datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)),
    ("M30", mt5.TIMEFRAME_M30, datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2026, 11, 7, tzinfo=timezone.utc)),
]
for name, tf, dfrom, dto in tests:
    rates = mt5.copy_rates_range(SYMBOL, tf, dfrom, dto)
    if rates is None:
        print(f"{name} {dfrom.date()}->{dto.date()} FAIL {mt5.last_error()}", flush=True)
    else:
        print(f"{name} {dfrom.date()}->{dto.date()} OK {len(rates)} bars", flush=True)
mt5.shutdown()
print("PROBE DONE", flush=True)
