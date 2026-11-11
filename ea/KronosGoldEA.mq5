//+------------------------------------------------------------------+
//|                                                KronosGoldEA.mq5  |
//|  Gold EA driven by the fine-tuned Kronos-gold model (8077).       |
//|                                                                  |
//|  Split of responsibilities (as requested):                       |
//|    MQL5 EA  : data communication (WebRequest) + open/close        |
//|               positions, risk management.                        |
//|    Python   : 8077 /api/ea_signal runs the model, parses the      |
//|               forecast bars and returns a flat signal JSON:      |
//|               signal / change_pct / last_close / pred_close /    |
//|               up_ratio / atr / sl_dist / tp_dist.                |
//|               The EA only parses those few flat fields, it does  |
//|               NOT parse the 120-bar forecast itself.             |
//|                                                                  |
//|  Flow: new H1 bar -> POST to 127.0.0.1:8077/api/ea_signal        |
//|        -> LONG / SHORT / FLAT -> open, reverse or hold.           |
//|  Python pulls the last 400 bars itself from the same MT5         |
//|  terminal, so the EA does not need to send OHLC or dates.        |
//|                                                                  |
//|  !!! BEFORE USE !!!                                              |
//|  1. MT5 -> Tools -> Options -> Expert Advisors -> tick            |
//|     "Allow WebRequest for listed URL" and add:                   |
//|        http://127.0.0.1:8077                                     |
//|     Without this, WebRequest fails with error 4014.              |
//|  2. 8077 must be running with the gold model loaded (it          |
//|     auto-loads kronos-gold on CUDA at startup).                  |
//|  3. Test on the DEMO account (Exness-MT5Trial5) first.           |
//|     This EA has NOT been backtested yet.                         |
//+------------------------------------------------------------------+
#property copyright "Kronos Gold EA"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

//--- Inputs
input string   InpServerUrl      = "http://127.0.0.1:8077"; // 8077 Python server
input string   InpSymbol         = "XAUUSDm";               // Gold symbol in MT5
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_H1;             // Signal timeframe
input int      InpPredLen        = 12;                      // Bars the model predicts
input int      InpHorizon        = 12;                      // Bar to judge direction on
input double   InpThresholdPct   = 0.10;                    // Min |change| % to trade (0.10 = 0.10%)
input double   InpRiskPercent    = 1.0;                     // % of balance risked per trade
input double   InpSL_ATR         = 1.5;                     // Stop loss = ATR * this
input double   InpTP_ATR         = 3.0;                     // Take profit = ATR * this
input int      InpATRPeriod      = 14;                      // ATR period (EA-side check)
input int      InpMaxSpreadPoints = 60;                     // Max spread in points, 0 = no limit
input ulong    InpMagic          = 20261111;                // Magic number
input string   InpTradeComment   = "KronosGold";            // Order comment
input bool     InpAllowLong      = true;
input bool     InpAllowShort     = true;
input bool     InpCloseOnOpposite = true;                   // Close + reverse on opposite signal
input int      InpRequestTimeoutMs = 120000;                // WebRequest timeout (model ~10-20s)

datetime g_lastBarTime = 0;

//+------------------------------------------------------------------+
//| Map ENUM_TIMEFRAMES to the string Python expects (M1/H1/...)     |
//+------------------------------------------------------------------+
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
//| Flat-JSON helpers: the Python reply has no nested objects,       |
//| so plain StringFind parsing is enough (no JSON library needed).  |
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

//+------------------------------------------------------------------+
//| Ask Python for a signal. Returns "" on failure.                  |
//| Out params are filled from the flat JSON reply.                  |
//+------------------------------------------------------------------+
string RequestSignal(double &change_pct, double &last_close, double &pred_close,
                     double &up_ratio, double &atr, double &sl_dist, double &tp_dist)
{
   change_pct = 0; last_close = 0; pred_close = 0;
   up_ratio = 0; atr = 0; sl_dist = 0; tp_dist = 0;

   string body = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"%s\",\"pred_len\":%d,\"horizon\":%d,"
      "\"threshold_pct\":%.4f,\"sl_atr_mult\":%.2f,\"tp_atr_mult\":%.2f}",
      InpSymbol, TimeframeToString(InpTimeframe), InpPredLen, InpHorizon,
      InpThresholdPct, InpSL_ATR, InpTP_ATR);

   string url = InpServerUrl + "/api/ea_signal";
   char post[], result[];
   string headers = "Content-Type: application/json\r\n";
   StringToCharArray(body, post, 0, WHOLE_ARRAY, CP_UTF8);
   if(ArraySize(post) > 0) ArrayResize(post, ArraySize(post) - 1); // drop trailing \0

   ResetLastError();
   int res = WebRequest("POST", url, NULL, NULL, InpRequestTimeoutMs, post, ArraySize(post), result, headers);
   if(res == -1)
   {
      int err = GetLastError();
      PrintFormat("KronosGold: WebRequest failed, error=%d. If 4014, add %s to MT5 WebRequest whitelist (Tools -> Options -> Expert Advisors).",
                  err, InpServerUrl);
      return "";
   }
   if(res != 200)
   {
      PrintFormat("KronosGold: server returned HTTP %d: %s", res, CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8));
      return "";
   }

   string json = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   PrintFormat("KronosGold: signal reply: %s", json);

   string signal = ExtractString(json, "\"signal\":\"");
   if(signal == "")
   {
      PrintFormat("KronosGold: could not parse signal from reply, error field: %s",
                  ExtractString(json, "\"error\":\""));
      return "";
   }
   change_pct = ExtractDouble(json, "\"change_pct\":");
   last_close = ExtractDouble(json, "\"last_close\":");
   pred_close = ExtractDouble(json, "\"pred_close\":");
   up_ratio   = ExtractDouble(json, "\"up_ratio\":");
   atr        = ExtractDouble(json, "\"atr\":");
   sl_dist    = ExtractDouble(json, "\"sl_dist\":");
   tp_dist    = ExtractDouble(json, "\"tp_dist\":");
   return signal;
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
      PrintFormat("KronosGold: close failed, error=%d", GetLastError());
      return false;
   }
   Print("KronosGold: position closed");
   return true;
}

//+------------------------------------------------------------------+
//| Lot size from fixed fractional risk: risk % of balance,           |
//| stop distance in price -> loss per lot via tick value.           |
//+------------------------------------------------------------------+
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

//+------------------------------------------------------------------+
//| ATR from the chart itself (sanity check / fallback for SL)       |
//+------------------------------------------------------------------+
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
//| Execute one signal                                               |
//+------------------------------------------------------------------+
void ExecuteSignal(const string signal, double sl_dist, double tp_dist)
{
   if(signal != "LONG" && signal != "SHORT")
   {
      PrintFormat("KronosGold: FLAT (change below threshold), no new trade.");
      return;
   }
   if(signal == "LONG" && !InpAllowLong)  { Print("KronosGold: LONG disabled by input."); return; }
   if(signal == "SHORT" && !InpAllowShort){ Print("KronosGold: SHORT disabled by input."); return; }

   // Spread filter
   if(InpMaxSpreadPoints > 0)
   {
      long spread = SymbolInfoInteger(InpSymbol, SYMBOL_SPREAD);
      if(spread > InpMaxSpreadPoints)
      {
         PrintFormat("KronosGold: spread %d points > max %d, skip trade.", spread, InpMaxSpreadPoints);
         return;
      }
   }

   ENUM_POSITION_TYPE curType; double curVol;
   bool hasPos = HasPosition(curType, curVol);
   ENUM_POSITION_TYPE wantType = (signal == "LONG") ? POSITION_TYPE_BUY : POSITION_TYPE_SELL;

   if(hasPos && curType == wantType)
   {
      Print("KronosGold: already in the signal direction, hold.");
      return;
   }
   if(hasPos && curType != wantType)
   {
      if(!InpCloseOnOpposite) { Print("KronosGold: opposite position held, CloseOnOpposite=false, skip."); return; }
      if(!CloseMyPosition()) return;
   }

   // Stop / target: prefer Python's ATR-based distances, fall back to local ATR
   double atr = LocalATR();
   if(sl_dist <= 0 && atr > 0) sl_dist = atr * InpSL_ATR;
   if(tp_dist <= 0 && atr > 0) tp_dist = atr * InpTP_ATR;
   if(sl_dist <= 0 || tp_dist <= 0)
   {
      Print("KronosGold: no valid SL/TP distance, skip trade.");
      return;
   }

   double volume = CalcVolume(sl_dist);
   if(volume <= 0)
   {
      Print("KronosGold: volume calculation failed, skip trade.");
      return;
   }

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
      PrintFormat("KronosGold: opened %s %.2f lots, SL dist=%.2f TP dist=%.2f", signal, volume, sl_dist, tp_dist);
   else
      PrintFormat("KronosGold: order failed, error=%d", GetLastError());
}

//+------------------------------------------------------------------+
//| Init                                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   if(!SymbolSelect(InpSymbol, true))
   {
      PrintFormat("KronosGold: cannot select symbol %s", InpSymbol);
      return INIT_FAILED;
   }
   PrintFormat("KronosGold EA started: %s %s, server=%s, horizon=%d bars, threshold=%.2f%%, risk=%.1f%%/trade",
               InpSymbol, TimeframeToString(InpTimeframe), InpServerUrl, InpHorizon, InpThresholdPct, InpRiskPercent);
   Print("KronosGold: make sure 127.0.0.1:8077 is in the MT5 WebRequest whitelist, and 8077 is running.");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   Print("KronosGold EA stopped.");
}

//+------------------------------------------------------------------+
//| OnTick: act once per new bar (H1 = once per hour)                |
//+------------------------------------------------------------------+
void OnTick()
{
   datetime barTime = iTime(InpSymbol, InpTimeframe, 0);
   if(barTime == 0 || barTime == g_lastBarTime) return;
   g_lastBarTime = barTime;

   PrintFormat("KronosGold: new bar %s, asking Python for signal...",
               TimeToString(barTime, TIME_DATE | TIME_MINUTES));

   double change_pct, last_close, pred_close, up_ratio, atr, sl_dist, tp_dist;
   string signal = RequestSignal(change_pct, last_close, pred_close, up_ratio, atr, sl_dist, tp_dist);
   if(signal == "")
   {
      Print("KronosGold: no signal (server/model error), wait for next bar.");
      return;
   }
   PrintFormat("KronosGold: signal=%s change=%.3f%% last=%.2f pred=%.2f up_ratio=%.2f atr=%.2f",
               signal, change_pct, last_close, pred_close, up_ratio, atr);
   ExecuteSignal(signal, sl_dist, tp_dist);
}
//+------------------------------------------------------------------+
