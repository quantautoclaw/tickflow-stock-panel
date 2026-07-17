# TickFlow Stock Panel × QuantX 整合集成方案

> 状态：设计稿（L1 数据层已实施，其余待排期）
> 定位：发挥两个项目特长的总体整合蓝图
> 关联文档：`docs/quantx-data-enhancement-technical-plan.md`（L1 数据层已实施方案）
> 最后更新：2026-07-16

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

---

## 2. 两个项目的能力盘点

### 2.1 TickFlow Stock Panel

| 模块 | 现状 | 评价 |
|------|------|------|
| 前端 | React 18 + TS + Tailwind，20+ 页面（Dashboard/Screener/行业/概念/涨停梯队/监控/复盘/回测/财务/自选等） | **核心资产**，交互细节打磨好 |
| 数据层 | Polars + Parquet，TickFlow 主源 + 插件化增强（QuantX 插件已接入） | 架构清晰，插件机制可复用 |
| 回测 | `backend/app/backtest/engine.py`（约 1600 行）：向量化日频，撮合支持 close_t / open_t+1，成本模型（佣金+印花税+滑点），移动止盈损，因子回测 + 策略回测两套前端页面 | 适合**交互式快速迭代**，秒级出图；非事件驱动，语义近似 |
| 策略 | 内置策略 + AI 生成器（`strategy/ai_generator.py`）+ 监控规则引擎 | 有特色（AI 生成），缺生命周期管理 |
| 实盘 | `frontend/src/pages/Trading.tsx` 为**占位页**，注释中规划了 QMT/掘金/Ptrade/vnpy 桥接 | **最大空白** |
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
| QuantX 实盘/监控/研究 | **HTTP** 代理 QuantX `apps/api` | 接口现成；故障隔离，QuantX 挂掉不拖垮 TickFlow |
| TickFlow 信号 → QuantX | **Webhook**（alert_handler 新增通道） | 与 Trading.tsx 注释中的既有规划一致 |

### 4.2 架构图

```text
┌─────────────────────────────────────────────────────────┐
│                TickFlow 前端 (React)                     │
│  选股/分析/监控 │ 回测(快速/严谨) │ 交易驾驶舱 │ 研究/复盘 │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│              TickFlow 后端 (FastAPI, BFF)                │
│                                                          │
│  L1 数据增强        L2 回测适配        L3/L4 代理层       │
│  (已实现 ✅)        BacktestEngine     QuantXProxy       │
│  plugins/quantx     Adapter            service           │
└──────┬──────────────────┬──────────────────┬─────────────┘
       │ in-process       │ subprocess       │ HTTP
       │ import           │                  │
┌──────▼─────────┐ ┌──────▼─────────┐ ┌──────▼─────────────┐
│ quantx_data    │ │ quantx CLI     │ │ QuantX apps/api    │
│ DataStore      │ │ backtest <id>  │ │ trading/monitoring │
│ (parquet 只读) │ │ (rqalpha/fast) │ │ position/risk/...  │
└────────────────┘ └────────────────┘ └──────┬─────────────┘
                                             │
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

### 6.2 设计：BacktestEngineAdapter

新增 `backend/app/services/quantx_backtest_adapter.py`：

```text
前端回测页新增「引擎」选择: fast(默认, 现有引擎) / rigorous(QuantX)

rigorous 模式流程:
1. 策略翻译: TickFlow 策略定义 → quantx_config JSON
   (写入 QuantX configs/strategies/tickflow_<id>.json;
    或直接引用 registry 中已有 strategy_id, 跳过翻译)
2. 执行: subprocess 调 `quantx backtest <id> --start ... --end ...`
   - 超时控制(如 10 分钟), 输出流式回传进度
   - 环境: 使用 QuantX 自己的 Python 环境, 路径由
     QUANTX_PROJECT_PATH / QUANTX_PYTHON_BIN 配置项指定
3. 结果归一化: 解析 QuantX reports/ 输出 →
   TickFlow 现有回测结果 schema (净值曲线/成交明细/统计指标)
   → 前端零改动复用现有图表组件
4. 缓存: 以 (strategy_hash, start, end, engine) 为 key,
   复用现有回测缓存机制
```

约束与注意：

- **遵守 QuantX 规矩**：新策略必须先登记 `strategies/registry.yaml`（status: idea 起步），不得新增 `scripts/backtests/run_*.py`（其 pre-commit 会拦截）。翻译器落盘走 `configs/strategies/`，属于其允许的 `quantx_config` dialect。
- 策略翻译**只覆盖可映射子集**：TickFlow 的信号型策略（条件进出场 + 止盈损）可映射；AI 生成的任意 Python 策略首期不翻译（提示用户"该策略暂不支持严谨引擎"）。
- 结果口径差异要在 UI 明示：两引擎撮合/成本模型不同，收益数字必然有差，这正是严谨复核的价值。

### 6.3 附加收益

QuantX `apps/api` 已有 `ic_analysis`、`walk_forward`、`timing_backtest` 路由，可在 L4 阶段以代理方式给 TickFlow 因子回测页增加 IC 分析和 walk-forward 视图，无需自研。

---

## 7. L3 实盘交易层：双向桥（填补 Trading 占位页）

这是**收益最大**的一块：TickFlow 缺的正是 QuantX 已生产化的能力，且 `Trading.tsx` 注释中的规划（信号多通道分发、QMT 桥接）与本方案完全吻合。

### 7.1 状态入口（QuantX → TickFlow 前端，纯读，先做）

新增 `backend/app/services/quantx_proxy.py` + `backend/app/api/trading.py`，HTTP 代理 QuantX `apps/api`：

| TickFlow 端点（新增） | 代理目标（QuantX 现成） | 前端展示 |
|----------------------|------------------------|----------|
| `GET /api/trading/positions` | broker positions 路由 | 持仓列表（成本/现价/盈亏） |
| `GET /api/trading/orders` | trading 路由 | 当日委托/成交 |
| `GET /api/trading/decisions` | `GET /api/monitoring/position-decisions?minutes=240` | **盘中持仓决策建议**（HOLD/REDUCE/EXIT/TAKE_PROFIT/ADD + LLM 中文解读）|
| `GET /api/trading/risk` | risk 路由 | 风控告警 |
| `GET /api/trading/status` | system 路由 | QuantX 服务健康度（不可用时前端明确降级提示） |

实施要点：

- 配置项：`QUANTX_API_BASE_URL`（默认 `http://127.0.0.1:8000` 之类，具体端口以 QuantX gateway 配置为准）、超时（2~5s）、认证凭据走 TickFlow 现有 secrets_store。
- **故障隔离**：QuantX api 不可达时返回明确的 `unavailable` 状态而非 500，前端 Trading 页显示"引擎离线"卡片；禁止阻塞其他页面。
- Trading 页 UI：持仓表 + 决策建议流（决策建议是差异化亮点，QuantX 的 LLM 解读直接展示即可）。

### 7.2 信号出口（TickFlow → QuantX，涉及交易链路，后做）

TickFlow 监控产生的 `StrategyAlert` 通过 alert_handler 新增 **webhook 通道** POST 到 QuantX：

```text
StrategyAlert (symbol/type/strategy_id/price/时间)
    → alert_handler 多通道分发 (SSE 已有; 新增 webhook)
    → POST QuantX api (进入决策日志 decision_journal / 建议队列)
    → QuantX 侧: 人工确认 或 StopOrderManager 既有流程执行
```

**红线**（与 QuantX 的闭环原则一致）：

1. TickFlow 信号**只进建议队列/决策日志，绝不直接下单**。QuantX 的原则是"LLM/外部信号提议，数据裁决，下单走人工或 StopOrderManager"。
2. 不绕过 promotion gate：TickFlow 策略要自动执行，必须先在 registry 走完 idea→…→live 晋级。
3. 决策日志的 `baseline_rank` 机制天然给 TickFlow 信号提供 A/B 对照——接入后 TickFlow 信号的 T+5/T+10 前向收益会被自动打分，形成质量反馈。

### 7.3 与 vnpy 栈的关系

QuantX 的 vnpy 栈（打板/tick 级）是独立回路，**本方案不桥接**。TickFlow 的日频/分钟级信号只对接 `LiveTradingRunner` 一侧。

---

## 8. L4 策略与研究层

### 8.1 策略注册表只读视图

- TickFlow 策略页新增"QuantX 策略库"标签：展示 `strategies/registry.yaml` 的 id / hypothesis / status / 晋级产物（经 QuantX api 或直接读 yaml）。
- **只读**：生命周期管理（晋级/下线）留在 QuantX 侧，TickFlow 不写入，避免破坏其单一真相源。
- TickFlow AI 生成器产出的策略：先在快速引擎验证 → 用户手动"推送到 QuantX registry（status: idea）" → 走 QuantX 晋级流程。

### 8.2 研究数据代理进复盘页

| 数据 | 来源 | 展示位置 |
|------|------|----------|
| scout 盘中扫描报告 | QuantX scout / insight 路由 | Review 复盘页 |
| LLM 决策日志 + 前向收益打分 | decision_journal（含 T+1 买 T+5/T+10 卖口径、一字板 unfillable 标记） | Review 复盘页（"当时的决策 vs 事后收益"） |
| IC 分析 / walk-forward | ic_analysis / walk_forward 路由 | 因子回测页新增标签 |

---

## 9. 实施顺序与工作量估计

| 阶段 | 内容 | 依赖 | 风险 | 工作量估计 |
|------|------|------|------|-----------|
| **P0** ✅ | 数据增强插件（已完成） | — | — | 已完成 |
| **P1** | ext_data 接入 moneyflow / limit_list / top_list / hsgt | P0 的 bridge | 低（纯读） | 小：预设 + 单位换算 + 前端 JOIN 展示 |
| **P2** | Trading 页状态入口（代理持仓/委托/决策建议/风控） | QuantX api 可达 | 低（纯读，故障隔离） | 中：proxy service + api 路由 + Trading 页 UI |
| **P3** | 回测适配器（rigorous 引擎 + 结果归一化） | 策略翻译器 | 中（口径差异、subprocess 管理） | 大：翻译器 + 适配器 + 前端引擎选择 |
| **P4** | 信号出口 webhook + registry 只读视图 + 研究数据代理 | P2 的 proxy | 中（交易链路，需谨慎） | 中 |

原则：**先读后写、先数据后交易**。P1/P2 都是纯读、立刻增值、零交易风险，优先落地。

---

## 10. 风险与对策

| 风险 | 说明 | 对策 |
|------|------|------|
| Python 环境冲突 | QuantX 统一 3.13、vnpy 需 3.11；rqalpha 依赖重 | 进程边界隔离：数据层 import 仅限 quantx_data（轻）；回测 subprocess 用 QuantX 自己的解释器（`QUANTX_PYTHON_BIN`） |
| QuantX 数据新鲜度 | QuantX 靠手动 `ingest_master.py --incremental`，无自动调度 | TickFlow 盘后 pipeline 增加新鲜度检查步骤：DataStore 最新日期落后 N 个交易日则前端提示"QuantX 数据陈旧，补缺已跳过" |
| 单位/口径不一致 | moneyflow 万元、hsgt 亿元；两引擎撮合模型不同 | 单位在接入层统一换算并写测试；回测结果 UI 明示引擎口径差异 |
| QuantX api 故障传导 | 代理层超时/宕机 | 短超时（2~5s）+ 明确 unavailable 状态 + 前端降级卡片；禁止影响非交易页面 |
| 交易安全 | 信号自动执行的误触发 | 信号只进建议队列；自动执行必须过 registry 晋级门禁；下单确认留在 QuantX 侧既有流程 |
| 上游同步 | 本项目是 fork/使用 shy3130 的开源项目，深度改动可能与上游更新冲突 | 集成代码收敛在 `plugins/quantx/`、`services/quantx_*`、独立 api 路由中，减少对主干文件的侵入；通用增强机制可考虑贡献回上游 |
| Symbol 格式 | 两边均为 `.SH/.SZ/.BJ` canonical | 低风险；边界处仍统一走 `quantx_data.symbol` 转换函数 |

---

## 11. 配置项汇总（新增）

| 配置项 | 用途 | 阶段 |
|--------|------|------|
| `QUANTX_PACKAGES_PATH` | quantx_data import 路径（已有） | P0 ✅ |
| `QUANTX_DATA_PATH` | DataStore 数据目录（已有） | P0 ✅ |
| `QUANTX_API_BASE_URL` | QuantX apps/api 地址 | P2 |
| `QUANTX_API_TIMEOUT` | 代理超时（秒，默认 5） | P2 |
| `QUANTX_PROJECT_PATH` | QuantX 项目根（subprocess 工作目录） | P3 |
| `QUANTX_PYTHON_BIN` | QuantX 侧 Python 解释器路径 | P3 |
| `QUANTX_WEBHOOK_ENABLED` / `QUANTX_WEBHOOK_URL` | 信号出口开关与地址 | P4 |

所有配置遵循现有 `app/config.py` Settings 模式，未配置时对应功能自动隐藏/降级，保持"可发现、可配置、可关闭"的插件哲学。
