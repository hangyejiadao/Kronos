# 回测框架 — README（2026-11-11 已按 WS 实盘对齐）

回测 = 先在历史数据上验证信号逻辑，再决定上不上模拟盘。EA 信号逻辑
（H1、12 根后涨跌超 0.10% 才交易、SL ATR×1.5 / TP ATR×3.0、每笔风险 1%）
到现在还没在真实数据上验证过，这是上实盘前必须补的一步。

## 文件（都在本目录，部署时拷到主机 `D:\Kronos\ea\`）

| 文件 | 作用 |
|---|---|
| `backtest.py` | 回测重放：读价格 CSV + 信号缓存 CSV，逐根模拟成交，算收益率/胜率/盈亏比/最大回撤/夏普。纯 CPU，秒出结果，适合扫参 |
| `generate_signals.py` | 主机端信号生成：调金模型在验证集上逐窗预测，写成信号缓存 CSV。只在主机 GPU 上跑 |
| `ea_config.yaml` | 两边共用的参数（阈值、ATR 倍数、风险、点差/滑点） |
| `EA_DESIGN.md` | 设计说明（含两段式为什么分开、成本假设） |

## 2026-11-11 改了什么（与 WS 实盘对齐）

- **反向信号反手**：之前 `backtest.py` 只在 SL/TP 出场，反向信号来了也不平仓，
  跟 EA 的 `InpCloseOnOpposite=true` 不一致。已改：反向信号当根开盘价平仓并反手，
  出场原因记为 `reverse`。合成数据实测：27 笔里 3 笔 `reverse`，方向翻转正常。
- **止损距离用实盘同款 ATR**：实盘 `/api/ea_signal` 发给 EA 的 `atr` 是
  14 根 high-low 均值，回测之前用的是经典真实波幅 ATR。已改默认 `atr_mode: proxy`
  （与实盘一致），`--atr-mode true` 可作对照。H1 黄金缺口小，两者结果接近。
- **信号生成与实盘同参**：`generate_signals.py` 从 lookback 512 / pred_len 48
  改成 **400 / 12**，与桥调 `/api/ea_signal` 的参数完全一致。H1 验证集
  （约 5,830 根，步长 6 → 约 970 次预测）GPU 时间从约 1.5–2 小时降到约 35 分钟。

## 在主机上跑（SSH 恢复后）

1. 先停任务计划里的 `KronosWebUI`（8077 占着 8G 显存，信号生成要独占 GPU；
   跑完再恢复 8077；客户在看页面时别跑）。
2. 信号生成（任务计划挂着跑，SSH 断了也不停）：
   ```powershell
   D:\Kronos\.venv-gold\Scripts\python.exe D:\Kronos\ea\generate_signals.py --timeframe H1
   # 输出 D:\Kronos\finetune_csv\signals\signals_H1.csv，约 35 分钟
   ```
3. 回测 + 扫参（CPU，秒级）：
   ```powershell
   D:\Kronos\.venv-gold\Scripts\python.exe D:\Kronos\ea\backtest.py --prices D:\Kronos\finetune_csv\data\XAUUSD_H1.csv --signals D:\Kronos\finetune_csv\signals\signals_H1.csv --config D:\Kronos\ea\ea_config.yaml --out-trades D:\Kronos\finetune_csv\signals\trades_H1.csv
   # 扫阈值：--threshold-pct 0.0005 / 0.0015 / 0.002 / 0.003 各跑一次，不用重新跑模型
   ```

只用验证集（每周期后 10%）回测，与 `evaluate_gold_vs_base.py` 同一切分；
训练集上回测会高估，模型见过那些数据。

## 还没做的 / 注意

- 真实数据回测还没跑：信号缓存要等主机跑完 `generate_signals.py` 才有。
- `spread_usd: 0.35` 是占位符，必须以 MT5 上 `XAUUSDm` 的实测点差为准
  （回测结论对点差很敏感）；滑点默认 0.10 美元。
- 回测在下一根开盘价成交，实盘 EA 是收到信号后按市价成交，两者有几秒到
  一分钟的时差；回测不做 EA 的“点差超 60 点跳过”那条规则。
- 本地验证：`py_compile` 通过；合成价格 + 合成信号跑通（27 笔，含 3 笔反手），
  数字本身没有意义（随机游走），只验证了引擎 mechanics。
