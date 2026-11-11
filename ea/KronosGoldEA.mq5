//+------------------------------------------------------------------+
//|                                                KronosGoldEA.mq5  |
//|  黄金 EA，由微调后的 Kronos-gold 模型驱动（8077）。                  |
//|                                                                  |
//|  职责分工（按要求）：                                             |
//|    MQL5 EA  ：数据通信（WebRequest）+ 开仓/平仓、风险管理。         |
//|                                                                  |
//|    Python   ：8077 的 /api/ea_signal 运行模型、解析预测 K 线，      |
//|               返回一个扁平的信号 JSON：                             |
//|               signal / change_pct / last_close / pred_close /    |
//|               up_ratio / atr / sl_dist / tp_dist。                |
//|               EA 只解析这几个扁平字段，不会自己解析 120 根预测 K 线。|
//|                                                                  |
//|                                                                  |
//|  流程：新 H1 K 线收盘 -> POST 到 127.0.0.1:8077/api/ea_signal     |
//|        -> LONG / SHORT / FLAT -> 开仓、反手或持有。                |
//|  Python 自己从同一台 MT5 终端取最近 400 根 K 线，EA 不需要发送      |
//|  OHLC 数据或日期。                                                |
//|                                                                  |
//|  !!! 使用前必读 !!!                                               |
//|  1. MT5 -> 工具 -> 选项 -> EA 交易 -> 勾选                          |
//|     “允许 WebRequest 的 URL 列表”，并添加：                         |
//|        http://127.0.0.1:8077                                     |
//|     不加的话，WebRequest 会报 4014 错误。                          |
//|  2. 8077 必须处于运行状态且已加载黄金模型（启动时会自动在 CUDA 上    |
//|     加载 kronos-gold）。                                          |
//|  3. 先在模拟账户（Exness-MT5Trial5）上测试。                       |
//|     这个 EA 还没有回测过。                                        |
//+------------------------------------------------------------------+
#property copyright "Kronos Gold EA"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

//--- 输入参数
input string   InpServerUrl      = "http://127.0.0.1:8077"; // 8077 Python 服务端地址
input string   InpSymbol         = "XAUUSDm";               // MT5 里的黄金品种
input ENUM_TIMEFRAMES InpTimeframe = PERIOD_H1;             // 信号周期
input int      InpPredLen        = 12;                      // 模型每次预测的 K 线根数
input int      InpHorizon        = 12;                      // 用第几根预测 K 线判断方向
input double   InpThresholdPct   = 0.10;                    // 触发交易的最小涨跌幅 %（0.10 = 0.10%）
input double   InpRiskPercent    = 1.0;                     // 每笔交易承担的账户余额风险 %
input double   InpSL_ATR         = 1.5;                     // 止损 = ATR × 该倍数
input double   InpTP_ATR         = 3.0;                     // 止盈 = ATR × 该倍数
input int      InpATRPeriod      = 14;                      // ATR 周期（EA 端校验用）
input int      InpMaxSpreadPoints = 60;                     // 最大点差（点），0 = 不限制
input ulong    InpMagic          = 20261111;                // Magic 号
input string   InpTradeComment   = "KronosGold";            // 订单备注
input bool     InpAllowLong      = true;
input bool     InpAllowShort     = true;
input bool     InpCloseOnOpposite = true;                   // 出现反向信号时平仓并反手
input int      InpRequestTimeoutMs = 120000;                // WebRequest 超时（模型约 10-20 秒）

datetime g_lastBarTime = 0;

//+------------------------------------------------------------------+
//| 把 ENUM_TIMEFRAMES 映射成 Python 要求的字符串（M1/H1/……）          |
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
//| 扁平 JSON 解析辅助函数：Python 返回的 JSON 没有嵌套对象，            |
//| 直接用 StringFind 解析就够了（不需要 JSON 库）。                    |
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
//| 向 Python 请求信号。失败时返回 ""。                                |
//| 输出参数从扁平 JSON 回复中填充。                                   |
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
   if(ArraySize(post) > 0) ArrayResize(post, ArraySize(post) - 1); // 去掉末尾的 \0

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
      PrintFormat("KronosGold: close failed, error=%d", GetLastError());
      return false;
   }
   Print("KronosGold: position closed");
   return true;
}

//+------------------------------------------------------------------+
//| 按固定比例风险算手数：每笔风险为余额的一定比例，                      |
//| 止损距离（价格）-> 通过跳动值换算成每手亏损。                       |
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
//| 直接从图表取 ATR（合理性校验 / 止损的备用值）                       |
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
//| 执行一个信号                                                       |
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

   // 点差过滤
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

   // 止损/止盈：优先用 Python 发来的基于 ATR 的距离，拿不到时用本地 ATR
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
//| 初始化                                                           |
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
//| OnTick：每根新 K 线只执行一次（H1 = 每小时一次）                   |
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
