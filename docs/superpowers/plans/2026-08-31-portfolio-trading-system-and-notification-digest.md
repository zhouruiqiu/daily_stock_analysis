# 持仓交易系统、策略治理与统一通知实施计划

> 本计划用于指导后续 Agent 在 `daily_stock_analysis` 中实施通知聚合、策略可用性治理、组合风险控制和交易计划功能。

## 1. 目标

在不接入自动下单、不保存东方财富登录凭据、不承诺盈利的前提下，完成以下能力：

1. 将分散的日常推送尽量合并为固定时点的综合简报。
2. 保留跌破支撑、大盘快速下跌和系统故障等紧急即时告警。
3. 审查并按真实数据依赖重新划分全部选股策略的执行时点。
4. 建立基于账户净值、仓位上限、风险预算和最大回撤的交易计划系统。
5. 将东方财富当前持仓作为新的权威持仓基线接入系统。
6. 增加 Web 组合风险和交易计划页面。
7. 完成测试、提交、推送和阿里云部署。

交易系统只生成计划和仓位建议，不自动提交、修改或取消任何订单。

## 2. 当前工作区与部署状态

### 2.1 本地工作区

执行前必须检查：

```bash
git status -sb
git diff --check
git diff --stat
git log --oneline --decorate -5
```

当前本地已有未提交的策略排行榜修复，主要涉及：

```text
apps/dsa-web/src/api/strategyEvaluation.ts
apps/dsa-web/src/pages/StrategyLeaderboardPage.tsx
docs/CHANGELOG.md
src/services/strategy_evaluation_scheduler.py
src/services/strategy_outcome_service.py
tests/test_strategy_evaluation_scheduler.py
tests/test_strategy_outcome_service.py
```

这些改动已经部署到阿里云，但尚未 commit/push。后续 Agent 必须保留并先独立提交，不能被新功能覆盖。

本地目录：

```text
.zcode/
```

属于本地产物，不得提交。

### 2.2 阿里云

项目目录：

```text
/opt/daily_stock_analysis
```

Systemd 服务：

```text
dsa
dsa-intraday-watch
```

最近排行榜修复备份：

```text
/opt/daily_stock_analysis/deploy-backups/leaderboard-fix-20260827_104105
```

## 3. 当前持仓证据

### 3.1 截图中的实际持仓

截图时间：`2026-08-27 10:58`。

| 股票 | 数量 | 截图现价 | 成本 | 市值 | 浮盈亏 |
|---|---:|---:|---:|---:|---:|
| 胜宏科技 | 100 | 263.180 | 375.088 | 26,318 | -29.835% |
| 铜冠铜箔 | 300 | 113.580 | 119.203 | 34,074 | -4.717% |
| 亨通光电 | 200 | 70.030 | 63.146 | 14,006 | +10.902% |
| 招商银行 | 200 | 39.400 | 43.532 | 7,880 | -9.492% |
| 桐昆股份 | 200 | 24.910 | 23.815 | 4,982 | +4.598% |
| 华电辽能 | 1000 | 14.520 | 14.795 | 14,520 | -1.859% |

组合估算：

```text
持仓市值：约101,780元
持仓成本：约114,163.51元
浮亏：约12,383.51元
相对持仓成本收益：约-10.85%
前两大持仓合计：约59.34%
截图疑似可用现金：158.88元，写库前必须确认字段含义
```

股票代码必须通过项目股票索引或权威行情源确认，预计为：

```text
胜宏科技 300476
铜冠铜箔 301217
亨通光电 600487
招商银行 600036
桐昆股份 601233
华电辽能 600396
```

不得在未验证代码的情况下修改数据库。

### 3.2 阿里云现有数据库持仓已过期

远端 `account_id=2` 当前仍保存：

```text
300476 胜宏科技 100股 成本375.088
301047 义翘神州 100股 成本89.550
300136 信维通信 500股 成本98.010
600036 招商银行 600股 成本40.331
600580 卧龙电驱 200股 成本36.275
```

因此当前盘中风险提示并不是基于截图里的最新持仓。新功能上线前必须先完成持仓基线对账。

不得伪造旧持仓的卖出价格、手续费或成交日期。

## 4. 风险配置

用户选择：

```text
风险档位：均衡型
最大组合回撤：8%
```

默认规则：

```text
单笔风险预算：账户净值的0.8%
单日亏损熔断：1.5%
单周回撤降仓：4%
组合最大回撤：8%
普通单股仓位上限：15%
高波动股票仓位上限：10%
防守型大盘股仓位上限：20%
同一行业或高相关主题上限：30%
最低现金比例：20%
```

总仓位上限跟随大盘状态：

```text
sustained_up   80%
neutral        60%
sustained_down 40%
```

禁止：

- 仅因亏损而补仓。
- 超过单股或行业上限后继续加仓。
- 数据过期时生成确定性交易建议。
- 使用历史收盘价冒充实时价触发交易。
- 将策略排名直接转换成买单。
- 自动下单、自动撤单或自动解锁交易账户。

## 5. Task 0：保护当前排行榜修复

1. 核对本地与阿里云排行榜文件 hash。
2. 运行已有排行榜、调度和 Web 测试。
3. 排除 `.zcode/`。
4. 建议独立提交：

```text
fix: repair strategy outcome evaluation and pending leaderboard states
```

5. 推送到：

```text
git@github.com:zhouruiqiu/daily_stock_analysis.git
```

不要将后续交易系统混入该修复提交。

## 6. Task 1：统一通知事件模型

新增统一事件结构，例如：

```python
NotificationDigestEvent(
    event_type: str,
    severity: str,
    occurred_at: datetime,
    title: str,
    summary: str,
    stock_code: Optional[str],
    dedup_key: str,
    payload: Dict[str, Any],
)
```

事件类型：

```text
market_state
market_crash
portfolio_watch
support_breach
dynamic_screening
strategy_evaluation
strategy_outcome
decision_signal
system_failure
```

建议新增：

```text
src/services/notification_digest_service.py
```

职责：

- 暂存普通事件。
- 按固定时间窗口合并。
- 对相同事件去重。
- 严重事件绕过聚合立即发送。
- 发送失败时保留未送达事件。
- 服务重启后不重复发送已确认消息。

如需持久化，新增：

```text
notification_digest_events
notification_digest_runs
```

## 7. Task 2：综合推送时段

### 10:05 开盘综合简报

合并：

- 大盘状态。
- 09:25盘前策略。
- 09:30持仓盯盘。
- 09:45强势题材。
- 10:00动态选股。
- 当前持仓交易计划。

### 11:35 午盘综合简报

合并：

- 持仓变化。
- 盈亏变化。
- 支撑风险。
- 大盘状态。
- 午后仓位建议。

### 15:15 收盘综合简报

合并：

- 当日持仓表现。
- 组合净值和回撤。
- 策略后验结果。
- 次日观察计划。
- 每周最后一个交易日附周榜。

### 必须立即发送

- 持仓跌破有效支撑。
- 大盘快速下跌。
- 单股快速下跌。
- 组合达到8%回撤。
- 盘中服务崩溃。
- 数据源全部不可用。

### 通知渠道

当前阿里云同时配置了企业微信群机器人和企业微信应用消息，可能造成重复推送。

默认建议日常只启用：

```text
wechat_work_app
```

是否保留群机器人必须通过配置控制，不应硬编码。

## 8. Task 3：策略可用性与分组

给策略增加显式元数据：

```yaml
evaluation_profile:
  cohort: preopen_previous_close | opening_0945 | intraday_1000
  risk_tier: low | medium | high
  preferred_regimes: []
  max_position_pct: 0.10
  minimum_holding_days: 1
  requires_realtime_snapshot: false
  requires_intraday_volume: false
```

### 09:25 使用上一交易日完整快照

```text
balanced_alpha
blue_chip_income
dual_low
low_volatility_quality
momentum_quality
oversold_reversal
quality_value
shrink_pullback
```

### 09:45 或 10:00 使用盘中快照

```text
capital_heat
dragon_board
volume_breakout
```

这三个策略依赖当日成交额、换手率、涨跌幅、量比和突破状态，不能再使用09:25不完整快照参与同一排行榜。

排行榜按 cohort 分开：

```text
盘前策略榜
开盘策略榜
盘中策略榜
```

## 9. Task 4：修复评测基础设施

必须完成：

1. 09:25策略评测放入独立后台 worker。
2. 评测不能阻塞09:30、09:45和10:00任务。
3. 增加运行锁和幂等 claim。
4. 上一交易日快照必须验证真实 `trade_date`。
5. 禁止将 Sina 集合竞价实时数据标记为 `previous_close`。
6. completed批次禁止覆盖。
7. 手动重试只补跑失败策略。
8. outcome只读取missing或pending。
9. evaluated和unable不重复请求Tushare。
10. leaderboard的`window`参数必须生效。
11. 周榜通过交易日历判断本周最后交易日。
12. 通知成功/失败写入批次状态。
13. Web展示全部策略、零候选、失败、pending、evaluated和unable。

## 10. Task 5：交易计划核心服务

新增：

```text
src/services/portfolio_trading_plan_service.py
src/schemas/trading_plan.py
```

输入：

```text
PortfolioSnapshot
MarketTrendState
RealtimeQuote
TrendAnalysisResult
StrategyEvidence
DecisionSignal
RiskProfile
```

单股输出建议结构：

```python
TradingPlanItem(
    stock_code,
    stock_name,
    current_quantity,
    current_weight_pct,
    target_weight_min_pct,
    target_weight_max_pct,
    max_additional_amount,
    max_additional_quantity,
    action,
    action_reason,
    entry_condition,
    reduce_condition,
    exit_condition,
    stop_price,
    trailing_stop_price,
    take_profit_levels,
    risk_budget_amount,
    blocking_reasons,
    data_quality,
)
```

组合输出：

```python
PortfolioTradingPlan(
    total_equity,
    invested_amount,
    cash_amount,
    exposure_pct,
    target_exposure_pct,
    peak_equity,
    drawdown_pct,
    risk_mode,
    new_positions_allowed,
    items,
    generated_at,
)
```

动作只允许：

```text
hold
observe
reduce
exit
add_if_confirmed
blocked
```

不输出无条件“买入”。

## 11. Task 6：仓位计算

核心公式：

```python
risk_budget = total_equity * 0.008
stop_distance = max(
    current_price - stop_price,
    current_price * minimum_stop_pct,
)
risk_based_quantity = floor(
    risk_budget / stop_distance / lot_size
) * lot_size
cap_based_quantity = floor(
    (position_cap_amount - current_market_value)
    / current_price
    / lot_size
) * lot_size
max_additional_quantity = max(
    0,
    min(risk_based_quantity, cap_based_quantity),
)
```

附加约束：

- 总仓位上限。
- 单股上限。
- 行业/主题上限。
- 现金下限。
- A股100股交易单位。
- 停牌和涨跌停。
- 实时价格有效性。
- 支撑位有效性。
- 数据时间和质量。

由于截图组合接近满仓，第一版计划应优先输出降低集中度和恢复现金，不应继续扩大总风险。

## 12. Task 7：8%组合回撤状态机

新增持久化状态：

```text
normal
caution
defensive
drawdown_lock
```

状态边界：

```text
回撤 < 4%    normal
4%–6%        caution
6%–8%        defensive
>= 8%        drawdown_lock
```

`drawdown_lock`行为：

- 禁止新增高风险股票。
- 禁止亏损补仓。
- 总仓位目标降至40%。
- 仅保留趋势和支撑仍有效的仓位。
- 通过净值恢复或用户显式解除。

组合净值高点必须持久化，不能随服务重启重置。

## 13. Task 8：东方财富持仓基线

不要篡改旧交易历史。

推荐流程：

1. 备份数据库。
2. 将旧`account_id=2`标记为历史账户或归档。
3. 新建新的东方财富持仓基线账户。
4. 使用截图中的数量和成本建立opening baseline。
5. 现金字段无法确认时标记unknown，不能伪造。
6. 验证全部股票代码。
7. 生成对账摘要。
8. baseline写入：

```text
source=eastmoney_screenshot
as_of=2026-08-27 10:58
```

建议新增：

```text
portfolio_position_baselines
portfolio_reconciliations
```

使用baseline作为新账户起点，不能制造不存在的历史成交记录。

## 14. Task 9：Web交易计划页面

在持仓页面增加：

- 组合风险状态。
- 当前仓位和目标仓位。
- 现金比例。
- 净值高点和回撤。
- 行业集中度。
- 单股仓位上限。
- 每只股票的交易计划。
- 阻止加仓原因。
- 数据更新时间。
- 计划历史。

可以增加路由：

```text
/portfolio/trading-plan
```

页面不提供自动下单按钮。

## 15. Task 10：企业微信交易计划

示例：

```text
📊 组合交易计划
净资产：101,939
当前仓位：99.8%
目标仓位：60%
现金：158.88
风险状态：防守

🚨 胜宏科技
当前仓位：25.9%
高波动上限：10%
动作：禁止加仓，等待减仓条件

⚠️ 铜冠铜箔
当前仓位：33.5%
上限：10%
动作：降低集中度
```

必须包含：

```text
这是规则化风险管理计划，不构成收益保证或自动交易指令。
```

## 16. 测试计划

### 16.1 单元测试

- 8%回撤边界。
- 单日和单周熔断。
- 单股仓位上限。
- 行业上限。
- 现金下限。
- 100股取整。
- 实时价或支撑缺失。
- 停牌和涨跌停。
- 禁止亏损补仓。
- 策略cohort。
- 普通通知聚合。
- 紧急消息旁路。
- 去重和服务重启。
- baseline幂等。

### 16.2 集成测试

- 09:25任务不阻塞09:30和09:45。
- 10:05只发送一份综合简报。
- 跌破支撑继续立即发送。
- Web交易计划API。
- 持仓baseline回放。
- 排行榜按cohort。
- 通知失败不影响持仓和策略数据。

### 16.3 验证命令

```bash
PATH="$PWD/venv/bin:$PATH" ./scripts/ci_gate.sh

cd apps/dsa-web
npm run lint
npm run build
```

涉及Web界面时，交付或PR说明必须提供页面截图。

## 17. 建议提交顺序

```text
fix: repair strategy evaluation scheduler and outcome contracts
feat: add strategy evaluation cohorts and availability audit
feat: add notification digest aggregation
feat: add portfolio trading plan risk engine
feat: add portfolio baseline reconciliation
feat: add trading plan web workspace
docs: document risk rules and notification schedule
```

不要将全部改动堆在一个提交中。

## 18. 部署顺序

1. 本地完整验证。
2. 提交并推送到：

```text
git@github.com:zhouruiqiu/daily_stock_analysis.git
```

3. 备份阿里云：

```text
运行时代码
.env
data/stock_analysis.db
static/
```

4. 首次部署时保持：

```env
PORTFOLIO_TRADING_PLAN_ENABLED=false
NOTIFICATION_DIGEST_ENABLED=false
```

5. 完成schema和API smoke。
6. 导入持仓baseline并核对。
7. 启用Web只读页面。
8. 执行一轮不发送通知的交易计划。
9. 再开启综合推送。
10. 验证`dsa`和`dsa-intraday-watch`。
11. 保存明确的rollback ID和命令。

## 19. 后续Agent硬性要求

- 用户已授权代码、Git、数据库和部署操作，但仍必须先备份。
- 不得自动下单。
- 不得承诺盈利。
- 不得保存东方财富登录凭据。
- 不得将截图现价当作长期有效实时价。
- 不得伪造旧持仓卖出记录。
- 必须保留本地未提交的排行榜修复。
- `.zcode/`不得提交。
- 阿里云存在大量SSH失败登录记录；SSH加固应作为独立任务处理。
