//+------------------------------------------------------------------+
//|                                            KronosGoldEA_WS.mq5   |
//|  Gold EA, WebSocket version.                                       |
//|                                                                  |
//|  Architecture (as requested):                                    |
//|    EA = WebSocket SERVER. A C++ DLL (KronosWsServer.dll,          |
//|    loaded by this EA) starts the WS server on 127.0.0.1:8081.    |
//|    Python is the WS CLIENT (ws_signal_bridge.py): on each newly   |
//|    closed H1 bar it asks 8077 /api/ea_signal for a signal --      |
//|    Python runs the gold model and parses the forecast there --    |
//|    then pushes the flat signal JSON to the EA over the socket.    |
//|    This EA polls the DLL, and on a new signal it opens / closes   |
//|    positions with the same risk rules as the HTTP version.        |
//|                                                                  |
//|  Why C++ for the server, not C#: MQL5 #import can only load a     |
//|  NATIVE DLL directly. A C# assembly would need an extra C++/CLI   |
//|  wrapper to be callable from MQL5, so the server is plain C++.    |
//|                                                                  |
//|  Setup:                                                          |
//|  1. Compile KronosWsServer.cpp -> KronosWsServer.dll, copy to     |
//|     MT5 data folder MQL5\Libraries\                              |
//|  2. In MT5: Tools -> Options -> Expert Advisors -> tick           |
//|     "Allow DLL imports". No WebRequest whitelist is needed now.   |
//|  3. 8077 running (gold model), then start ws_signal_bridge.py    |
//|     on the host; it connects to this EA and pushes signals.       |
//|  4. Attach this EA to the XAUUSDm H1 chart, allow Algo Trading.   |
//|  Demo account first: this signal logic is NOT backtested yet.    |
//+------------------------------------------------------------------+
#property copyright "Kronos Gold EA (WebSocket)"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

//--- DLL: WebSocket server started inside the MT5 process
#import "KronosWsServer.dll"
int    WsStart(int port);
void   WsStop();
int    WsIsRunning();
int    WsClientCount();
int    WsHasSignal();
int    WsGetSignal(char &buffer[], int bufferSize);
int    WsSendText(const char &msg[], int len);
#import

//--- Inputs (trading only -- no HTTP settings any more)
input string   InpSymbol          = "XAUUSDm";
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_H1;
input int      InpWsPort          = 8081;      // WS server port (localhost only)
input double   InpRiskPercent     = 1.0;       // % of balance risked per trade
input double   InpSL_ATR          = 1.5;       // Stop loss = ATR * this
input double   InpTP_ATR          = 3.0;       // Take profit = ATR * this
input int      InpATRPeriod       = 14;
input int      InpMaxSpreadPoints = 60;        // Max spread in points, 0 = no limit
input ulong    InpMagic           = 20261111;
input string   InpTradeComment    = "KronosGoldWS";
input bool     InpAllowLong       = true;
input bool     InpAllowShort      = true;
input bool     InpCloseOnOpposite = true;

string g_lastSignalBarTime = "";

//+------------------------------------------------------------------+
//| Flat-JSON helpers (Python sends a flat object, no nesting)       |
//+------------------------------------------------------------------+
string ExtractString(const string &json, const string key)
{
   int pos = StringFind(json, key);
   if(pos < 0) return "";
   pos += StringLen(key);
   int end = StringFind(json, "\"", pos);
   if(end < 0) return "";
   return StringSubstr(json, pos, end - pos);
}

double ExtractDouble(const string &json, const string key)
{
   int pos = StringFind(json, key);
   if(pos < 0) return 0.0;
   pos += StringLen(key);
   int end = StringFind(json, ",", pos);
   int end2 = StringFind(json, "}", pos);
   if(end < 0 || (end2 >= 0 && end2 < end)) end = end2;
   if(end < 0) return 0.0;
   return StringToDouble(StringSubstr(json, pos, end - pos));
}

string TimeframeToString(ENUM_TIMEFRAMES tf)
{
   switch(tf)
   {
      case PERIOD_M1:  return "M1";
      case PERIOD_M5:  return "M5";
      case PERIOD_M15: return "M15";
      case PERIOD_M30: return "M30";
      case PERIOD_H1:  return "H1";
      case PERIOD_H4:  return "H4";
      case PERIOD_D1:  return "D1";
      case PERIOD_W1:  return "W1";
      default:         return "H1";
   }
}

//+------------------------------------------------------------------+
//| Position helpers (only our own magic number counts)              |
//+------------------------------------------------------------------+
bool HasPosition(ENUM_POSITION_TYPE &type, double &volume)
{
   type = (ENUM_POSITION_TYPE)(-1); volume = 0;
   if(!PositionSelect(InpSymbol)) return false;
   if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) return false;
   type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   volume = PositionGetDouble(POSITION_VOLUME);
   return true;
}

bool CloseMyPosition()
{
   if(!trade.PositionClose(InpSymbol))
   {
      PrintFormat("KronosGoldWS: close failed, error=%d", GetLastError());
      return false;
   }
   Print("KronosGoldWS: position closed");
   return true;
}

double CalcVolume(double sl_dist)
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance <= 0 || sl_dist <= 0) return 0;
   double riskMoney = balance * InpRiskPercent / 100.0;
   double tickValue = SymbolInfoDouble(InpSymbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(InpSymbol, SYMBOL_TRADE_TICK_SIZE);
   if(tickValue <= 0 || tickSize <= 0) return 0;
   double lossPerLot = (sl_dist / tickSize) * tickValue;
   if(lossPerLot <= 0) return 0;
   double vol = riskMoney / lossPerLot;
   double vMin  = SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_MIN);
   double vMax  = SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_MAX);
   double vStep = SymbolInfoDouble(InpSymbol, SYMBOL_VOLUME_STEP);
   if(vStep > 0) vol = MathFloor(vol / vStep) * vStep;
   vol = MathMax(vMin, MathMin(vMax, vol));
   return NormalizeDouble(vol, 2);
}

double LocalATR()
{
   int handle = iATR(InpSymbol, InpTimeframe, InpATRPeriod);
   if(handle == INVALID_HANDLE) return 0;
   double buf[];
   if(CopyBuffer(handle, 0, 1, 1, buf) != 1) { IndicatorRelease(handle); return 0; }
   IndicatorRelease(handle);
   return buf[0];
}

//+------------------------------------------------------------------+
//| Execute one signal (same rules as the HTTP version)              |
//+------------------------------------------------------------------+
void ExecuteSignal(const string signal, double sl_dist, double tp_dist)
{
   if(signal != "LONG" && signal != "SHORT")
   {
      PrintFormat("KronosGoldWS: FLAT, no new trade.");
      return;
   }
   if(signal == "LONG" && !InpAllowLong)   { Print("KronosGoldWS: LONG disabled."); return; }
   if(signal == "SHORT" && !InpAllowShort) { Print("KronosGoldWS: SHORT disabled."); return; }

   if(InpMaxSpreadPoints > 0)
   {
      long spread = SymbolInfoInteger(InpSymbol, SYMBOL_SPREAD);
      if(spread > InpMaxSpreadPoints)
      {
         PrintFormat("KronosGoldWS: spread %d > max %d, skip.", spread, InpMaxSpreadPoints);
         return;
      }
   }

   ENUM_POSITION_TYPE curType; double curVol;
   bool hasPos = HasPosition(curType, curVol);
   ENUM_POSITION_TYPE wantType = (signal == "LONG") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;

   if(hasPos && curType == wantType)
   {
      Print("KronosGoldWS: already in the signal direction, hold.");
      return;
   }
   if(hasPos && curType != wantType)
   {
      if(!InpCloseOnOpposite) { Print("KronosGoldWS: opposite held, CloseOnOpposite=false, skip."); return; }
      if(!CloseMyPosition()) return;
   }

   double atr = LocalATR();
   if(sl_dist <= 0 && atr > 0) sl_dist = atr * InpSL_ATR;
   if(tp_dist <= 0 && atr > 0) tp_dist = atr * InpTP_ATR;
   if(sl_dist <= 0 || tp_dist <= 0) { Print("KronosGoldWS: no valid SL/TP, skip."); return; }

   double volume = CalcVolume(sl_dist);
   if(volume <= 0) { Print("KronosGoldWS: volume calc failed, skip."); return; }

   trade.SetExpertMagicNumber(InpMagic);
   bool ok = false;
   if(signal == "LONG")
   {
      double ask = SymbolInfoDouble(InpSymbol, SYMBOL_ASK);
      ok = trade.Buy(volume, InpSymbol, ask, ask - sl_dist, ask + tp_dist, InpTradeComment);
   }
   else
   {
      double bid = SymbolInfoDouble(InpSymbol, SYMBOL_BID);
      ok = trade.Sell(volume, InpSymbol, bid, bid + sl_dist, bid - tp_dist, InpTradeComment);
   }
   if(ok)
      PrintFormat("KronosGoldWS: opened %s %.2f lots, SL dist=%.2f TP dist=%.2f", signal, volume, sl_dist, tp_dist);
   else
      PrintFormat("KronosGoldWS: order failed, error=%d", GetLastError());
}

// Send a short status back to Python over the same socket (optional,
// best-effort: failures are ignored).
void SendStatus(const string bar_time)
{
   ENUM_POSITION_TYPE t; double v;
   bool hasPos = HasPosition(t, v);
   string posStr = "FLAT";
   if(hasPos) posStr = (t == POSITION_TYPE_BUY) ? "LONG" : "SHORT";
   string msg = StringFormat("{\"type\":\"ea_ack\",\"bar_time\":\"%s\",\"position\":\"%s\",\"balance\":%.2f,\"clients\":%d}",
                             bar_time, posStr, AccountInfoDouble(ACCOUNT_BALANCE), WsClientCount());
   char data[];
   StringToCharArray(msg, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(ArraySize(data) > 0) ArrayResize(data, ArraySize(data) - 1);
   WsSendText(data, ArraySize(data));
}

//+------------------------------------------------------------------+
//| Drain all queued WS messages and act on each new signal          |
//+------------------------------------------------------------------+
void PollWebSocket()
{
   while(WsHasSignal() > 0)
   {
      char buf[8192];
      int n = WsGetSignal(buf, ArraySize(buf));
      if(n <= 0)
      {
         if(n == -1) Print("KronosGoldWS: signal too large for buffer, dropped.");
         break;
      }
      string json = CharArrayToString(buf, 0, n, CP_UTF8);
      PrintFormat("KronosGoldWS: received: %s", json);

      string signal = ExtractString(json, "\"signal\":\"");
      if(signal == "")
      {
         Print("KronosGoldWS: message has no signal field, ignored.");
         continue;
      }
      string bar_time = ExtractString(json, "\"bar_time\":\"");
      if(bar_time != "" && bar_time == g_lastSignalBarTime)
      {
         PrintFormat("KronosGoldWS: duplicate signal for bar %s, ignored.", bar_time);
         continue;
      }
      if(bar_time != "") g_lastSignalBarTime = bar_time;

      double change_pct = ExtractDouble(json, "\"change_pct\":");
      double last_close = ExtractDouble(json, "\"last_close\":");
      double pred_close = ExtractDouble(json, "\"pred_close\":");
      double up_ratio   = ExtractDouble(json, "\"up_ratio\":");
      double atr        = ExtractDouble(json, "\"atr\":");
      double sl_dist    = ExtractDouble(json, "\"sl_dist\":");
      double tp_dist    = ExtractDouble(json, "\"tp_dist\":");
      PrintFormat("KronosGoldWS: signal=%s change=%.3f%% last=%.2f pred=%.2f up_ratio=%.2f atr=%.2f",
                  signal, change_pct, last_close, pred_close, up_ratio, atr);
      ExecuteSignal(signal, sl_dist, tp_dist);
      SendStatus(bar_time);
   }
}

//+------------------------------------------------------------------+
//| Init / Deinit / Tick                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   if(!SymbolSelect(InpSymbol, true))
   {
      PrintFormat("KronosGoldWS: cannot select symbol %s", InpSymbol);
      return INIT_FAILED;
   }
   int started = WsStart(InpWsPort);
   if(started < 0 || WsIsRunning() == 0)
   {
      PrintFormat("KronosGoldWS: WsStart(%d) failed. Check KronosWsServer.dll is in MQL5\\Libraries and DLL imports are allowed.", InpWsPort);
      return INIT_FAILED;
   }
   PrintFormat("KronosGoldWS started: %s %s, WS server on 127.0.0.1:%d (%s), risk=%.1f%%/trade",
               InpSymbol, TimeframeToString(InpTimeframe), InpWsPort,
               (started == 0) ? "already running" : "started",
               InpRiskPercent);
   Print("KronosGoldWS: waiting for ws_signal_bridge.py to connect and push signals.");
   EventSetTimer(60);  // heartbeat: re-check the server is alive
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   WsStop();
   Print("KronosGoldWS stopped, WS server shut down.");
}

void OnTimer()
{
   if(WsIsRunning() == 0)
      Print("KronosGoldWS: WARNING - WS server not running (DLL unloaded or stopped).");
   else
      PollWebSocket();  // also drain on the timer in case ticks are sparse
}

void OnTick()
{
   PollWebSocket();
}
//+------------------------------------------------------------------+
