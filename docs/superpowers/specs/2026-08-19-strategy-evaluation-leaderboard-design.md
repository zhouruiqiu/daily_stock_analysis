# 全策略盘前评测与排行榜设计

## 1. 目标

每天在 A 股集合竞价结束后，以统一、可复现且无未来数据污染的口径运行全部内置选股策略，持续跟踪候选股票在后续 1、3、5 个交易日的表现，并通过 Web 策略排行榜和企业微信日报/周报比较策略效果。

本功能是观察性策略实验，不自动交易、不自动修改策略权重，也不把短期收益排名解释为确定性的策略优劣。

## 2. 范围

首版仅覆盖：

- 市场：A 股 `cn`。
- 策略：`src/services/screening/strategies/` 中当次加载成功且 `screening.enabled=true` 的全部内置策略；当前为 11 个，不写死数量或名称。
- 每个策略：默认保存 Top 5。
- 后验周期：T+1、T+3、T+5 交易日。
- 基准：沪深300（`000300`）。
- 展示：Web 独立排行榜页面。
- 通知：企业微信每日结果摘要和每周最后一个交易日的周榜。

首版不包含多年历史回放、自动调参、自动交易、策略权重自动更新、港股/美股、手续费滑点模拟和盘中动态换仓。

## 3. 核心评测口径

### 3.1 09:25 盘前实验

交易日 09:25（`Asia/Shanghai`）启动一次实验批次：

1. 使用上一交易日已经完整形成的日 K、技术指标、成交额、换手率、量比和基本面数据。
2. 获取 09:25 集合竞价价格，仅作为当次候选快照的可见参考字段，不替代上一交易日完整因子。
3. 依次运行全部启用的内置策略，每个策略保存 Top N。
4. 禁用 LLM 重排、随机种子轮换和远程 post analyzer，保留确定性的本地筛选、评分、风险规则和本地 scorecard。
5. 单个策略失败时保存该策略的失败状态和脱敏错误，继续运行其他策略。

现有盘中 `screen()` 依赖当日实时快照，不能直接在 09:25 原样复用。实现需为策略实验建立显式的 `previous_close` 快照模式，避免集合竞价阶段不完整的成交额、换手率和量比导致策略间不公平。

`dragon_board` 等普通运行时要求实时快照的策略，在评测链路中仅豁免“必须是当日实时快照”这一来源要求，仍执行其全部量价、趋势、风险和板块热度过滤；输入统一替换为已冻结的上一交易日收盘快照。该豁免只存在于评测服务，不改变 Web 手工选股和盘中动态选股的生产契约。

### 3.2 模拟入场价

- 权威入场日就是 09:25 选股批次所属的同一交易日。
- 权威入场价是该交易日 09:30 形成的有效开盘价。
- 09:25 集合竞价价不作为最终收益起点。
- 入场日停牌、开盘价缺失、非有限或非正数时保持 `pending`；只有在规则可判定永久无效时才进入 `unable`。
- 不得使用入场日收盘价或更晚价格回填开盘价。

### 3.3 后验收益

对每个候选分别计算：

```text
stock_return_pct = (target_close / entry_open - 1) * 100
benchmark_return_pct = (benchmark_target_close / benchmark_entry_open - 1) * 100
excess_return_pct = stock_return_pct - benchmark_return_pct
```

其中 T+1、T+3、T+5 分别为入场交易日之后第 1、3、5 个开放交易日的收盘价，不包含入场当日收盘。交易日严格使用项目交易日历解析，不按自然日加减。

最大不利波动使用入场日至目标日区间内最低价：

```text
max_adverse_excursion_pct = max(0, (entry_open - min_low) / entry_open * 100)
```

### 3.4 策略聚合

每个策略、实验窗口和 horizon 聚合：

- 候选数量。
- `pending / evaluated / unable` 数量。
- Top N 等权平均个股收益。
- 等权平均沪深300超额收益。
- 正收益比例。
- 跑赢沪深300比例。
- 最大不利波动。

同一股票被多个策略选中时，每个策略保留独立观察记录；同一策略同一实验批次内按规范化股票代码去重。

正式排名至少要求 5 个完整实验交易日且当前 horizon 有足够已评估样本。未满足时仍展示原始计数和已有收益，但状态为“样本不足，仅供观察”，不输出名次。

## 4. 数据模型

新增三张独立表，不改变现有 `screening_runs` 的诊断历史语义。

### 4.1 `strategy_evaluation_runs`

一条记录表示一个交易日的全策略实验批次：

- `id`
- `run_id`：稳定唯一标识。
- `market`
- `selection_date`
- `scheduled_at`
- `started_at / completed_at`
- `status`：`running / partial / completed / failed`
- `strategy_count / completed_strategy_count / failed_strategy_count`
- `top_n`
- `benchmark_code`
- `snapshot_mode`：首版固定 `previous_close`
- `engine_version`
- `summary_json`
- `created_at / updated_at`

唯一键为 `(market, selection_date, engine_version)`，保证重启或重复 tick 幂等。

### 4.2 `strategy_evaluation_picks`

一条记录表示某策略在某实验批次选中的一只股票：

- `id`
- `run_id`
- `strategy`
- `strategy_version`
- `stock_code / stock_name`
- `rank`
- `screen_score / final_score`
- `auction_price`
- `selection_snapshot_json`
- `source_json / warnings_json`
- `created_at`

唯一键为 `(run_id, strategy, stock_code)`；排序用 `(run_id, strategy, rank)` 索引。

### 4.3 `strategy_evaluation_outcomes`

一条记录表示一个 pick 在一个 horizon 和 engine version 下的后验结果：

- `id`
- `pick_id`
- `horizon`：`1d / 3d / 5d`
- `engine_version`
- `status`：`pending / evaluated / unable`
- `entry_trade_date / target_trade_date`
- `entry_open / target_close / min_low`
- `benchmark_entry_open / benchmark_target_close`
- `stock_return_pct / benchmark_return_pct / excess_return_pct`
- `max_adverse_excursion_pct`
- `reason_code`
- `created_at / updated_at / evaluated_at`

唯一键为 `(pick_id, horizon, engine_version)`。同一 engine version 下只有 `pending` 可重试更新，终态不覆盖；规则变化必须提升 engine version。

## 5. 服务边界

### 5.1 `StrategyEvaluationService`

负责：

- 创建或恢复当天实验批次。
- 加载当次可用策略列表。
- 构建一次共享的上一交易日全市场快照。
- 对全部策略执行确定性筛选。
- 保存策略状态和候选快照。
- 生成当日日报数据。

共享快照是公平性边界：同一批次全部策略必须使用相同的数据版本，不能在11次运行中各自重新拉取不同时间点的数据。

### 5.2 `StrategyOutcomeService`

负责：

- 按交易日历生成 pick × horizon 待评估 key。
- 获取股票和沪深300的权威开盘、收盘、最低价窗口。
- 幂等保存后验结果。
- 对缺失的未来行情保持可重试 `pending`。
- 生成策略聚合统计和排行榜。

行情优先复用 DSA `DataFetcherManager` 和已有日 K 缓存。服务器已实际验证 Tushare 的 `trade_cal`、`daily`、`daily_basic`、`index_daily`、`stock_basic` 权限可用；单一数据源失败时继续走现有 fallback。

### 5.3 定时任务

新增独立后台任务，不复用普通每日分析任务状态：

- 09:25：创建并执行全策略实验。
- 09:30 后：补录已形成的入场开盘价，可与 outcome worker 合并执行。
- 每个交易日收盘后：推进所有到期的 T+1/T+3/T+5 outcome。
- 每周最后一个交易日收盘后：生成并发送周榜。

任务必须遵守 A 股交易日历、`Asia/Shanghai` 时区、日期/任务幂等 claim 和进程内防重入锁。进程重启后可恢复未完成的 `running / pending`，不能重复创建候选。

## 6. 配置

新增配置并同步 `.env.example`、配置注册表和文档：

```env
STRATEGY_EVALUATION_ENABLED=false
STRATEGY_EVALUATION_TIME=09:25
STRATEGY_EVALUATION_TOP_N=5
STRATEGY_EVALUATION_HORIZONS=1,3,5
STRATEGY_EVALUATION_BENCHMARK=000300
STRATEGY_EVALUATION_DAILY_NOTIFY=true
STRATEGY_EVALUATION_WEEKLY_NOTIFY=true
```

约束：

- `TOP_N` 范围为 1–20。
- horizons 仅允许 `1,3,5` 的非空子集，重复值去重并升序。
- benchmark 首版只接受 `000300`。
- 功能默认关闭；启用前先验证数据源和通知路由。

## 7. API

在 `/api/v1/screening/evaluation` 下新增只读查询接口：

- `GET /status`：开关、最近调度、正在运行状态、最近错误。
- `GET /runs`：实验批次列表。
- `GET /runs/{run_id}`：当日全部策略状态与 Top N。
- `GET /leaderboard?horizon=5d&window=20`：排行榜。
- `GET /strategies/{strategy}/history`：策略每日候选和收益历史。

首版不提供任意历史批量回填或删除 API。可提供受管理员认证保护的 `POST /run-now` 用于当日失败后的显式重试，但它必须复用同一幂等 run，不创建第二批候选。

## 8. Web 页面

在 `apps/dsa-web/` 新增“策略排行榜”独立页面和导航入口。

页面包含：

1. 顶部状态：最近运行时间、评测进度、数据源、更新时间和异常摘要。
2. Horizon 切换：T+1、T+3、T+5。
3. 排行榜：策略、完整实验日数、有效样本、平均收益、平均超额、正收益率、跑赢基准率、最大不利波动和状态。
4. 今日结果：11个策略的运行状态、候选数量和 Top N。
5. 策略详情：每日候选、入场价、目标价、各 horizon 收益及异常原因。

排序仅对满足样本门槛的策略生效；样本不足和数据异常策略放在正式排名之后，避免把缺失数据当作零收益。

## 9. 企业微信

复用 `NotificationService.send_with_results(..., route_type="alert")` 和现有分片能力。

### 9.1 每日日报

- 实验日期和完成时间。
- 11个策略的成功/失败状态。
- 每个成功策略的 Top N。
- 已到期 outcome 的简要更新。
- 数据源和异常摘要。

### 9.2 每周周榜

- 当前 T+5 正式排名或样本不足状态。
- 前三名和后三名。
- 平均收益、平均超额、正收益率、最大不利波动和有效样本数。
- 明确“不构成投资建议”。

通知失败不回滚已经保存的实验或 outcome；失败必须记录，可在状态接口查看。

## 10. 失败与数据质量

- 09:25 数据不完整：批次保存 `failed` 或 `partial`，不得退回当日盘中快照冒充统一盘前数据。
- 单策略失败：其他策略继续，日报显示失败原因。
- 入场价或目标行情缺失：保持 `pending` 并公平重试。
- 股票停牌、退市或代码冲突：按明确 reason code 进入 `pending` 或 `unable`。
- 基准行情缺失：相关 outcome 不得标记 `evaluated`。
- 数据库写入失败：实验任务视为失败，不能只发通知不留权威记录。
- 通知失败：实验数据保留，记录通知失败状态。

所有持久化 warning、错误和 API 响应必须沿用项目脱敏边界，不保存 token、请求头、完整第三方响应或敏感 URL。

## 11. 验证

### 后端

- 配置解析、非法配置 fail closed。
- 交易日 09:25 调度、非交易日跳过、重复 tick 幂等。
- 同一批次11个策略共享同一快照。
- 单策略失败隔离。
- Top N、代码去重和稳定排序。
- 下一交易日开盘入场与 T+1/T+3/T+5 日期解析。
- pending 重试、终态不可覆盖和 engine version 隔离。
- 沪深300基准、超额收益和最大不利波动计算。
- 排名样本门槛和缺失数据不按零处理。
- 日报、周报和通知失败隔离。
- API Schema 和鉴权边界。

### Web

- loading、空状态、样本不足、partial、failed 和正常排名。
- T+1/T+3/T+5切换。
- 今日策略结果和策略历史详情。
- API失败时保留最近成功数据并显示可见错误。
- lint、build 和页面截图验收。

### 真实环境 smoke

- 验证服务器 Tushare 和至少一个 fallback 日 K 数据源。
- 以通知关闭模式运行一天全策略实验。
- 检查数据库幂等、候选数量、入场日解析和日志脱敏。
- 启用通知后验证一次企业微信日报。

## 12. 发布与回滚

发布前备份远端运行时代码和数据库。部署后先保持 `STRATEGY_EVALUATION_ENABLED=false` 完成 schema、API、Web 和手动 smoke，再显式启用定时任务。

代码回滚：恢复远端运行时代码并重启相关 Systemd 服务。配置回滚：设 `STRATEGY_EVALUATION_ENABLED=false`。数据库表采用追加式兼容设计，代码回滚不自动删除实验数据；如需删除，另行制定并确认数据清理计划。
