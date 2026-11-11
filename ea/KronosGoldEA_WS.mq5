//+------------------------------------------------------------------+
//|                                            KronosGoldEA_WS.mq5   |
//|  黄金 EA，WebSocket 版。                                            |
//|                                                                  |
//|  架构（按要求）：                                                   |
//|    EA = WebSocket 服务端。C++ DLL（KronosWsServer.dll，             |
//|    由本 EA 加载）在 127.0.0.1:8081 上启动 WS 服务。                  |
//|    Python 是 WS 客户端（ws_signal_bridge.py）：每根新的 H1 K 线      |
//|    收盘后，它向 8077 的 /api/ea_signal 请求信号——                    |
//|    Python 在那边运行黄金模型并解析预测结果——                          |
//|    然后把扁平的信号 JSON 通过 socket 推给 EA。                       |
//|    本 EA 轮询 DLL，收到新信号后按与 HTTP 版相同的风控规则开仓/平仓。  |
//|                                                                  |
//|                                                                  |
//|  服务端为什么用 C++ 而不用 C#：MQL5 的 #import 只能直接加载           |
//|  原生 DLL。C# 程序集需要额外包一层 C++/CLI 才能被 MQL5 调用，         |
//|  所以服务端直接用纯 C++ 写。                                        |
//|                                                                  |
//|  部署步骤：                                                        |
//|  1. 编译 KronosWsServer.cpp -> KronosWsServer.dll，复制到           |
//|     MT5 数据文件夹的 MQL5\Libraries\                              |
//|  2. 在 MT5 里：工具 -> 选项 -> EA 交易 -> 勾选                       |
//|     “允许 DLL 导入”。现在不再需要 WebRequest 白名单。                |
//|  3. 8077 保持运行（黄金模型），然后在主机上启动 ws_signal_bridge.py，|
//|     它会连上本 EA 并推送信号。                                      |
//|  4. 把本 EA 挂到 XAUUSDm 的 H1 图表上，允许算法交易。                |
//|  先跑模拟账户：这套信号逻辑还没有回测过。                            |
//+------------------------------------------------------------------+
#property copyright "Kronos Gold EA (WebSocket)"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

//--- DLL：在 MT5 进程内启动的 WebSocket 服务端
#import "KronosWsServer.dll"
int    WsStart(int port);
void   WsStop();
int    WsIsRunning();
int    WsClientCount();
int    WsHasSignal();
int    WsGetSignal(char &buffer[], int bufferSize);
int    WsSendText(const char &msg[], int len);
#import

//--- 输入参数（只管交易，不再有 HTTP 相关设置）
input string   InpSymbol          = "XAUUSDm";
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_H1;
input int      InpWsPort          = 8081;      // WS 服务端口（仅本机）
input double   InpRiskPercent     = 1.0;       // 每笔交易承担的账户余额风险 %
input double   InpSL_ATR          = 1.5;       // 止损 = ATR × 该倍数
input double   InpTP_ATR          = 3.0;       // 止盈 = ATR × 该倍数
input int      InpATRPeriod       = 14;
input int      InpMaxSpreadPoints = 60;        // 最大点差（点），0 = 不限制
input ulong    InpMagic           = 20261111;
input string   InpTradeComment    = "KronosGoldWS";
input bool     InpAllowLong       = true;
input bool     InpAllowShort      = true;
input bool     InpCloseOnOpposite = true;

string g_lastSignalBarTime = "";

//+------------------------------------------------------------------+
//| 扁平 JSON 解析辅助函数（Python 发来的是扁平对象，没有嵌套）          |
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
//| 持仓辅助函数（只认自己 Magic 号的持仓）                            |
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
//| 执行一个信号（与 HTTP 版规则相同）                                 |
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

// 通过同一条 socket 把简短状态回发给 Python（可选，尽力而为，失败忽略）。
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
//| 取出队列里所有 WS 消息，对每个新信号执行操作                         |
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
//| 初始化 / 反初始化 / 逐笔报价                                       |
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
   EventSetTimer(60);  // 心跳：定期检查服务端是否还活着
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
      PollWebSocket();  // 定时器里也取一次，以防报价稀疏时漏掉消息
}

void OnTick()
{
   PollWebSocket();
}
//+------------------------------------------------------------------+
