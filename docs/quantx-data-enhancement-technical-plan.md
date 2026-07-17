# QuantX 数据增强插件技术实施方案

> 状态：已实施（代码、自动化测试、真实数据修复与试拉均已完成）  
> 适用项目：TickFlow Stock Panel  
> 目标版本：建议拆分为 3 个 PR 交付  
> 最后更新：2026-07-16

---

## 1. 背景与问题

项目已经存在两类扩展能力：

1. `ext_data`：用于概念、行业、资金流、外部标签等附加数据，通过 JOIN 进入分析页面。
2. `data_providers/plugins`：用于日 K、除权因子、分钟 K、实时行情、财务等行情数据源。

QuantX 提供日 K、ETF、指数、标的维表和实时行情，属于第二类，应作为数据源插件接入。

当前实验实现通过以下方式直接侵入主流程：

- `Settings.data_backend = quantx`
- FastAPI lifespan 中执行 `quantx_bridge.bootstrap()`
- `QuoteService` 中直接判断 `data_backend`
- 直接读写 TickFlow Panel 的 Parquet
- 将前复权数据写入要求“不复权数据”的 `kline_daily`

该实现存在以下阻断问题：

- 可能发生二次复权，且真实 `raw_close/raw_high/raw_low` 会被现有 pipeline 覆盖。
- 启动时同步拉数并全量计算 enriched，导致前端长时间无法访问。
- 按日期分区判断补缺，不能补齐同一天缺失的股票。
- 用 `symbol/name` 覆盖 instruments，导致股本、涨跌停价等字段丢失。
- QuantX 不出现在设置页，前端展示的数据源与实际数据源不一致。
- `incremental_sync()` 没有接入盘后调度。
- 默认路径绑定开发者本机，无法用于 Docker、Windows 和桌面打包环境。

因此，本方案将 QuantX 定位为：

> **可发现、可配置、可关闭、可测试的行情增强插件。TickFlow 保持主数据源，QuantX 按 `(symbol, date)` 补缺；实时行情按 provider chain 获取。**

---

## 2. 目标与非目标

### 2.1 目标

1. QuantX 通过现有 `backend/app/plugins/` 机制注册。
2. TickFlow 继续作为默认主数据源。
3. QuantX 历史行情默认只补缺，不覆盖 TickFlow 已有数据。
4. 所有日 K 统一以“不复权 OHLCV”进入现有 pipeline，只复权一次。
5. QuantX 实时行情可作为优先源，失败或覆盖不足时回退 TickFlow。
6. QuantX 维表采用字段级 coalesce，不破坏 TickFlow 字段。
7. 启动过程不拉 QuantX 数据、不全量计算数据。
8. 支持盘后自动增量补缺和前端手动历史补全。
9. 前端可看到插件状态、增强状态、实时来源和覆盖率。
10. 设计为通用增强机制，后续 AkShare、Tushare 等插件可以复用。

### 2.2 非目标

本次不做：

- 修改 QuantX 项目源码。
- 将 QuantX 数据复制到独立第二套查询引擎。
- 将 QuantX 接入 `ext_data`。
- 在启动阶段自动回填全部历史。
- 同时维护两套 enriched 指标体系。
- 首期支持 QuantX 财务和分钟 K。
- 自动判断并替换 TickFlow 的“疑似错误值”；首期只补缺。

---

## 3. 总体架构

```text
                        ┌──────────────────────┐
                        │  设置页 / API 配置    │
                        │                      │
                        │ primary: tickflow    │
                        │ enhancers: [quantx]  │
                        │ realtime chain:      │
                        │ [quantx, tickflow]   │
                        └──────────┬───────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              │                                         │
              ▼                                         ▼
┌─────────────────────────────┐          ┌─────────────────────────────┐
│ 盘后/手动历史同步            │          │ 实时行情轮询                │
│                             │          │                             │
│ 1. TickFlow 主同步          │          │ 1. QuantX 获取行情          │
│ 2. QuantX 按 key 补缺       │          │ 2. 按 symbol 统计覆盖率      │
│ 3. 统一写 raw parquet       │          │ 3. 不足时回退 TickFlow      │
└──────────────┬──────────────┘          └──────────────┬──────────────┘
               │                                         │
               └──────────────────┬──────────────────────┘
                                  ▼
                   ┌─────────────────────────────┐
                   │ KlineRepository             │
                   │                             │
                   │ replace / keep_existing     │
                   │ key = (symbol, date)        │
                   └──────────────┬──────────────┘
                                  ▼
                   ┌─────────────────────────────┐
                   │ 现有 indicators.pipeline    │
                   │ raw → 前复权 → enriched     │
                   └──────────────┬──────────────┘
                                  ▼
                   ┌─────────────────────────────┐
                   │ API / 策略 / 监控 / 前端     │
                   └─────────────────────────────┘
```

---

## 4. 核心设计决策

### 4.1 主数据源与增强源分离

现有 `daily_data_provider` 保留，表示主数据源。新增：

```json
{
  "data_enhancers": ["quantx"],
  "realtime_provider_chain": ["quantx", "tickflow"]
}
```

含义：

- `daily_data_provider=tickflow`：盘后主同步仍走 TickFlow。
- `data_enhancers=[quantx]`：主同步完成后使用 QuantX 补缺。
- `realtime_provider_chain=[quantx,tickflow]`：实时先尝试 QuantX，再用 TickFlow 补充或回退。

不使用隐藏的 `DATA_BACKEND` 总开关。

### 4.2 单一复权责任

数据契约明确为：

```text
kline_daily / kline_etf_daily：不复权 OHLCV
kline_*_enriched：由项目现有 pipeline 统一生成前复权 OHLC
raw_close/raw_high/raw_low：由 pipeline 从不复权 OHLC 生成
```

QuantX Provider 必须调用：

```python
store.get_bars(..., adjust="")
```

禁止将 `adjust="pre"` 的结果写入 `kline_daily`。

### 4.3 补缺粒度

历史补缺主键固定为：

```text
(symbol, date)
```

不能用“日期目录是否存在”作为补缺依据。

### 4.4 冲突策略

Repository 支持两种策略：

| 策略 | 使用场景 | 行为 |
|---|---|---|
| `replace` | 主数据源、实时行情 | 新记录覆盖相同 key 的旧记录 |
| `keep_existing` | QuantX 历史增强 | 已有 key 保留，只插入缺失 key |

首期 QuantX 历史增强只使用 `keep_existing`。

### 4.5 启动阶段零取数

FastAPI lifespan 只允许：

- 加载插件清单。
- 执行轻量 availability 检查。
- 加载本地缓存。
- 启动已有后台服务。

禁止在 lifespan 中：

- 拉取 QuantX 历史数据。
- 拉取 QuantX 实时数据。
- 执行 `run_pipeline()`。
- 覆盖 instruments。

---

## 5. 文件变更清单

### 5.1 删除当前实验代码

修改：

```text
backend/app/config.py
backend/app/main.py
backend/app/services/quote_service.py
```

删除：

```text
backend/app/tickflow/quantx_bridge.py
```

应删除内容：

- `data_backend`
- `quantx_packages_path` 的硬编码默认值
- `quantx_data_root` 的硬编码默认值
- `quantx_ingest_lookback_days`
- lifespan 中的 `quantx_bridge.bootstrap(repo)`
- `QuoteService` 中的 QuantX 特判

### 5.2 新增文件

```text
backend/app/plugins/quantx/__init__.py
backend/app/plugins/quantx/plugin.yaml
backend/app/plugins/quantx/bridge.py
backend/app/plugins/quantx/provider.py
backend/app/services/data_enhancement.py
backend/app/services/provider_chain.py
backend/tests/test_quantx_provider.py
backend/tests/test_data_enhancement.py
backend/tests/test_realtime_provider_chain.py
```

### 5.3 修改现有文件

```text
backend/app/data_providers/custom/loader.py
backend/app/services/preferences.py
backend/app/api/settings.py
backend/app/api/pipeline.py
backend/app/jobs/daily_pipeline.py
backend/app/services/quote_service.py
backend/app/services/instrument_sync.py
backend/app/services/index_sync.py
backend/app/tickflow/repository.py
backend/app/indicators/pipeline.py（仅在需要暴露精确重算入口时修改）
frontend/src/lib/api.ts
frontend/src/pages/settings/DataSources.tsx
frontend/src/pages/Data.tsx
frontend/src/components/Layout.tsx（如展示实时来源）
.env.example
docs/plugin-development.md
```

---

## 6. QuantX 插件设计

### 6.1 `plugin.yaml`

```yaml
name: quantx
display_name: "QuantX 数据增强"
runtime: none
entry: app.plugins.quantx.provider:QuantXProvider
check: app.plugins.quantx.bridge:availability
datasets: [daily, realtime, instruments]
role: enhancer
asset_types: [stock, etf, index]
description: "使用 QuantX 补充缺失日K、标的维表和实时行情"
install_hint: "请配置 QUANTX_PACKAGES_PATH 和 QUANTDATA_ROOT"
```

说明：

- `runtime=none` 表示项目不自动安装 QuantX。
- `check` 负责判断 QuantX 是否可导入、数据目录是否可读。
- loader 需要将 `role`、`asset_types` 原样返回给前端。
- 前端对 `runtime=none && available=false` 不显示“安装”按钮，改为显示“配置说明”。

### 6.2 环境配置

`.env.example` 增加：

```env
# QuantX 插件，可选。留空时插件显示为未配置，不影响主功能。
QUANTX_PACKAGES_PATH=
QUANTDATA_ROOT=
```

约定：

- 如果 Python 环境已经可以直接 `import quantx_data`，`QUANTX_PACKAGES_PATH` 可留空。
- 如果不能直接 import，`QUANTX_PACKAGES_PATH` 指向 QuantX monorepo 的 `packages/`。
- `QUANTDATA_ROOT` 指向 QuantX 数据根目录。
- 不提供任何开发者本机默认绝对路径。

### 6.3 `bridge.py`

职责：

- 处理 QuantX import。
- 创建 QuantX DataStore 单例。
- 创建 HttpQuoteChain 单例。
- 提供 availability 检查和资源释放。

建议接口：

```python
def availability() -> tuple[bool, str]: ...
def get_store(): ...
def get_http_quote_chain(): ...
def close() -> None: ...
```

availability 检查顺序：

1. 尝试直接 `import quantx_data`。
2. 失败时读取 `QUANTX_PACKAGES_PATH` 并临时加入 `sys.path`。
3. 验证 `QUANTDATA_ROOT` 已配置且存在。
4. 调用 `get_store(root)`，但不读取全市场数据。
5. 返回 `(True, "ok")` 或清晰错误原因。

示例错误：

```text
未配置 QUANTX_PACKAGES_PATH，且当前 Python 环境无法 import quantx_data
QUANTDATA_ROOT 不存在: /path/to/quantdata
QuantX 初始化失败: ...
```

`close()` 必须调用 HttpQuoteChain 的 `stop()`，避免测试或 reload 时遗留线程池。

### 6.4 Provider 配置 shim

插件 loader 当前通过 `provider.config.datasets` 判断能力，因此 QuantX Provider 需要提供兼容配置：

```python
@dataclass
class _QuantXConfig:
    name: str = "quantx"
    display_name: str = "QuantX 数据增强"
    datasets: dict = field(default_factory=lambda: {
        "daily": None,
        "realtime": None,
        "instruments": None,
    })
    path: None = None
    builtin: bool = True
    role: str = "enhancer"
```

### 6.5 `get_daily()` 契约

```python
def get_daily(
    self,
    symbols: list[str],
    start_time: datetime | None,
    end_time: datetime | None,
    asset_type: str = "stock",
    on_chunk_done=None,
) -> pl.DataFrame:
    ...
```

资产分流：

```python
if asset_type == "index":
    raw = store.get_index_bars(...)
elif asset_type in {"stock", "etf"}:
    raw = store.get_bars(
        symbols=symbols or None,
        start_date=start_date,
        end_date=end_date,
        adjust="",
    )
```

对于 `stock/etf`，根据 `asset_type` 列过滤：

```text
stock -> CS
etf   -> ETF
index -> INDX 或 get_index_bars
```

标准输出：

```text
symbol: String
 date: Date
 open: Float64
 high: Float64
 low: Float64
 close: Float64
 volume: Float64
 amount: Float64
```

数据单位：

```text
volume：手（1 手 = 100 股）
amount：元
change_pct：如存在，使用小数制；但 daily 标准输出无需保留
```

QuantX DataStore 当前标准层已经约定 `volume=手、amount=元`，Provider 仍应增加样本审计：

```text
amount / (close * volume) 的中位数应大致接近 100
```

如果单位审计明显异常，应抛出可见错误，不得静默写盘。

必须执行：

- symbol 后缀标准化为 `.SH/.SZ/.BJ`。
- date 转 `pl.Date`。
- OHLCV 转 `Float64`。
- 去重 `(symbol,date)`，`keep="last"`。
- 过滤 `open=0 && high=0` 的停牌行。
- 校验必填列。
- 不写盘、不计算 enriched。

### 6.6 `get_realtime()` 契约

```python
def get_realtime(self) -> list[dict]:
    ...
```

Provider 从 QuantX instruments 中提取 `CS/ETF/INDX` symbol，再调用 HttpQuoteChain。

标准记录：

```python
{
    "symbol": "600000.SH",
    "name": "浦发银行",
    "last_price": 10.52,
    "prev_close": 10.41,
    "open": 10.45,
    "high": 10.60,
    "low": 10.39,
    "volume": 123456.0,
    "amount": 130000000.0,
    "change_pct": 0.0106,
    "change_amount": 0.11,
    "amplitude": 0.0202,
    "turnover_rate": null,
    "_source": "quantx:sina"
}
```

缺失衍生字段时由 Provider 或现有 QuoteService 统一回算：

```python
change_amount = last_price - prev_close
change_pct = change_amount / prev_close
amplitude = (high - low) / prev_close
```

无效记录过滤规则：

- symbol 为空。
- `last_price <= 0`。
- symbol 不属于 `CS/ETF/INDX`。
- 价格无法转为有限数值。

### 6.7 `get_instruments()` 契约

```python
def get_instruments(self, asset_type: str = "stock") -> list[dict]:
    ...
```

返回兼容 `instrument_sync._flatten_instruments()` 的字典：

```python
{
    "symbol": "600000.SH",
    "name": "浦发银行",
    "code": "600000",
    "exchange": "SH",
    "region": "CN",
    "type": "stock",
    "ext": {
        "listing_date": "1999-11-10",
        "total_shares": None,
        "float_shares": None,
        "limit_up": None,
        "limit_down": None,
    },
}
```

QuantX 不存在的字段返回 `None`，由维表合并逻辑保留 TickFlow 值。

---

## 7. 插件 loader 扩展

修改 `backend/app/data_providers/custom/loader.py`。

### 7.1 插件状态新增字段

`_PLUGIN_STATUS[name]` 增加：

```python
{
    "role": manifest.get("role", "provider"),
    "asset_types": manifest.get("asset_types", ["stock"]),
}
```

支持 role：

```text
provider：可作为主数据源
 enhancer：只能作为增强源
 both：两者都支持
```

### 7.2 类型修正

当前 `_PROVIDERS` 标注为 `dict[str, GenericHTTPProvider]`，但实际也注册插件 Provider。建议改为：

```python
_PROVIDERS: dict[str, Any] = {}
```

或定义统一 Protocol，避免错误类型暗示。

### 7.3 close 约定

`load_all()` 重建 provider 前继续调用：

```python
provider.close()
```

QuantX Provider 的 `close()` 必须释放 HttpQuoteChain。

---

## 8. 偏好与 API 设计

### 8.1 Preferences

修改 `backend/app/services/preferences.py`，新增：

```python
def get_data_enhancers() -> list[str]: ...
def set_data_enhancers(names: list[str]) -> list[str]: ...
def get_realtime_provider_chain() -> list[str]: ...
def set_realtime_provider_chain(names: list[str]) -> list[str]: ...
```

默认值：

```python
data_enhancers = []
realtime_provider_chain = []
```

兼容逻辑：

- 若 `realtime_provider_chain` 为空，继续使用旧 `realtime_data_provider`。
- 若链非空，使用链路由。
- 非法、不可用或未注册的 provider 从链中移除。
- `tickflow` 始终允许出现在 realtime chain。
- enhancer 必须是 `role in {enhancer,both}`。

推荐保存结构：

```json
{
  "daily_data_provider": "tickflow",
  "data_enhancers": ["quantx"],
  "realtime_provider_chain": ["quantx", "tickflow"]
}
```

### 8.2 设置 API

扩展 `DataProvidersIn`：

```python
class DataProvidersIn(BaseModel):
    daily_data_provider: str | None = None
    adj_factor_provider: str | None = None
    minute_data_provider: str | None = None
    realtime_data_provider: str | None = None
    financial_data_provider: str | None = None
    data_enhancers: list[str] | None = None
    realtime_provider_chain: list[str] | None = None
```

扩展：

```http
PUT /api/settings/preferences/data-providers
```

响应增加：

```json
{
  "daily_data_provider": "tickflow",
  "adj_factor_provider": "same_as_daily",
  "minute_data_provider": "tickflow",
  "realtime_data_provider": "tickflow",
  "financial_data_provider": "tickflow",
  "data_enhancers": ["quantx"],
  "realtime_provider_chain": ["quantx", "tickflow"]
}
```

### 8.3 插件卸载/失效处理

QuantX 被禁用或 availability 变为 false 时：

- 从 `data_enhancers` 删除。
- 从 `realtime_provider_chain` 删除。
- 若链变空，回退旧 `realtime_data_provider`。
- 不删除已经写入的历史数据。

---

## 9. Repository 补缺写入

修改 `backend/app/tickflow/repository.py`。

### 9.1 结果模型

建议新增到 `backend/app/data_providers/schemas.py` 或 `repository.py`：

```python
@dataclass(frozen=True)
class MergeResult:
    input_rows: int
    inserted_rows: int
    replaced_rows: int
    unchanged_rows: int
    inserted_symbols: frozenset[str]
    inserted_dates: frozenset[date]
    existing_dates_changed: frozenset[date]
```

### 9.2 公共接口

```python
def merge_daily_asset(
    self,
    asset_type: str,
    df: pl.DataFrame,
    *,
    conflict: Literal["replace", "keep_existing"] = "replace",
) -> MergeResult:
    ...
```

资产目录：

```python
stock -> kline_daily
index -> kline_index_daily
etf   -> kline_etf_daily
```

### 9.3 合并算法

每个 date 分区在 `_write_lock` 内执行：

```python
incoming_keys = incoming.select("symbol", "date").unique()
existing_keys = existing.select("symbol", "date").unique()
new_keys = incoming_keys.join(existing_keys, on=["symbol", "date"], how="anti")
```

`replace`：

```python
merged = pl.concat([existing, incoming], how="diagonal_relaxed").unique(
    subset=["symbol", "date"],
    keep="last",
)
```

`keep_existing`：

```python
missing = incoming.join(
    existing.select("symbol", "date"),
    on=["symbol", "date"],
    how="anti",
)
merged = pl.concat([existing, missing], how="diagonal_relaxed")
```

`keep_existing` 不采用简单反向 concat，以免同 key 的 schema 差异导致意外字段覆盖。

### 9.4 原子写入

继续复用 `_atomic_write_parquet()`。只有 `missing.height > 0` 或 replace 有变化时才写盘，避免无变化时刷新 mtime。

### 9.5 兼容现有接口

以下接口保持现有 replace 行为：

```python
append_daily()
append_index_daily()
append_etf_daily()
flush_live_daily()
merge_live_daily_asset()
```

内部可以委托 `merge_daily_asset(..., conflict="replace")`，但不改变现有语义。

---

## 10. 维表安全合并

新增到 `backend/app/services/instrument_sync.py`：

```python
def merge_instrument_frames(
    primary: pl.DataFrame,
    enhancement: pl.DataFrame,
) -> pl.DataFrame:
    ...
```

### 10.1 合并规则

1. 主键为 `symbol`。
2. TickFlow 非空字段优先。
3. TickFlow 为空时使用 QuantX。
4. QuantX 新 symbol 追加。
5. 保留主表和增强表的所有列。
6. 输出按 symbol 去重并排序。

字段级语义：

```text
result.name         = coalesce(tickflow.name, quantx.name)
result.exchange     = coalesce(tickflow.exchange, quantx.exchange)
result.total_shares = coalesce(tickflow.total_shares, quantx.total_shares)
result.float_shares = coalesce(tickflow.float_shares, quantx.float_shares)
result.limit_up     = coalesce(tickflow.limit_up, quantx.limit_up)
result.limit_down   = coalesce(tickflow.limit_down, quantx.limit_down)
```

### 10.2 写入入口

Repository 增加：

```python
def save_instruments(self, asset_type: str, df: pl.DataFrame) -> None:
    ...
```

禁止插件直接 `write_parquet()`。

写入后统一：

- 原子替换。
- 失效对应 instruments cache。
- 刷新对应 DuckDB view。

---

## 11. 历史增强服务

新增 `backend/app/services/data_enhancement.py`。

### 11.1 请求模型

```python
@dataclass(frozen=True)
class EnhancementRequest:
    provider: str
    start_date: date
    end_date: date
    asset_types: tuple[str, ...]
    conflict: str = "keep_existing"
```

### 11.2 返回模型

```python
@dataclass
class EnhancementSummary:
    provider: str
    started_at: str
    finished_at: str
    assets: dict[str, dict]
    inserted_rows: int
    affected_symbols: list[str]
    affected_dates: list[str]
```

### 11.3 主入口

```python
def run_enhancement(
    repo: KlineRepository,
    request: EnhancementRequest,
    on_progress=None,
) -> EnhancementSummary:
    ...
```

处理流程：

```text
1. 校验 provider 已注册、available、role 合法
2. 获取/合并 instruments
3. 按 asset_type 获取 symbol universe
4. 调 provider.get_daily(adjust="")
5. schema 与单位校验
6. repo.merge_daily_asset(..., keep_existing)
7. 根据 MergeResult 重算 enriched
8. rebuild views
9. clear/refresh cache
10. 返回汇总
```

### 11.4 symbol universe

优先级：

```text
1. repo 中已有对应 instruments
2. QuantX provider.get_instruments(asset_type)
3. 用户显式传入 symbols（未来扩展）
```

不能因为 TickFlow capabilities 不足就把 universe 降级成 demo symbols；增强插件应能使用自己的 instruments。

### 11.5 enriched 重算策略

#### 股票

- 新增了全新日期分区：调用 `run_pipeline(new_dates_only=True)`。
- 给已有日期补了 symbol：调用 `run_pipeline(symbols=affected_symbols)`。
- 两者同时发生：调用 `run_pipeline(new_dates_only=True, symbols=affected_symbols)`。

#### 指数

指数不复权。新增辅助函数：

```python
def recompute_asset_enriched(
    repo,
    asset_type="index",
    symbols=None,
    target_dates=None,
) -> int:
    ...
```

- 新日期：读取至少 120 个日历日历史预热，只写 target dates。
- 已有日期补行：为 affected symbols 读取完整可用历史后重算，并 merge 回分区。

#### ETF

ETF 读取 `adj_factor_etf/all.parquet`，使用现有 `compute_enriched(raw, factors, instruments)`。

正确性优先于首期性能。后续如 affected symbol 太多，再增加批次和增量窗口优化。

---

## 12. 盘后管道集成

修改 `backend/app/jobs/daily_pipeline.py`。

股票流程调整为：

```text
Step 0    同步并增强 instruments
Step 1    主数据源日K同步
Step 1.5  除权因子同步
Step 1.8  执行 data_enhancers 日K补缺
Step 2    统一计算 enriched
Step 2.3  指数/ETF 主同步 + 增强补缺
Step 3    刷新视图和缓存
```

### 12.1 Step 1.8

```python
enhancement_symbols: set[str] = set()
enhancement_new_dates: set[date] = set()
enhancement_existing_dates: set[date] = set()

for provider_name in preferences.get_data_enhancers():
    result = enhance_daily_range(...)
    enhancement_symbols.update(result.inserted_symbols)
    enhancement_new_dates.update(result.inserted_dates - existing_daily_dates)
    enhancement_existing_dates.update(result.existing_dates_changed)
```

### 12.2 与除权重算合并

```python
symbols_to_recompute = set(affected_adj_symbols) | enhancement_symbols
```

pipeline 分支：

```python
if not enriched_exists or backward_extension:
    run_pipeline()
elif forward_incremental:
    run_pipeline(new_dates_only=True, symbols=list(symbols_to_recompute) or None)
elif symbols_to_recompute:
    run_pipeline(symbols=list(symbols_to_recompute))
```

### 12.3 错误策略

- 单个 enhancer 失败不能损坏主数据源结果。
- 手动“增强任务”失败：job 标记 failed。
- 完整盘后管道中的 enhancer 失败：加入 `stage_errors`，最终任务标记部分失败。
- 已经成功写入的主数据保留。
- 日志必须记录 provider、asset、日期范围、输入行数、新增行数。

---

## 13. 手动增强 API

修改 `backend/app/api/pipeline.py`。

### 13.1 Endpoint

```http
POST /api/pipeline/enhance
```

请求：

```json
{
  "provider": "quantx",
  "start_date": "2024-01-01",
  "end_date": "2025-07-15",
  "asset_types": ["stock", "etf", "index"],
  "conflict": "keep_existing"
}
```

约束：

- `provider` 必须是已加载 enhancer。
- `start_date <= end_date`。
- 默认最大跨度建议 5 年；更长需明确确认或分批。
- 首期只允许 `conflict=keep_existing`，避免前端误覆盖主数据。

响应：

```json
{
  "job_id": "...",
  "reused": false
}
```

### 13.2 Job 执行

复用：

```text
_long_task_executor
job_store
try_acquire_run_slot
release_run_slot
```

增强任务与现有盘后任务共享全局写入槽，禁止并发修改 Parquet。

建议进度 stage：

```text
enhance_instruments
enhance_stock_daily
enhance_etf_daily
enhance_index_daily
compute_enriched
refresh_views
done
```

### 13.3 状态查询

继续复用：

```http
GET /api/pipeline/jobs/{job_id}
```

成功结果示例：

```json
{
  "provider": "quantx",
  "inserted_rows": 12450,
  "assets": {
    "stock": {"input": 2000000, "inserted": 12000},
    "etf": {"input": 100000, "inserted": 350},
    "index": {"input": 3000, "inserted": 100}
  }
}
```

---

## 14. 实时 Provider Chain

新增 `backend/app/services/provider_chain.py`。

### 14.1 返回模型

```python
@dataclass
class RealtimeChainResult:
    records: list[dict]
    sources: list[str]
    source_counts: dict[str, int]
    expected_symbols: int
    received_symbols: int
    coverage_ratio: float
    errors: dict[str, str]
    elapsed_ms: float
```

### 14.2 获取算法

```python
def fetch_realtime_chain(
    provider_names: list[str],
    repo: KlineRepository,
    capset,
) -> RealtimeChainResult:
    ...
```

处理：

1. 从 repo 获取期望的股票、ETF、指数 symbol 集合。
2. 按链顺序请求 provider。
3. 每个 provider 的记录先标准化和过滤。
4. 使用 `records_by_symbol.setdefault(symbol, record)`，前面的 provider 优先。
5. 覆盖率达到阈值后停止请求后续 provider。
6. 未达到阈值则继续用后续 provider 补 symbol。
7. 返回合并记录和来源统计。

默认停止阈值：

```python
REALTIME_COVERAGE_STOP_RATIO = 0.95
```

如果用户只开启股票/ETF/指数中的部分类型，expected symbols 只计算启用类型。

### 14.3 TickFlow 适配

`tickflow` 仍由现有 SDK 路径获取，但封装成 provider chain 的一个 fetch 分支。必须遵守 capability：

- none：不可用。
- free：仅自选模式时不执行全市场 TickFlow fallback。
- Starter+：允许全市场 fallback。

QuantX 可用时，none/free 用户可以获得 QuantX 全市场实时行情，不应再被 TickFlow tier 阻止。

### 14.4 QuoteService 改造

`QuoteService.realtime_mode()`：

```python
if has_available_full_market_provider_chain():
    return "full_market"
```

`_fetch_full_market_quotes()`：

```python
result = provider_chain.fetch_realtime_chain(...)
if not result.records:
    logger.warning(...)
    return
self._process_full_market_records(
    result.records,
    t0=t0,
    now_ts=now_ts,
)
self._last_realtime_chain_result = result
```

禁止在 QuoteService 中 import QuantX 模块。

### 14.5 状态接口

`QuoteService.status()` 增加：

```json
{
  "realtime_provider_chain": ["quantx", "tickflow"],
  "realtime_sources": ["quantx:sina"],
  "source_counts": {"quantx:sina": 5370},
  "expected_symbols": 5500,
  "received_symbols": 5370,
  "coverage_ratio": 0.9764,
  "source_errors": {}
}
```

前端据此展示真实数据来源，不再根据旧 preference 猜测。

---

## 15. 前端方案

### 15.1 API 类型

修改 `frontend/src/lib/api.ts`。

插件状态增加：

```ts
export interface PluginDataSourceItem {
  name: string
  display_name: string
  datasets: string[]
  runtime: string
  available: boolean
  status: string
  description: string
  install_hint: string
  role?: 'provider' | 'enhancer' | 'both'
  asset_types?: string[]
}
```

Preferences 增加：

```ts
data_enhancers?: string[]
realtime_provider_chain?: string[]
```

### 15.2 设置页布局

`DataSources.tsx` 拆为两个区域。

#### 主数据源

保留现有 TickFlow、stock-sdk、自定义 HTTP provider 的“使用”逻辑。

#### 数据增强

对 `role=enhancer/both` 的插件显示：

```text
QuantX 数据增强
状态：可用
能力：日K补缺 / 实时 / 维表
资产：股票 / ETF / 指数
[启用增强] [试拉]
```

启用后：

```text
[✓ 已启用] [停用] [历史补全]
```

不可用且 `runtime=none`：

```text
状态：未配置
原因：QUANTDATA_ROOT 未配置
[查看配置说明]
```

不得显示无效的“安装”按钮。

### 15.3 当前数据路由

设置页顶部由单一“当前数据源”改为：

```text
日K主源       TickFlow
日K增强       QuantX
实时链路      QuantX → TickFlow
分钟K         TickFlow
财务          TickFlow
```

### 15.4 数据页历史补全

`frontend/src/pages/Data.tsx` 增加“QuantX 历史补全”对话框：

字段：

- Provider（默认 QuantX）
- 开始日期
- 结束日期
- 股票、ETF、指数复选框
- 固定提示：“仅补缺，不覆盖已有 TickFlow 数据”

提交 `/api/pipeline/enhance`，复用现有 job 轮询和进度展示。

### 15.5 实时来源展示

实时状态区域显示：

```text
实时来源：QuantX · 新浪
覆盖：5370 / 5500（97.6%）
更新时间：10:23:18
```

若发生回退：

```text
实时来源：QuantX · 腾讯 + TickFlow 补充
```

---

## 16. 数据质量与安全校验

写盘前必须执行以下校验。

### 16.1 Schema

日 K 必填：

```text
symbol/date/open/high/low/close/volume/amount
```

### 16.2 数值

```text
open/high/low/close >= 0
high >= max(open, low, close)（允许极小浮点误差）
low <= min(open, high, close)
volume >= 0
amount >= 0
```

停牌行仍使用项目现有规则：

```text
open == 0 && high == 0 -> 过滤
```

### 16.3 唯一性

```text
(symbol, date) 唯一
```

重复时 Provider 内先 `keep="last"`，Repository 再执行冲突策略。

### 16.4 日期范围

Provider 不得返回请求范围外数据。写盘前再次过滤：

```text
start_date <= date <= end_date
```

### 16.5 symbol

接受并统一转换：

```text
600000.SH
000001.SZ
430047.BJ
```

拒绝无法识别的后缀，不能默认猜成股票并写入。

### 16.6 单位审计

抽样计算：

```text
implied_ratio = amount / (close * volume)
```

因为 volume 单位为手、amount 为元，中位数通常应接近 100。建议合理告警范围：

```text
10 <= median(implied_ratio) <= 1000
```

超出范围时任务失败，防止把“股”和“手”混用的数据写入生产目录。

---

## 17. 并发与性能

### 17.1 写并发

- 所有历史增强任务复用 `try_acquire_run_slot()`。
- Repository 内继续使用 `_write_lock`。
- 同一时间只允许一个盘后/增强重任务写 Parquet。
- 实时写与历史写仍由 Repository 分区锁串行化。

### 17.2 内存

QuantX 全市场 400 天可能超过百万行，禁止无界一次性复制多份 DataFrame。

实现要求：

- 按 asset type 分开处理。
- 必要时按年份或日期窗口分批，建议每批 90～180 天。
- 每批写完释放 DataFrame。
- enriched 全量重算复用现有 pipeline，不在 Provider 中重复计算。

### 17.3 无变化优化

如果 `MergeResult.inserted_rows == 0`：

- 不重写 parquet。
- 不刷新 enriched。
- 不重建全量缓存。
- 任务返回“无缺失数据”。

### 17.4 启动性能

启用 QuantX 后的后端启动耗时不应显著高于未启用状态。availability 检查目标：

```text
< 500ms（本地磁盘正常时）
```

不得读取全市场 bars 作为 availability 检查。

---

## 18. 日志与可观测性

统一日志字段：

```text
provider
asset_type
start_date
end_date
input_rows
inserted_rows
replaced_rows
coverage_ratio
elapsed_ms
```

示例：

```text
quantx enhancement: asset=stock range=2025-07-01..2025-07-15 input=52341 inserted=127 existing=52214 elapsed=1.82s
realtime chain: sources=quantx:sina received=5370 expected=5500 coverage=97.64% elapsed=2.13s
```

禁止输出：

- API Key。
- 完整用户路径中的敏感信息（状态接口可按需只显示是否配置）。
- 大量 symbol 全列表；只输出数量和最多 10 个样例。

---

## 19. 测试方案

### 19.1 QuantX Provider 单元测试

文件：

```text
backend/tests/test_quantx_provider.py
```

用 fake store，不依赖真实 QuantX 数据目录。

测试：

1. `get_daily(stock)` 只返回 CS。
2. `get_daily(etf)` 只返回 ETF。
3. index 走 `get_index_bars()`。
4. 调用 `get_bars(adjust="")`，绝不使用 `adjust="pre"`。
5. date、数值类型和列顺序正确。
6. volume/amount 单位审计异常时拒绝。
7. 实时 `last -> last_price` 映射正确。
8. 无效实时记录被过滤。
9. `close()` 释放 HttpQuoteChain。

### 19.2 Repository 合并测试

文件：

```text
backend/tests/test_data_enhancement.py
```

场景：

1. 已有日期仅一只股票，QuantX 返回三只，最终为三只。
2. 相同 `(symbol,date)` 价格冲突时，`keep_existing` 保留 TickFlow。
3. `replace` 使用新记录。
4. 不同日期正常创建分区。
5. 无新增时不改文件 mtime。
6. MergeResult 的行数、symbol、date 正确。
7. 原子写中断不产生可扫描的半文件。

### 19.3 复权测试

构造一只发生除权的股票：

- raw 数据来自 QuantX `adjust=""`。
- factor 来自现有 adj_factor。
- 执行项目 pipeline。

断言：

```text
enriched.raw_close == QuantX raw close
enriched.close == raw close 经项目 pipeline 前复权后的结果
不存在二次复权
```

### 19.4 维表测试

TickFlow：

```text
symbol/name/float_shares/total_shares/limit_up/limit_down
```

QuantX：

```text
symbol/name/exchange
```

断言合并后：

- TickFlow 股本字段保留。
- TickFlow 涨跌停价保留。
- 空 exchange 被 QuantX 补上。
- QuantX 新 symbol 被追加。

### 19.5 Provider chain 测试

文件：

```text
backend/tests/test_realtime_provider_chain.py
```

测试：

1. QuantX 覆盖率 100%，不调用 TickFlow。
2. QuantX 失败，回退 TickFlow。
3. QuantX 返回部分 symbol，TickFlow 只作为补充来源合并。
4. 同一 symbol 前序 provider 优先。
5. 无 provider 可用时返回空，不抛到轮询线程外。
6. none/free tier 不错误调用付费 TickFlow 全市场接口。
7. status 返回真实来源和覆盖率。

### 19.6 启动测试

断言 lifespan：

- 不调用 QuantX `get_bars()`。
- 不调用 QuantX realtime。
- 不调用全量 `run_pipeline()`。
- QuantX 不可用时应用仍能 ready。

### 19.7 前端测试/检查

至少执行：

```bash
cd frontend
pnpm exec tsc --noEmit
pnpm build
```

人工验收：

- 插件可见。
- 不可用原因可见。
- 可启用/停用增强。
- 历史补全进度正常。
- 实时来源显示与后端 status 一致。

---

## 20. 数据迁移与回滚

### 20.1 实验代码尚未发布时

直接撤销实验 bridge，不需要数据配置迁移。

如果实验代码已经向 `data/` 写入过数据，不能假设现有 kline 分区正确。建议提供一次性审计脚本：

```text
backend/scripts/audit_quantx_experimental_ingest.py
```

审计内容：

- 查找实验启用期间新增的分区。
- 比较 raw_close 与 QuantX 不复权 close。
- 检查是否存在二次复权迹象。
- 检查 instruments 是否只剩 symbol/name。
- 只输出报告，不自动删除。

### 20.2 配置兼容

若旧环境存在：

```env
DATA_BACKEND=quantx
```

新版本应忽略该配置，并输出一次 warning：

```text
DATA_BACKEND=quantx 已废弃，请在“设置 → 数据源 → 数据增强”启用 QuantX
```

可保留一个版本的兼容提示，下一版本删除。

### 20.3 回滚

停用 QuantX 只需：

```json
{
  "data_enhancers": [],
  "realtime_provider_chain": ["tickflow"]
}
```

停用不会删除已经补入的数据。若需删除 QuantX 补入行，首期不自动支持，因为当前 Parquet 未持久化 source lineage。

因此首期历史增强必须严格 `keep_existing`，上线前建议备份 `data/`。

### 20.4 后续 lineage

后续可增加独立 ingest journal：

```text
data/provider_ingest_log/*.jsonl
```

记录新增 key 的 provider、job_id 和时间，以支持精确回滚，但不作为本期阻断项。

---

## 21. 分阶段实施

### PR 1：安全纠偏与 QuantX 插件

范围：

- 删除 `data_backend` 和 bridge 特判。
- 删除启动同步。
- 新建 QuantX plugin/bridge/provider。
- QuantX 日 K 强制使用不复权数据。
- loader 返回 role/asset_types。
- 增加 Provider 单元测试。

验收：

- 后端启动不取 QuantX 数据。
- 设置 API 能看到 QuantX 插件状态。
- Provider 试拉返回标准 raw schema。
- 现有全部测试通过。

### PR 2：通用历史增强

范围：

- Preferences 增加 `data_enhancers`。
- Repository 增加 `keep_existing`。
- instruments coalesce。
- 新增 data_enhancement 服务。
- 盘后 pipeline 接入。
- 手动 `/api/pipeline/enhance`。
- 数据页历史补全入口。

验收：

- 能补已有日期缺失的 symbol。
- 不覆盖 TickFlow 相同 key。
- 不破坏 instruments 字段。
- 新增数据正确进入 enriched。

### PR 3：实时链路与可观测性

范围：

- 新增 `realtime_provider_chain`。
- QuoteService 改为 provider chain。
- 覆盖率与 fallback。
- 设置页显示真实路由。
- Layout/Data 页显示实时来源。

验收：

- QuantX 成功时使用 QuantX。
- QuantX 失败时按能力回退 TickFlow。
- 前端来源、数量、覆盖率与后端一致。

---

## 22. 完成定义（Definition of Done）

所有条件满足才可认为方案完成：

- [x] 项目中不存在 `settings.data_backend` 的 QuantX 分支。
- [x] FastAPI lifespan 不执行 QuantX 拉数或全量 pipeline。
- [x] QuantX 位于 `backend/app/plugins/quantx/`。
- [x] 设置页可以看到 QuantX 的可用状态和配置原因。
- [x] QuantX daily 只返回不复权数据。
- [x] 历史补缺按 `(symbol,date)` 执行。
- [x] TickFlow 相同 key 不被 QuantX 覆盖。
- [x] instruments 的股本、涨跌停字段不丢失。
- [x] 盘后任务会执行已启用 enhancer。
- [x] 手动历史补全为异步 job，不阻塞 HTTP。
- [x] 实时行情使用 provider chain，不在 QuoteService 中特判 QuantX。
- [x] 前端显示真实实时来源与覆盖率。
- [x] QuantX 不可用时不影响 TickFlow 主流程。
- [x] 后端现有测试全部通过。
- [x] 新增复权、补缺、维表和 realtime chain 测试通过。
- [x] 前端 TypeScript 检查和 build 通过。
- [x] 文档和 `.env.example` 已更新。

---

## 23. 最终数据行为

实施完成后，系统行为应明确为：

```text
1. TickFlow 是默认主数据源。
2. QuantX 是用户显式启用的增强插件。
3. TickFlow 已有历史行情保持不动。
4. QuantX 只补缺失的 (symbol,date)。
5. 所有日K以不复权价格进入项目。
6. 项目现有 pipeline 统一执行一次前复权和指标计算。
7. 实时行情优先 QuantX，并按配置回退其他 provider。
8. 所有最终数据继续复用现有 Parquet、enriched、策略、监控、API 和前端。
```
