# 黄金模型 EA — MQL5 + Python 分工

> 注意：这份是 **HTTP 版（备用）**。2026-11-11 已改走 **WebSocket 版，见 `README_WS.md`**：EA 做服务端（C++ DLL 起 WS 服务），Python 做客户端推信号。交易逻辑两版一致，HTTP 版留着当备用。

基于已训好的黄金微调模型（`D:\Kronos\finetune_csv\finetuned\XAUUSD_all_timeframes_base\`，8077 页面上叫「黄金微调模型」）。

## 分工（按你的要求）

| 一边 | 干什么 |
|---|---|
| **MQL5 EA** `KronosGoldEA.mq5` | 数据通信 + 开仓/平仓。每个新 K 线收完，向 8077 发一次 `WebRequest`，拿到信号后按风控开单、设止损止盈、反向信号平仓反手。 |
| **Python** 8077 `/api/ea_signal` | 解析模型返回数据。跑金模型、拿预测 K 线、算出方向/幅度/ATR 止损距离，返回一个扁平 JSON，EA 只用 `StringFind` 读几个字段，不用解析 120 根 K 线。 |

流程：新 H1 K 线 → EA POST `{"symbol":"XAUUSDm","timeframe":"H1","pred_len":12,"horizon":12}` → Python 自己从同一台 MT5 终端取最近 400 根 K 线 → 金模型预测未来 12 根 → Python 算信号 → EA 开单。

信号规则（Python 里算，参数 EA 可调）：
- 第 12 根预测收盘价比现价涨 ≥ 阈值（默认 0.10%）→ `LONG`
- 跌 ≤ -阈值 → `SHORT`，否则 `FLAT`（不新开单）
- 止损距离 = ATR×1.5，止盈距离 = ATR×3.0；手数 = 每笔风险 1% 账户余额 ÷ 止损距离算出

## 文件

- `KronosGoldEA.mq5` — EA 源码，放 MT5 的 `MQL5\Experts\`
- `patch_ea_signal.py` — 给 8077 的 `app.py` 加 `/api/ea_signal` 的补丁，放 `D:\Kronos\ea\`（**不要放进 `D:\Kronos\webui\`，Flask 重载会反复重启**）

## 部署步骤（在 Windows 主机上）

1. **Python 端加接口**
   ```powershell
   mkdir D:\Kronos\ea
   # 把 patch_ea_signal.py 拷到 D:\Kronos\ea\
   D:\Kronos\.venv-gold\Scripts\python.exe D:\Kronos\ea\patch_ea_signal.py
   # 补丁会自动备份 app.py，语法检查通过后，重启任务计划里的 KronosWebUI 任务
   ```
   验证（约 10–20 秒出结果）：
   ```powershell
   Invoke-RestMethod -Uri http://127.0.0.1:8077/api/ea_signal -Method Post -ContentType 'application/json' -Body '{"symbol":"XAUUSDm","timeframe":"H1","pred_len":12,"horizon":12}'
   # 应返回 {"signal":"LONG"/"SHORT"/"FLAT","change_pct":...,"last_close":...,...}
   ```

2. **MT5 白名单（关键一步，不然 EA 报 4014）**
   MT5 → 工具 → 选项 → EA 交易 → 勾选「允许 WebRequest 的 URL 列表」，添加：
   ```
   http://127.0.0.1:8077
   ```

3. **EA 编译安装**
   - 把 `KronosGoldEA.mq5` 拷到 MT5 的 `MQL5\Experts\`（终端在 `D:\mt5\`，数据文件夹见 MT5 → 文件 → 打开数据文件夹）
   - MetaEditor 打开编译，0 错误 0 警告才能用
   - 挂到 `XAUUSDm` 的 H1 图表上，勾选「允许算法交易」

4. **先跑模拟盘**
   账户是 Exness-MT5Trial5（277839177）模拟盘。先看专家日志：每次新 K 线应有一条 `signal=... change=...%`，信号方向对了才开单。确认无误再考虑实盘。

## EA 可调参数（输入里改）

- `InpThresholdPct` 0.10 — 信号阈值（%），越大信号越少、越稳
- `InpRiskPercent` 1.0 — 每笔风险占余额 %
- `InpSL_ATR` 1.5 / `InpTP_ATR` 3.0 — 止损/止盈的 ATR 倍数
- `InpHorizon` 12 / `InpPredLen` 12 — 看未来第几根定方向
- `InpMaxSpreadPoints` 60 — 点差超过这个不交易
- `InpMagic` 20261111 — 只认自己这个 Magic 的持仓

## 注意

- 预测一次约 10–20 秒（GPU），EA 只在新 K 线时请求一次，H1 一小时一次，不影响交易。
- 8077 停了 EA 就拿不到信号，会在日志里报错、等下一根 K 线再试，不会乱开单。
- **还没回测。** 这套信号逻辑（12 根后涨跌超 0.10% 才交易）在历史数据上的胜率、回撤都还没验证，务必先模拟盘跑一段时间，下一步就是回测框架。
