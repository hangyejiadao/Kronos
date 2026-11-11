# EA + 回测框架 — 设计草稿（2026-11-10，待用户定路线和周期）

> **2026-11-11 更新：路线已定 — WebSocket 版。**用户 11:01 决定 EA 做 WS 服务端
> （C++ DLL）、Python 做 WS 客户端推信号，文件在
> `~/workspace/goals/gold-model-ea-and-backtest-framework/files/`（`README_WS.md`）。
> 本稿的回测框架已按实盘对齐：`generate_signals.py` 改成 lookback 400 / pred_len 12
> （与桥调 `/api/ea_signal` 的参数一致，GPU 时间从 ~2 小时降到 ~35 分钟）；
> `backtest.py` 修了反向信号不反手的问题（之前只在 SL/TP 出场，现在反向信号
> 当根平仓反手，与 EA 的 `InpCloseOnOpposite=true` 一致），止损距离改用实盘
> 同款 ATR（14 根 high-low 均值，`atr_mode: proxy`）。回测仍未在真实数据上跑过——
> 信号生成要上主机跑，等 SSH 恢复。

来源：用户 2026-11-10 16:27（UTC 08:27）要求“用黄金微调模型写一个 EA，可以用 Python，回测写一个回测框架”；
随后 16:28 问 “MQL 可以跟模型交互吗”，助手给出了 Python EA 与 MQL5+HTTP 两条路，
到本稿为止用户还没选路线、也没定周期。本稿把两条路都做成可直接开工的状态，
只剩这两个决定。

## 推荐结论（先给答案，细节在下面）

- **周期：先做 H1。** H1 有 58,302 根（2014–2026），每小时才收一根 K 线，
  48 根预测约 6 秒、120 根约 14–17 秒（GPU 实测），时间完全够用；
  M1 每分钟就要预测一次，14 秒预测 + 请求往返会吃掉大半根 K 线，而且点差/噪音占比大；
  H4/D1 信号太少，回测验证要等很久。H1 也是之前 PatchTST 试点和 8077 演示用的周期。
- **路线：建议先走 Python EA + Python 回测（用户最初的要求），MQL5 当备选。**
  Python EA 直接用 `MetaTrader5` 包下单、直接调 `KronosPredictor`，不用经过 HTTP，
  8077 挂了也不影响交易；回测和实盘共用同一套信号逻辑（`signal.py` 的规则），
  回测结果才能直接指导实盘参数。MQL5 路线的唯一优势是 EA 跑在 MT5 里面、
  图表上能看到持仓和画线，但要多一层 HTTP，且 MQL5 里解析 JSON 很别扭（见下）。
- **如果坚持要 MQL5，先给 8077 加一个 `/api/signal` 轻接口再写 EA。**
  现在的 `/api/predict` 返回整段 history（400 根）+ forecast（120 根）的完整 K 线，
  MQL5 要从几十 KB 的 JSON 里抠数字；一个只返回
  `{"signal": 1, "last_close": ..., "pred_close_12": ..., "pred_move_pct": ...}`
  的接口，MQL5 解析只要几行。补丁草稿见 `signal_endpoint_patch.md`（只改 app.py，
  不动模型和页面；**本稿没动主机上的 app.py**，等用户定路线再上）。

## 两条路线对比

| | Python EA（推荐） | MQL5 EA + HTTP 调 8077 |
|---|---|---|
| 模型调用 | 直接 `KronosPredictor`，与 `evaluate_gold_vs_base.py` 同一套导入 | `WebRequest()` POST `http://<主机IP>:8077/api/predict` |
| 前置条件 | Windows 主机 + MT5 登录（277839177，Exness-MT5Trial5，demo）+ 8077 不必开着 | 8077 必须一直开着；MT5 需把该 URL 加进 WebRequest 白名单（工具 → 选项 → EA 交易） |
| 信号逻辑 | 与回测共用 `ea_config.yaml` + 信号规则 | 同一套规则，但要在 MQL5 里再写一遍（两处实现，容易走样） |
| 下单 | `MetaTrader5` 包 `order_send` | 原生 `CTrade`，图表可见 |
| 风险 | SSH 断了没事（任务计划挂着）；但 Python 进程挂了 MT5 不知道 | 8077 重启/换模型时 EA 拿不到预测；HTTP 超时要自己重试 |
| 工作量 | 中：EA 脚本 + 回测框架（本稿已搭骨架） | 中偏大：MQL5 EA + JSON 解析 + `/api/signal` 补丁 |

无论哪条路，**回测框架都是 Python 的**（已在 `backtest.py`）：回测要跑大量历史、
扫参数、算指标，MQL5 策略测试器接外部模型反而不方便。

## 共享信号规则（两条路、回测、实盘同一套）

- 品种 `XAUUSDm`（Exness 带 m 后缀，见 `export_xauusd_mt5_chunked.py`），周期 H1（默认，可改）。
- 每根 K 线收盘后跑一次：取最近 `lookback=512` 根（与训练一致，`max_context=512`），
  请求 `pred_len=48`（训练的预测长度；48 根约 6 秒，见 `evaluate_gold_vs_base.py` 头部注释），
  取预测第 12 根收盘价与最后一根历史收盘价比较。
- `pred_move_pct = (pred_close_12 - last_close) / last_close`。
  `> +threshold` 做多，`< -threshold` 做空，否则不动。`threshold` 默认 0.10%，**是待回测扫参的起点，
  不是验证过的数字**——H1 上 12 根的典型波动还没实测，扫参范围见 `ea_config.yaml`。
- 同一时间只持一仓；反向信号出现时平掉旧仓再开新仓；无信号时持有到止损/止盈。
- 止损/止盈按 ATR(14) 算：SL = 1.5×ATR，TP = 3.0×ATR（2:1 盈亏比，默认值，可调）。
- 仓位：每笔固定风险 1% 权益；`手数 = 风险金额 / (SL价差 × 100)`（XAUUSD 1 手 = 100 盎司，
  价格动 1 美元 = 每手 100 美元），向下取到 0.01 手，并设上限。
- 成本：点差 + 滑点都按价格计入成交价。**点差默认值 0.35 美元只是占位符，
  必须以主机 MT5 `symbol_info("XAUUSDm")` 的实测值为准**（8077 的
  `/api/mt5/symbol-info` 就返回 `spread`/`spread_float`）；滑点默认 0.10 美元。
  回测结论对点差很敏感，扫参时要把点差也当变量看。

## 回测框架：为什么分两段（关键设计）

实盘每小时预测一次没问题，但回测要对几千根历史逐根预测：每根约 6 秒，
H1 验证集（后 10%，约 5,830 根）逐根跑要 ~10 小时，全量更久。
所以分两段，**信号生成（慢、GPU）与回测重放（快、CPU）彻底分开**：

1. `generate_signals.py`（只在 Windows 主机上跑，GPU）：按步长（默认每 6 根，即每 6 小时）
   在验证集内生成信号，写成 `signals_<TF>.csv` 缓存。步长是回测专用的折中，
   不是实盘行为——实盘是每根都预测。步长越小越接近实盘，GPU 时间越长；
   步长 6 时 H1 验证集约 970 次预测，约 1.5–2 小时。
2. `backtest.py`（哪里都能跑，纯 CPU）：读价格 CSV + 信号缓存 CSV，逐根重放，
   按点差/滑点模拟成交，算收益率、夏普、最大回撤、胜率、盈亏比、交易次数。
   改阈值/止损/仓位不用重新跑模型，秒出结果，适合扫参。

**只用验证集回测**：与 `evaluate_gold_vs_base.py` 同一切分——每周期后 10% 为验证集，
窗口只在验证集内部取（lookback 可以探入训练集尾部当上下文，未来 48 根必须全在验证集内）。
训练集上回测会高估，因为模型见过那些数据。D1/W1/MN1 验证集太短（MN1 总共才 155 根），
回测只做 M1–H4，H1 先行。

## 文件清单（本稿，本地 workspace，未同步到主机、未运行）

- `ea_config.yaml` — 全部可调参数（周期、阈值、ATR 倍数、风险比例、点差/滑点、步长）。
- `backtest.py` — 回测重放 + 指标，已用合成数据在本地跑通过（见下“已验证”）。
- `generate_signals.py` — 主机端信号生成（调 `KronosPredictor`，与评估脚本同款导入），
  本地只做了语法检查，没跑（模型和数据都在主机上）。
- `KronosGoldEA.mq5` — MQL5 路线草稿（调 `/api/predict`，解析 forecast 第 12 根），
  **未编译**，需在主机 MetaEditor 里编译；若走 MQL 路线，建议先上 `/api/signal` 再简化它。
- `signal_endpoint_patch.md` — 给 8077 加 `/api/signal` 的补丁草稿（未应用）。

## 已验证 / 未验证

- 已验证：`backtest.py` 在本地用合成价格 + 合成信号跑通，
  `python3 -m py_compile backtest.py generate_signals.py` 通过；
  `/api/predict` 的请求/响应形状已对照主机版 `app.py` 核实
  （POST JSON：`symbol/timeframe/lookback/pred_len/temperature/top_p/sample_count`，
  返回 `forecast: [{t,o,h,l,c}]`；模型切换是单独的 `/api/load-model`，`kronos-gold` 为当前加载模型）。
- 未验证：信号阈值、ATR 倍数、步长 6 的回测结果——都要等主机跑完 `generate_signals.py`
  才有；MQL5 草稿未编译；点差/滑点实测值未知。
- 注意：8077 现在正给客户用着 GPU（用户 2026-11-10 早上定的客户页面），
  `generate_signals.py` 跑之前要先停 `KronosWebUI` 任务（评估脚本头部也是这么要求的），
  否则 8GB 显存会被两边抢；跑完再把 8077 恢复。

## 等用户定的两件事（定完就开工）

1. 路线：Python EA（推荐）还是 MQL5 + HTTP？
2. 周期：H1（推荐）还是其他？若选 M1，预测耗时和点差问题要先实测再定。
3. 附带：金模型 vs 基础模型的对比评估（`evaluate_gold_vs_base.py` 已写好，还没跑，
   已在待发送队列里等点头）要不要跟回测一起排——回测用金模型信号，
   对比评估告诉你金模型的方向准确率到底比基础模型好多少，两者互补，不冲突。
