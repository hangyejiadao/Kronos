import MetaTrader5 as mt5
mt5.initialize(path=r"D:\mt5\terminal64.exe")
print("account:", mt5.account_info().server, mt5.account_info().login)
for sym in ["XAUUSD", "XAUUSDm", "GOLD", "XAUUSD.a"]:
    info = mt5.symbol_info(sym)
    print(sym, "->", None if info is None else (info.name, info.visible, info.path))
    if info is not None:
        print("  select:", mt5.symbol_select(sym, True))
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 3)
        print("  H1 last 3:", rates)
mt5.shutdown()
