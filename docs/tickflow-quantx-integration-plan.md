# TickFlow Stock Panel × QuantX 整合集成方案

> 状态：P1(数据打通)/P2(只读代理)/P3(严谨回测)已实施；P4 中"信号转发"与"registry
> 只读视图"因 QuantX 侧无可用端点而未实施(详见第 7.2/8.1 节调研结论)，"研究数据代理"
> 部分完成(大盘复盘+回测历史已接入，决策日志/IC分析未接入)。
> 定位：发挥两个项目特长的总体整合蓝图
> 关联文档：`docs/quantx-data-enhancement-technical-plan.md`（L1 数据层已实施方案）
> 最后更新：2026-07-17

---

## 1. 背景与目标

本地存在两个量化项目，能力高度互补：

- **TickFlow Stock Panel**（本项目）：前端体验好，覆盖选股、分析、监控、复盘全流程，但回测引擎偏轻量、实盘交易为占位页。
- **QuantX**（`/Users/james/quant/QuantX`）：数据、回测、实盘、研究基础设施深厚，但 Web 前端（Vue）打磨程度低。

目标：**以 TickFlow 前端为统一驾驶舱，以 QuantX 为引擎室**，形成一套研究 → 验证 → 实盘 → 复盘的完整闭环，同时不破坏任何一方的架构约束。

### 1.1 明确不做

- 不合并两个代码库，不迁移任何一方的存量代码。
- 不修改 QuantX 源码（与 L1 数据方案保持一致的约束）。
- 不绕过 QuantX 的 strategy registry 晋级门禁自动下单。
- 不在 TickFlow 进程内 import rqalpha / vnpy 等重依赖。
- 不废弃 TickFlow 自有回测引擎（定位不同，双引擎并存）。
- **TickFlow 不执行交易、不直连经纪商/QMT/vnpy Gateway、不新增交易页面**——下单与执行 100% 留在 QuantX 侧，TickFlow 只做只读展示与信号转发（详见第 7 节）。

---

## 2. 两个项目的能力盘点

### 2.1 TickFlow Stock Panel

| 模块 | 现状 | 评价 |
|------|------|------|
| 前端 | React 18 + TS + Tailwind，20+ 页面（Dashboard/Screener/行业/概念/涨停梯队/监控/复盘/回测/财务/自选等） | **核心资产**，交互细节打磨好 |
| 数据层 | Polars + Parquet，TickFlow 主源 + 插件化增强（QuantX 插件已接入） | 架构清晰，插件机制可复用 |
| 回测 | `backend/app/backtest/engine.py`（约 1600 行）：向量化日频，撮合支持 close_t / open_t+1，成本模型（佣金+印花税+滑点），移动止盈损，因子回测 + 策略回测两套前端页面 | 适合**交互式快速迭代**，秒级出图；非事件驱动，语义近似 |
| 策略 | 内置策略 + AI 生成器（`strategy/ai_generator.py`）+ 监控规则引擎 | 有特色（AI 生成），缺生命周期管理 |
| 实盘 | 无（原占位页 `Trading.tsx` 已被 upstream 因合规原因删除，不复活；TickFlow 不执行交易，只读展示 QuantX 持仓/决策） | 按设计留白，非缺陷 |
| 复盘 | Review 页 + market_recap | 有 UI 骨架，缺决策级数据 |

### 2.2 QuantX

| 模块 | 现状 | 评价 |
|------|------|------|
| 数据 | `DataStore` 标准表：日 K 2005~now（24.8M 行）、daily_basic、**moneyflow（资金流）**、**limit_list（涨停板明细）**、**top_list（龙虎榜）**、**hsgt（北向）**、概念/行业图谱、5m/1m 分钟线 | 深度远超 TickFlow 数据源 |
| 回测 | rqalpha 真源（事件驱动）+ fast_backtest（向量化）+ vnpy 栈（tick 级盘中）；入口 `quantx backtest <strategy_id>`；registry.yaml 单一真相源 | **严谨验证**级别 |
| 实盘 | `TradeExecutor`（xqshare→QMT）、`LiveTradingRunner`、`StopOrderManager`、`PositionMonitor`（7 条盘中规则 + LLM 中文解读，只建议不下单）、vnpy 打板执行 | 已生产化 |
| 研究 | strategy registry（idea→candidate→validated→paper→live 晋级门禁）、scout AI 选股、LLM 决策日志 + T+5/T+10 前向收益打分、IC 回放、walk-forward | 完整的研究方法论 |
| API | `apps/api`（FastAPI）：trading / monitoring / position / risk / ic_analysis / walk_forward / timing_backtest 等路由 | **可直接代理**，无需新开发 |
| 前端 | `apps/web`（Vue） | 弱项，整合后可逐步退役 |

### 2.3 互补结论

两个项目几乎没有重叠浪费：TickFlow 缺的（严谨回测、实盘、研究闭环）正是 QuantX 的强项；QuantX 缺的（前端体验）正是 TickFlow 的强项。

---

## 3. 实施形态：双项目分工，不建新项目

**决策：保持两个项目各自独立演进，不新建第三个统一项目。**

- tickflow-stock-panel = **驾驶舱**：全栈保留（后端即 BFF 聚合层，不可砍），承担选股/分析/监控/复盘 UI、快速回测、聚合展示。
- QuantX = **引擎室**：数据真源、严谨回测、实盘执行、策略晋级、研究闭环，现有部署（VPS/QMT）不动。

理由：

1. **新项目会同时失去两个上游**：tickflow 上游（shy3130）迭代快，fork 断供后每个新功能都要手工搬运；QuantX 已跑通实盘环境，搬迁风险大。
2. **架构约束不兼容**：QuantX 治理严格（registry 真相源、pre-commit 拦截、vnpy 3.11 独立环境），tickflow 轻量插件式，硬合并必伤其一。
3. **风险画像不同**：展示工具挂了没损失，实盘链路挂了要命；进程/仓库隔离本身就是风控。

分工与依赖方向：

| | tickflow（驾驶舱） | QuantX（引擎室） |
|---|---|---|
| 集成代码 | **几乎全部在此**：`plugins/quantx/`、`services/quantx_*`、独立 api 路由 | 仅最薄一层：apps/api 信号接收 webhook；策略登记走既有 registry 流程 |
| 依赖方向 | tickflow → QuantX（单向） | 不感知 tickflow 存在，api 保持通用 |
| git 策略 | 保留 `upstream` remote 定期合并上游；集成代码收敛在新增文件，主干文件保持最小 diff | 正常演进 |

跨项目契约（信号 webhook payload、回测结果 schema）先以本文档定义为准；仅当两侧需要 import 同一份 schema 时再抽小契约包，现阶段不做。

---

## 4. 总体架构

### 4.1 核心原则

> **TickFlow 后端作为 BFF（Backend for Frontend）聚合层：轻的进程内 import，重的走进程边界。**

| 集成对象 | 方式 | 理由 |
|----------|------|------|
| QuantX 数据（DataStore） | 进程内 import `quantx_data`（已实现，`plugins/quantx/bridge.py`） | 纯读 parquet，依赖轻（polars），无环境冲突 |
| QuantX 回测引擎 | **subprocess** 调 `quantx backtest <id>` | rqalpha 依赖重；vnpy 需 Python 3.11 与 TickFlow 环境隔离 |
| QuantX 持仓/决策/研究（只读） | **HTTP** 代理 QuantX `apps/api` | 接口现成；故障隔离，QuantX 挂掉不拖垮 TickFlow；TickFlow 不执行交易 |
| TickFlow 信号 → QuantX 决策日志 | **Webhook**（alert_handler 新增通道） | 转发信号供打分，非下单指令 |

### 4.2 架构图

```text
┌─────────────────────────────────────────────────────────┐
│                TickFlow 前端 (React)                     │
│  选股/分析 │ 监控中心(+只读持仓/决策) │ 回测(快速/严谨) │ 复盘/策略 │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│              TickFlow 后端 (FastAPI, BFF)                │
│                                                          │
│  L1 数据增强        L2 回测适配        L3/L4 代理层       │
│  (已实现 ✅)        BacktestEngine     QuantXProxy       │
│  plugins/quantx     Adapter            service(只读+转发) │
└──────┬──────────────────┬──────────────────┬─────────────┘
       │ in-process       │ subprocess       │ HTTP
       │ import           │                  │
┌──────▼─────────┐ ┌──────▼─────────┐ ┌──────▼─────────────┐
│ quantx_data    │ │ quantx CLI     │ │ QuantX apps/api    │
│ DataStore      │ │ backtest <id>  │ │ trading/monitoring │
│ (parquet 只读) │ │ (rqalpha/fast) │ │ position/risk/...  │
└────────────────┘ └────────────────┘ └──────┬─────────────┘
                                             │ (交易执行 100% 留在 QuantX 侧)
                                      ┌──────▼─────────────┐
                                      │ LiveTradingRunner  │
                                      │ TradeExecutor(QMT) │
                                      │ PositionMonitor    │
                                      └────────────────────┘
```

---

## 5. L1 数据层（已实施 ✅ + 扩展方向）

### 5.1 已实施

见 `docs/quantx-data-enhancement-technical-plan.md`：

- QuantX 作为数据增强插件注册（`backend/app/plugins/quantx/`）。
- TickFlow 主源，QuantX 按 `(symbol, date)` 补缺不覆盖。
- 统一以不复权 OHLCV 入库，只复权一次（含 `repair_quantx_bar_units.py` 修复脚本）。
- 实时行情 provider chain（QuantX 优先，覆盖不足回退 TickFlow）。
- 维表字段级 coalesce；盘后自动增量 + 前端手动补全；设置页可见可关。

### 5.2 扩展：个性化数据接入 ext_data

QuantX 有四张 TickFlow 数据源完全没有的表，天然适合走已有的 `ext_data` 预设机制（JOIN 进分析页）：

| QuantX 表 | 关键字段 | 前端受益页面 |
|-----------|----------|--------------|
| `std_moneyflow` | 大单/特大单买卖额、净流入 | 行业/概念分析（龙头因子可加"主力净流入"）、个股分析 |
| `std_limit_list` | 封单额、开板次数、连板统计、首/末次封板时间 | 涨停梯队（现有页面直接增强） |
| `std_top_list` | 龙虎榜买卖席位金额、上榜原因 | 个股分析、复盘 |
| `std_hsgt` | 北向资金净流入 | Dashboard、大盘概览 |

实施要点：

1. 新增 ext_data 预设（复用 `services/ext_presets.py` 机制），数据经 `plugins/quantx/bridge.py` 的 DataStore 单例读取。
2. 注意单位换算：moneyflow 单位为**万元**，hsgt 为**亿元**（QuantX CLAUDE.md 明确标注），入库前统一为元。
3. README 中作者已表态"tickflow 数据源没有人气/资金流向等个性化数据，将开放自有第三方数据"——本扩展与上游项目方向一致，后续可考虑贡献回上游。

---

## 6. L2 回测层：双引擎并存

### 6.1 定位划分

| | TickFlow 引擎（保留） | QuantX rqalpha（新增接入） |
|---|---|---|
| 用途 | 交互式快速迭代、因子初筛 | 上线前严谨验证 |
| 语义 | 向量化近似撮合 | 事件驱动、逐日撮合、涨跌停/停牌/退市处理完整 |
| 速度 | 秒级 | 分钟级 |
| 数据 | TickFlow parquet（QuantX 补缺后） | QuantX DataStore 全量（2005~now） |

研究流程：**TickFlow 快速引擎筛想法 → 一键送 QuantX 严谨复核 → 结果同屏对比。**

### 6.2 实施（已完成 ✅）：HTTP 适配，不用 subprocess

实际实现比最初设想的 subprocess 方案更简单：QuantX `apps/api` 已提供
`POST /api/strategies/execute-config` 同步执行完整 rqalpha 回测并返回结果
（含结果落库），因此复用 P2 建立的 `QUANTX_API_BASE_URL` HTTP 通道即可，
不需要管理 QuantX 自己的 Python 解释器/subprocess/输出解析。

- `backend/app/services/quantx_strategy_translator.py`：TickFlow 策略 → QuantX
  `StrategyConfig` JSON 翻译器。**只翻译可声明式表达的子集**——scoring（打分
  排名）+ limit + 止损/持有期上限 + exclude_st；scoring 字段必须命中已人工核对
  QuantX `packages/quantx_factors/registry.py` 的白名单（momentum/rsi/
  volume_ratio/atr/macd_hist/kdj_k），命中白名单外字段直接拒绝翻译并返回
  错误信息，不做静默近似。策略自定义的 `filter()` 技术形态判断（如 60 日新高、
  放量突破）**不翻译**——QuantX `FilterSpec` 只支持简单 field/op/value 比较，
  无法表达任意布尔逻辑。universe 固定用 QuantX 内置 `a_share` 股票池（全 A
  股剔除 ST/科创板/北交所）。
- `backend/app/services/quantx_backtest_adapter.py`：翻译 → POST 执行 → 结果
  归一化（`daily_nav[].nav` → `equity_curve[].value`，`summary` 直接透传）。
  未配置/超时（独立的 `QUANTX_BACKTEST_TIMEOUT`，默认 300s，明显长于监控代理
  的 5s）/连接失败/HTTP 错误一律降级为 `ok=False` + `error`，不抛异常。
- `POST /api/backtest/strategy/rigorous`：从 `StrategyEngine` 取
  `StrategyDef.meta/basic_filter/stop_loss/max_hold_days` 喂给适配器。返回的
  `translation_warnings` **必须展示给用户**，不能被当作与 TickFlow 原策略
  等价的验证结果。
- 前端 `api.strategyBacktestRigorous()` 客户端方法已就绪，但**未接入**
  `StrategyBacktest.tsx`（现有页面 2400+ 行，深度 UI 改动风险与本次改动量不
  匹配，留作后续独立评估）。

约束与注意：

- 策略翻译**只覆盖可映射子集**，且是"打分排名逻辑"的复核，不是策略的完整
  移植；`filter()` 里的自定义技术形态过滤条件不参与严谨回测，两侧结果不可
  直接等同比较。
- 结果口径差异要在 UI 明示：两引擎撮合/成本模型不同，收益数字必然有差，这
  正是严谨复核的价值。

### 6.3 附加收益

QuantX `apps/api` 已有 `ic_analysis`、`walk_forward`、`timing_backtest` 路由，可在 L4 阶段以代理方式给 TickFlow 因子回测页增加 IC 分析和 walk-forward 视图，无需自研。

---

## 7. L3 决策展示层：只读代理 + 信号转发（不涉及交易执行）

**边界声明**：TickFlow 自始至终不执行交易、不直连券商/QMT/vnpy Gateway，不新增独立的「交易」页面。下单、撮合、风控拦截 100% 留在 QuantX 侧的 `TradeExecutor` / `StopOrderManager` / `LiveTradingRunner`。TickFlow 只做两件事：**读**QuantX 的持仓与决策建议、**转发**监控信号给 QuantX 的决策日志做打分。两者都不触碰经纪商接口，因此与 upstream「research-only boundaries」的合规方向不冲突。

> 背景：upstream 在 `697ff2a fix(compliance): reinforce research-only boundaries (#108)` 中删除了旧的 `Trading.tsx` 占位页（原注释规划的是 TickFlow 自己桥接 QMT/掘金/vnpy 直接下单）。本方案不复活该页面、不采用其信号直连下单的设计——下面的只读展示/信号转发功能改为并入监控中心、复盘等**现有**页面。

### 7.1 只读代理（已完成 ✅）

新增 `backend/app/services/quantx_proxy.py` + `backend/app/api/quantx_monitor.py`，HTTP 代理 QuantX `apps/api`；前端并入**现有**监控中心页（`QuantxMonitorPanel`），不新增独立页面。端点对照实际验证过的 QuantX 源码（`apps/api/routes/trading.py`、`monitoring.py`），与最初设想的端点名有出入（如 QuantX 并无独立的 broker-positions/risk 路由）：

| TickFlow 端点（已实现） | 代理目标（QuantX 现成，已核实真实存在） | 前端展示位置 |
|----------------------|------------------------|----------|
| `GET /api/quantx/positions` | `GET /api/trading/positions` + `GET /api/trading/asset`（合并） | 监控中心只读卡片（持仓/成本/现价/盈亏/总资产） |
| `GET /api/quantx/decisions` | `GET /api/monitoring/position-decisions?minutes=240` | **盘中持仓决策建议**（HOLD/REDUCE/EXIT/TAKE_PROFIT/ADD + LLM 中文解读），并入监控中心 |
| `GET /api/quantx/alerts` | `GET /api/monitoring/alerts`（含风控类告警，QuantX 并无独立的 `/api/risk/*` 告警端点） | 监控中心告警 |
| `GET /api/quantx/status` | `GET /api/monitoring/runtime-status` | QuantX 服务健康度（不可用时前端明确降级提示，仅影响该卡片） |
| `GET /api/quantx/market-review` | `GET /api/insight/market-review/latest`（只读加载已保存报告，不触发 LLM 生成） | 复盘页 `QuantxReviewCard`（"第二意见"，与 TickFlow 自有复盘并存） |
| `GET /api/quantx/runs` | `GET /api/runs`（策略回测运行历史） | 复盘页 `QuantxReviewCard` |

实施要点：

- 配置项：`QUANTX_API_BASE_URL`、`QUANTX_API_TIMEOUT`（默认 5s，只读代理用）、`QUANTX_BACKTEST_TIMEOUT`（默认 300s，严谨回测用，见第 6 节）。
- **故障隔离**：QuantX api 不可达（未配置/超时/连接失败/HTTP 错误）一律返回 `available: False` + `error` 原因，不抛异常；前端组件在不可用时直接 `return null`（不占页面空间、不打扰未配置该功能的用户），不阻塞其余功能。
- 这些数据是**只读展示**，前端不提供任何下单/撤单操作入口；`quantx_proxy.py` 模块函数名与端点均只含 GET，从不代理 `/api/trading/order`、`/api/trading/cancel`、`/api/trading/stop-orders`（POST）等写入端点。

### 7.2 信号转发（未实施 ❌ — QuantX 侧无可用端点）

**调研结论：QuantX `apps/api` 目前没有任何 HTTP 端点可以写入决策日志**（`packages/quantx_data/decision_journal.py` 只能进程内 `import` 使用，`apps/api/routes/` 下没有对应路由）。要实现"TickFlow 信号 POST 进 QuantX 决策日志"，唯一路径是给 QuantX 新增一个写入端点——这直接违反本方案 §1.1 的"不修改 QuantX 源码"红线。

因此原计划的 webhook 转发**暂不实施**。若未来确有需求，正确做法是：

1. 先在 QuantX 侧（独立评估、独立 PR）新增一个**只写决策日志、不触发下单**的最小端点（如 `POST /api/decision-journal/external-signal`），QuantX 团队自行决定是否接受这类外部输入。
2. TickFlow 侧再对接该端点，遵循下面的红线不变。

**红线**（若未来实施，必须遵守）：

1. TickFlow 信号**只进建议队列/决策日志，绝不直接下单**。
2. 不绕过 promotion gate：TickFlow 策略要自动执行，必须先在 registry 走完 idea→…→live 晋级。
3. 决策日志的 `baseline_rank` 机制可为 TickFlow 信号提供 A/B 对照打分。

### 7.3 与 vnpy 栈的关系

QuantX 的 vnpy 栈（打板/tick 级）是独立回路，**本方案不桥接**。TickFlow 的日频/分钟级信号只对接 `LiveTradingRunner` 一侧。

---

## 8. L4 策略与研究层

### 8.1 策略注册表只读视图（未实施 ❌ — QuantX 侧无可用端点）

**调研结论**：QuantX `apps/api` 没有任何端点读取 `strategies/registry.yaml` 的生命周期状态（idea/candidate/validated/paper/live）。已核实的 `GET /api/strategies/catalog` 只读取 `configs/strategies/*.json` + 回测结果库，不包含 registry 的晋级状态字段。

直接读取 QuantX 项目目录下的 `registry.yaml` 文件（跨文件系统读取）在两者部署于不同主机时不成立，且绕开 QuantX api 直读其内部文件违背"通过 apps/api 交互"的架构原则，因此**不采用**。若未来需要，同 7.2：应由 QuantX 侧新增一个只读端点（如 `GET /api/strategies/registry`），而不是 TickFlow 侧变通读取。

TickFlow AI 生成器产出的策略推送到 QuantX 的流程（先在快速引擎验证 → 用户手动导出 config → 人工登记进 QuantX registry）暂时仍是纯人工操作，未做任何自动化对接。

### 8.2 研究数据代理进复盘页（部分完成）

| 数据 | 来源 | 状态 | 展示位置 |
|------|------|------|----------|
| 大盘复盘报告 | `GET /api/insight/market-review/latest`（只读加载已保存报告） | ✅ 已完成（见 7.1） | Review 复盘页 `QuantxReviewCard` |
| 策略回测运行历史 | `GET /api/runs`（ResultStore） | ✅ 已完成（见 7.1） | Review 复盘页 `QuantxReviewCard` |
| LLM 决策日志 + 前向收益打分 | decision_journal | ❌ 未实施 — 同 7.2，QuantX 侧无读取端点（`packages/quantx_data/decision_journal.py` 只能进程内 import，`apps/api/routes/` 无对应路由） | — |
| IC 分析 / walk-forward | `ic_analysis` / `walk_forward` 路由 | 未调研（apps/api 确有这两个路由文件，但本轮未核实具体端点签名，留作后续） | 因子回测页（待定） |

---

## 9. 实施顺序与工作量估计

| 阶段 | 内容 | 依赖 | 风险 | 工作量估计 |
|------|------|------|------|-----------|
| **P0** ✅ | 数据增强插件（已完成） | — | — | 已完成 |
| **P1** ✅ | ext_data 接入 moneyflow / limit_list / top_list（个股维度）+ hsgt 接入 Dashboard（大盘聚合，不走 ext_data） | P0 的 bridge | 低（纯读） | 已完成 |
| **P2** ✅ | 只读代理（持仓/决策建议/告警/运行健康），并入监控中心 | QuantX api 可达 | 低（纯读，故障隔离） | 已完成 |
| **P3** ✅ | 回测适配器（rigorous 引擎 + 结果归一化，HTTP 而非 subprocess） | 策略翻译器 | 中（口径差异，已在文档/UI 明示） | 已完成后端；前端未接入 2400+ 行的 StrategyBacktest.tsx（留作后续） |
| **P4** 部分 | 大盘复盘+回测历史代理进复盘页 ✅；信号转发 webhook ❌ 与 registry 只读视图 ❌ 因 QuantX 侧无可用端点未实施 | P2 的 proxy | 已实施部分为低风险纯读；未实施部分需先在 QuantX 侧新增端点 | 已完成可行部分 |

原则：**先读后写、先数据后决策展示**。P1/P2 都是纯读、立刻增值、不涉及交易执行，优先落地。TickFlow 全程不新增交易页面、不直连经纪商。P4 的两个未实施项不是"没做"，而是调研后确认 QuantX 侧当前没有对应端点，实施会违反"不修改 QuantX 源码"的既定约束——留待后续与 QuantX 侧协调是否新增只读端点。

---

## 10. 风险与对策

| 风险 | 说明 | 对策 |
|------|------|------|
| Python 环境冲突 | QuantX 统一 3.13、vnpy 需 3.11；rqalpha 依赖重 | 进程边界隔离：数据层 import 仅限 quantx_data（轻）；回测 subprocess 用 QuantX 自己的解释器（`QUANTX_PYTHON_BIN`） |
| QuantX 数据新鲜度 | QuantX 靠手动 `ingest_master.py --incremental`，无自动调度 | TickFlow 盘后 pipeline 增加新鲜度检查步骤：DataStore 最新日期落后 N 个交易日则前端提示"QuantX 数据陈旧，补缺已跳过" |
| 单位/口径不一致 | moneyflow 万元、hsgt 亿元；两引擎撮合模型不同 | 单位在接入层统一换算并写测试；回测结果 UI 明示引擎口径差异 |
| QuantX api 故障传导 | 代理层超时/宕机 | 短超时（2~5s）+ 明确 unavailable 状态 + 前端降级卡片；禁止影响监控中心/复盘页其余功能 |
| 交易安全 | 误把只读展示/信号转发当成交易通道 | TickFlow 前端不提供任何下单/撤单操作入口；转发的信号只进 QuantX 决策日志做打分，不触发自动执行；自动执行必须过 registry 晋级门禁，执行逻辑 100% 在 QuantX 侧 |
| 上游同步 | 本项目是 fork/使用 shy3130 的开源项目，深度改动可能与上游更新冲突；upstream 已表态「research-only boundaries」（见 `697ff2a`，删除了原 Trading.tsx 占位页） | 集成代码收敛在 `plugins/quantx/`、`services/quantx_*`、独立 api 路由中，减少对主干文件的侵入；不复活/新增交易执行类页面，与 upstream 合规方向保持一致；通用增强机制可考虑贡献回上游 |
| Symbol 格式 | 两边均为 `.SH/.SZ/.BJ` canonical | 低风险；边界处仍统一走 `quantx_data.symbol` 转换函数 |

---

## 11. 配置项汇总（新增）

| 配置项 | 用途 | 阶段 |
|--------|------|------|
| `QUANTX_PACKAGES_PATH` | quantx_data import 路径 | P0 ✅ |
| `QUANTDATA_ROOT` | DataStore 数据目录 | P0 ✅ |
| `QUANTX_API_BASE_URL` | QuantX apps/api 地址，只读代理 + 严谨回测共用 | P2 ✅ |
| `QUANTX_API_TIMEOUT` | 只读代理请求超时（秒，默认 5） | P2 ✅ |
| `QUANTX_BACKTEST_TIMEOUT` | 严谨回测请求超时（秒，默认 300，rqalpha 事件驱动回测可能耗时数分钟） | P3 ✅ |

`QUANTX_PROJECT_PATH` / `QUANTX_PYTHON_BIN`（subprocess 方案）未采用——P3 改用 HTTP 调 QuantX 的 `POST /api/strategies/execute-config`，复用 `QUANTX_API_BASE_URL`。`QUANTX_WEBHOOK_*`（P4 信号出口）未实施，见第 7.2 节。

所有配置遵循现有 `app/config.py` Settings 模式，未配置时对应功能自动隐藏/降级，保持"可发现、可配置、可关闭"的插件哲学。
