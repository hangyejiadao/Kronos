# 黄金模型 EA — WebSocket 版（当前方案）

HTTP 版（`KronosGoldEA.mq5` + `/api/ea_signal`）已写好留作备用，**现在改走 WebSocket，按你的要求：EA 做服务端**。

## 架构

```
Python 桥 ws_signal_bridge.py            EA (MT5, XAUUSDm H1)
  (WS 客户端)                               (WS 服务端)
  新收盘 H1 K 线 ──> 调 8077 /api/ea_signal ─┐
  (金模型跑预测，Python 解析成信号)          │
  ── WS 推送 {"signal":"LONG",...} ──────────>│ C++ DLL 内起 WS 服务
                                                127.0.0.1:8081
                                                EA 轮询收信号 → 开/平仓
                                                可选回发 ea_ack / 状态
```

- **服务端在 EA 里**：C++ DLL `KronosWsServer.dll` 被 EA 加载后，在 MT5 进程内起 WebSocket 服务，只监听 `127.0.0.1:8081`，不对外网暴露。
- **为什么用 C++ 而不用 C#**：MQL5 的 `#import` 只能直接加载原生 DLL。C# 程序集 MT5 加载不了，要再包一层 C++/CLI，绕一圈，所以服务端直接用 C++ 写，零第三方库（Winsock2 + Windows CryptoAPI 做握手 SHA-1）。
- **Python 还是解析模型**：模型照样在 8077 跑，`/api/ea_signal`（见 `patch_ea_signal.py`）把 120 根预测解析成 `signal / change_pct / last_close / pred_close / up_ratio / atr / sl_dist / tp_dist`，桥只负责把这个扁平 JSON 经 WS 推给 EA。EA 不解析 K 线。
- **谁触发**：桥每 20 秒看一次 MT5 最新收盘 H1 K 线，变了才调一次模型、推一次信号。EA 收到就交易，同一根 K 线的重复信号会按 `bar_time` 去重。

## 文件

| 文件 | 放哪里 | 说明 |
|---|---|---|
| `KronosWsServer.cpp` | 主机上编译 | WS 服务端 DLL 源码，单文件，无依赖。MSVC：`cl /LD /EHsc KronosWsServer.cpp /link ws2_32.lib Crypt32.lib`；MinGW：`g++ -shared -o KronosWsServer.dll KronosWsServer.cpp -lws2_32 -lcrypt32` |
| `KronosWsServer.dll` | `MT5数据文件夹\MQL5\Libraries\` | 编译产物（主机上编译，不要放 `webui` 里） |
| `KronosGoldEA_WS.mq5` | `MT5数据文件夹\MQL5\Experts\` | EA v2，MetaEditor 编译 |
| `ws_signal_bridge.py` | `D:\Kronos\ea\` | Python WS 客户端 + 推信号的桥，纯标准库，不用 pip 装 websocket 库 |
| `patch_ea_signal.py` | `D:\Kronos\ea\` | 给 8077 加 `/api/ea_signal`（桥要调它；已验证可打补丁、语法通过、重复运行幂等） |

## 部署步骤（主机上）

1. **8077 加信号接口**（只需一次）：
   ```powershell
   D:\Kronos\.venv-gold\Scripts\python.exe D:\Kronos\ea\patch_ea_signal.py
   # 重启任务计划里的 KronosWebUI 任务
   ```

2. **编译 DLL**（主机上，任选其一）：
   ```powershell
   # VS 开发者命令提示符
   cl /LD /EHsc KronosWsServer.cpp /link ws2_32.lib Crypt32.lib
   # 或 MinGW
   g++ -shared -o KronosWsServer.dll KronosWsServer.cpp -lws2_32 -lcrypt32 -static-libgcc -static-libstdc++
   copy KronosWsServer.dll <MT5数据文件夹>\MQL5\Libraries\
   ```

3. **EA**：`KronosGoldEA_WS.mq5` 拷到 `MQL5\Experts\`，MetaEditor 编译 0 错误；MT5 → 工具 → 选项 → EA 交易 → 勾选「允许 DLL 导入」。**不再需要 WebRequest 白名单**（WS 不走 WebRequest）。

4. **启动桥**：
   ```powershell
   D:\Kronos\.venv-gold\Scripts\python.exe D:\Kronos\ea\ws_signal_bridge.py
   # 先测试一次：加 --once，只推当前最新收盘 K 线的信号就退出
   D:\Kronos\.venv-gold\Scripts\python.exe D:\Kronos\ea\ws_signal_bridge.py --once
   ```

5. **挂 EA**：挂到 `XAUUSDm` H1 图表，允许算法交易。专家日志应出现 `KronosGoldWS started ... WS server on 127.0.0.1:8081`，桥连上后 EA 每收到信号打一条 `signal=... change=...%`。

## 已验证的

- `patch_ea_signal.py`：在 `app.py` 副本上打补丁成功、编译通过、重复运行不重复打。
- `ws_signal_bridge.py` 的 WS 客户端：对一个本地 WS 服务桩实测通过——握手、客户端掩码发送、服务端 126 长度帧接收都正常。
- `KronosWsServer.cpp`：用桩头文件在 Linux g++ 下 `-fsyntax-only` 通过（真机编译仍需在主机上用 MSVC/MinGW 做一次）；过程中修了两个实 bug：`CryptBinaryToString` 返回长度含结尾 NUL、不能直接进 accept key；`DllMain` 里不要 `WsStop()`（loader lock 下 join 线程可能死锁，改由 EA 的 `OnDeinit` 停服务）。

## 还没做的（需上主机）

- 在主机上真编译出 `KronosWsServer.dll`，MetaEditor 编译 EA v2。
- 端到端：桥 → EA → 下单，模拟盘先跑。
- 回测框架：信号逻辑还没在历史数据上验证，实盘前先模拟盘 + 回测。
