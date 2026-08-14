# 自选股盘中盯盘独立运行模式设计

## 目标

为现有 `IntradayWatchWorker` 增加独立运行入口，使服务器可在不启动 Web 服务、
不执行每日完整 AI 分析的情况下，持续监控 Web 自选股并在 A 股交易时段推送
技术面简报。

## 范围

- 新增 CLI 参数 `--intraday-watch-only`。
- 该模式仅启动 `IntradayWatchWorker` 的周期循环，不进入 `run_full_analysis()`。
- 自选股继续使用 Web 和系统设置共同维护的 `STOCK_LIST`；每轮重新加载配置，
  页面新增或删除自选股后无需重启。
- 使用 `INTRADAY_WATCH_INTERVAL_MINUTES` 控制间隔，服务器保持 30 分钟。
- 使用 `INTRADAY_WATCH_MARKET=cn`，仅在 `INTRADAY` 和
  `CLOSING_AUCTION` 阶段分析并推送；盘前、午休、盘后、周末和休市日跳过。
- 推送继续使用 `NOTIFICATION_ALERT_CHANNELS` 的 alert 路由。
- 不新增信号去重或阈值触发；只要处于交易时段且至少一只自选股分析成功，
  每轮推送一份完整简报。

## 运行架构

服务器保留现有两个相互独立的 systemd 进程：

```text
dsa
  main.py --serve-only --host 0.0.0.0 --port 8000
  负责 Web/API

dsa-intraday-watch
  main.py --intraday-watch-only
  负责盘中自选股简报
```

`dsa-intraday-watch` 不监听网络端口，和 `dsa` 不发生端口冲突。服务启动后立即
执行一次 `run_once()`；之后按配置间隔执行。非交易时段的立即执行只记录跳过，
不会发消息。

## CLI 行为

`--intraday-watch-only` 是最高优先级的专用运行模式：

- 不启动 Web/API。
- 不运行回测、大盘复盘、普通单次分析或每日 schedule task。
- 若 `INTRADAY_WATCH_ENABLED=false`，记录未启用并以非零状态退出，避免 systemd
  显示“运行正常但永远不工作”。
- 收到 `SIGTERM`/`KeyboardInterrupt` 后退出，交由 systemd 管理生命周期。
- 单轮异常由 worker 隔离，周期循环继续运行。

## 配置与部署

服务器 `.env` 增加：

```dotenv
INTRADAY_WATCH_ENABLED=true
INTRADAY_WATCH_INTERVAL_MINUTES=30
INTRADAY_WATCH_MARKET=cn
```

部署时先运行通知只读诊断，从服务器现有配置中选择已启用渠道写入
`NOTIFICATION_ALERT_CHANNELS`，并确认 alert 路由至少命中一个可用渠道，再创建
并启动 `dsa-intraday-watch.service`。systemd 单元使用项目现有 `venv` 和工作
目录，设置 `Restart=always`，但不保存任何通知凭据。

## 错误处理

- 自选股为空：本轮跳过。
- 市场阶段无法判断：本轮跳过。
- 单只股票数据失败：记录 warning，继续其他股票。
- 全部股票失败：不推送。
- 通知失败：记录错误，下一周期继续。
- 进程级启动失败：systemd 标记失败并按重启策略处理。

## 测试与验收

- CLI 契约测试证明专用模式不会调用 `run_full_analysis()`。
- 周期循环测试证明启动立即执行、使用配置间隔并可正常终止。
- 关闭开关时专用模式明确失败，不进入循环。
- 现有 `IntradayWatchWorker` 测试继续覆盖自选股、交易阶段、分析和通知。
- 服务器验收检查两个 systemd 服务均为 active、Web 健康接口正常，并在日志中
  看到非交易时段跳过或交易时段分析记录。

## 回滚

停止并禁用 `dsa-intraday-watch.service`，删除新增 systemd 单元，并将
`INTRADAY_WATCH_ENABLED` 恢复为 `false` 或移除。现有 `dsa` Web 服务不需要改变。
