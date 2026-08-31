# 剩余工作交接文档（第三期收尾 + 治理待办）

> 生成时间：2026-08-31。供后续 Agent / 用户继续实施。
> 上层总计划：`docs/superpowers/plans/2026-08-31-portfolio-trading-system-and-notification-digest.md`

## 1. 当前完成基线（截至 2026-08-31 晚）

| 阶段 | 内容 | 提交 | 状态 |
|---|---|---|---|
| 第一期 Task 0 | 排行榜修复保护（评测窗口越界 + pending 展示） | `77d45d06` | ✅ 已部署 |
| 第一期 Task 8 | 持仓基线重建（账户 id=3，6 只，现金 158.88） | 数据操作 | ✅ 已生效 |
| 第二期 Task 1/2 | 通知聚合（10:05/11:35/15:15 三简报 + 紧急旁路） | `ea870422` | ✅ 已启用 |
| 修复 | 复盘/Agent 指数改用实时源（require_realtime） | `24e3de80` | ✅ 已部署 |
| 第三期 Task 5/6/7 | 交易计划引擎 + 8% 回撤状态机 + 只读 API | `2f8bec46` | ✅ 已部署已启用 |

服务器：`root@8.163.74.65`，部署命令 `bash /root/deploy_dsa.sh [commit]`（git pull 流）。
当前已启用开关：`NOTIFICATION_DIGEST_ENABLED=true`、`PORTFOLIO_TRADING_PLAN_ENABLED=true`、`SCREENING_ENABLED=true`、`INTRADAY_WATCH_ENABLED=true`。

## 2. 剩余功能任务（第三期收尾）

### Task 3：策略分时 cohorts（数据基础，建议先做）

给 `src/services/screening/strategies/*.yaml` 增加 `evaluation_profile` 元数据：

```yaml
evaluation_profile:
  cohort: preopen_previous_close | opening_0945 | intraday_1000
  risk_tier: low | medium | high
  max_position_pct: 0.10
```

分组归属（总计划第 8 节）：

- `preopen_previous_close`（09:25 用上一交易日快照）：balanced_alpha、blue_chip_income、dual_low、low_volatility_quality、momentum_quality、oversold_reversal、quality_value、shrink_pullback
- `opening_0945`：dragon_board
- `intraday_1000`：capital_heat、volume_breakout

要做的事：
1. 09:25 评测只跑 preopen cohort（盘中数据策略不得用昨日快照参与同一榜）。
2. 排行榜（`StrategyOutcomeService.get_leaderboard` + Web `StrategyLeaderboardPage`）按 cohort 分三榜：盘前策略榜 / 开盘策略榜 / 盘中策略榜。
3. `opening_0945` 与 `intraday_1000` 的后验评测时机需设计（依赖当日收盘数据，T+1 评测）。

### Task 9：Web 交易计划页面

后端 API 已就绪：`GET /api/v1/portfolio/trading-plan`（登录态访问，返回结构见 `src/schemas/trading_plan.py::PortfolioTradingPlan.to_dict`）。

页面要求（总计划第 14 节）：路由 `/portfolio/trading-plan`（或并入持仓页 tab），展示组合风险状态、当前/目标仓位、现金比例、净值高点与回撤、每只股票计划（动作/理由/止损/止盈/建议数量/阻止原因）、数据更新时间。**不提供任何下单按钮。**

注意：涉及 Web UI 的 PR 需附页面截图（AGENTS.md 要求）。

### Task 10：企微交易计划推送

格式参考总计划第 15 节示例（净资产/仓位/风险状态 + 每股动作摘要），必须附带：
`这是规则化风险管理计划，不构成收益保证或自动交易指令。`

建议接入点：15:15 收盘简报合并推送（digest 已上线，作为 portfolio_watch 类事件缓冲即可），或独立 `report` 路由。

### Task 4 残余：评测基建

`77d45d06` 已修窗口越界、失败隔离、pending 展示。仍缺：
- 评测放独立后台 worker（不阻塞 09:30/09:45/10:00 任务）
- 周榜按交易日历判断本周最后交易日
- 通知成功/失败写入批次状态

## 3. 数据一致性待办

| 事项 | 现状 | 需要什么 |
|---|---|---|
| 持仓账户 vs 盯盘列表 | 账户 id=3 有招商银行 200 股；STOCK_LIST（7 只）无招行、含新易盛/中际旭创 | 用户确认招行是否已卖 + 新易盛/中际旭创数量成本（或持仓页截图），再做一次基线对账 |
| 行业集中度上限 30% | 交易计划 v1 未实现（无行业映射） | 依赖股票→行业数据源，二期再做 |
| 防守型大盘股 20% 上限 | 分类未自动判定（只有 high_vol/normal） | 需基本面/波动率分类依据 |

## 4. 安全与治理待办（重要）

1. **SSH 加固**：服务器存在大量爆破登录记录（总计划第 19 节点名）。改密钥登录 + fail2ban，独立任务处理。
2. **凭据轮换**：SSH 密码、DS/GLM/Tushare/Tavily key、企微应用 secret 均在对话中明文出现过，建议全部轮换。
3. **仓库私有化**：fork `zhouruiqiu/daily_stock_analysis` 仍为公开（fork 无法转私有）。方案：新建 private 仓库推送 + 服务器改 deploy key + 删除 fork，会失去 Copilot 免费版对私有库的操作能力。
4. **清理测试残留**：仓库根目录 `PUSH_TEST.md`（git 推送测试遗留，提交 `aab315cb`）应删除。
5. **双通道重复推送**：`NOTIFICATION_ALERT_CHANNELS=wechat_work_app,wechat` 每条告警发两遍。若只留一路，改此配置即可。
6. **GitLab/GitHub 身份隔离**：本地全局 git 身份仍是公司邮箱；仓库级已改为个人邮箱，可加 `includeIf` 按目录自动切换。

## 5. 观察项（无需动作，留意即可）

- **明日（下一交易日）首次全自动简报**：09:25 评测 → 09:30/10:00 盯盘 → 09:45/10:00 选股全部进缓冲，10:05 出第一份自动开盘简报；15:15 出收盘简报。验证 digest_events 表与企微到达。
- **交易计划状态机首次峰值**：峰值=当前权益已入库；回撤从 0 开始累积，跌破 4%/6%/8% 才有状态变化。
- **Akshare 指数日K延迟**：当日 bar 晚间更新；复盘已走实时源不受影响，但依赖日K的其他路径（如 T+1 后验）在盘后立刻跑可能拿不到当日 bar，评测任务安排在晚间之后更稳。

## 6. 硬性红线（继承总计划第 19 节，任何 Agent 必须遵守)

- 不自动下单、不撤单、不解锁交易账户；不输出无条件"买入"。
- 不承诺盈利；所有计划类输出带免责声明。
- 不保存东方财富登录凭据。
- 不将截图现价当长期实时价；数据过期不给确定性建议。
- 不伪造旧持仓卖出记录；持仓变更走基线对账。
- 动手前备份数据库（`/root/dsa-backup-*`）；`.zcode/` 不入库；commit message 英文无 Co-Authored-By。
- 新配置必须同步 `.env.example`；用户可见变更更新 `docs/CHANGELOG.md`（扁平格式）。

## 7. 验证命令

```bash
# 后端
cd /opt/daily_stock_analysis && venv/bin/python -m pytest tests/ -m "not network" -q

# 前端
cd apps/dsa-web && npm run lint && npm run build

# 部署
bash /root/deploy_dsa.sh          # 最新
bash /root/deploy_dsa.sh <commit> # 回滚
```
