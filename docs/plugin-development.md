# 数据源插件开发指南

数据源插件是可选的行情数据来源(stock-sdk、akshare 等),作为独立模块放在
`backend/app/plugins/` 下。用户**手动安装依赖**后才可用(开发模式);不安装完全不影响主功能。

> 💡 **Docker 部署已预装**:内置插件(如 stock-sdk)的 Node 运行时与 `node_modules` 已在镜像构建期装好,Docker 下无需手动 `npm install`,开箱即用。下方"手动安装依赖"仅适用于开发模式。

## 快速上手

一个插件 = 一个目录 + 一个 `plugin.yaml` 清单:

```
backend/app/plugins/<your_plugin>/
├── plugin.yaml          # 清单(必需)
├── provider.py          # Provider 实现(必需)
├── ...                  # 桥接/依赖文件(按需)
```

### plugin.yaml 字段

```yaml
name: my_source                          # 唯一标识, 只允许 [a-z0-9_], 也是 provider name
display_name: "我的数据源"                 # 设置页显示名
runtime: python                          # 运行时类型: node | python | none
entry: app.plugins.my_source.provider:MyProvider   # provider 类的导入路径
check: app.plugins.my_source.bridge:availability   # 可用性检测函数(可选)
datasets: [daily, adj_factor, minute, realtime]     # 支持的数据集
role: provider                           # provider(可作主源) | enhancer(仅增强) | both
asset_types: [stock, etf, index]         # 支持的资产类型 (增强源用)
description: "数据源描述"
install_hint: "pip install xxx"          # 未装依赖时显示的安装提示
```

### role 与 asset_types

`role` 决定插件在数据路由中的定位:

| role | 含义 |
|---|---|
| `provider` | 可作为主数据源 (设置页「使用」) |
| `enhancer` | 仅作增强源, 按 (symbol, 日期) 补缺, 不覆盖主数据 |
| `both` | 两者都支持 |

`asset_types` 声明插件覆盖的资产 (`stock` / `etf` / `index`), 增强源用它在设置页展示能力, 并约束历史补全的范围。

设置页对 `role=enhancer/both` 的插件显示独立的「数据增强」区: 可启用/停用、试拉、查看配置说明。
`runtime=none` 且不可用时, 不显示「安装」按钮, 改为显示 `install_hint`(配置说明)。

### runtime 字段说明

| runtime | 含义 | 典型场景 |
|---|---|---|
| `python` | 纯 Python 依赖, `pip install` | akshare、tushare |
| `node` | 需要 Node.js 运行时, `npm install` | stock-sdk(已内置,见下) |

> stock-sdk 在 Docker 镜像里已预装 Node 运行时与依赖;开发模式下才需手动 `npm install`。
| `none` | 项目不自动安装依赖 | 外部 monorepo、纯 HTTP API 源 |

`runtime` 字段决定安装按钮行为；实际可用性仍由 `check` 函数负责。

### check 函数

插件自己负责检测依赖是否已安装。后端启动时会调用此函数:

```python
# app/plugins/my_source/bridge.py
def availability() -> tuple[bool, str]:
    """返回 (是否可用, 原因)。不抛异常。"""
    try:
        import akshare  # noqa: F401
        return True, "ok"
    except ImportError:
        return False, "未安装 akshare, 运行: pip install akshare"
```

- **可用** → 插件注册进路由表, 设置页可切换
- **不可用** → 设置页显示插件卡片但灰显, 展示 `install_hint`

## Provider 接口契约

Provider 是一个普通 Python 类(无需继承基类), 实现以下方法签名。方法签名对齐
`GenericHTTPProvider`, 这样 services 层(kline_sync / quote_service 等)的路由逻辑
零改动即可路由到插件。

```python
class MyProvider:
    name = "my_source"
    builtin = True  # 标记为内置(不可被用户编辑/删除)

    def __init__(self):
        self.config = MyConfig()  # 需有 .datasets 属性(dict, key 是数据集名)

    def close(self) -> None:
        """清理资源(load_all 重建注册表时会调)。"""

    def get_daily(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None) -> pl.DataFrame:
        """日K: 返回 schema [symbol, date, open, high, low, close, volume, amount]"""

    def get_adj_factors(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None) -> pl.DataFrame:
        """除权因子: 返回 schema [symbol, trade_date, ex_factor]"""

    def get_minute(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None, freq="1m") -> pl.DataFrame:
        """分钟K: 返回 schema [symbol, datetime, open, high, low, close, volume, amount]"""

    def get_realtime(self) -> list[dict]:
        """全市场实时快照: 返回 list[dict], 每行含 symbol/last_price/prev_close/open/high/low/volume"""

    def get_instruments(self, asset_type="stock") -> list[dict]:
        """标的维表(可选): 返回 tickflow Instrument 形状的行, 供 instrument_sync 复用 flatten"""
```

### config.datasets 的作用

`provider_has_dataset(name, dataset)` 通过 `dataset in provider.config.datasets` 判断。
这是 services 层路由的关键: 用户在设置页选了插件, 但某数据集未声明时, 该数据集
自动回退 TickFlow。

```python
class MyConfig:
    datasets = {"daily": ..., "realtime": ...}  # key 是数据集名, value 任意
```

## 现有插件参考

- **`backend/app/plugins/stocksdk/`** — Node 型插件, 通过 subprocess 桥接调用 stock-sdk
  - `bridge.py` — Python↔Node 桥接 + availability 检测
  - `bridge.mjs` — Node 端(并发池、重试、SDK 解析)
  - `provider.py` — Provider 实现(归一化、分批、错误降级)
- **`backend/app/plugins/quantx/`** — 增强源插件 (role=enhancer), 读取 QuantX DataStore
  - `bridge.py` — quantx_data import + DataStore/HttpQuoteChain 单例 + availability
  - `provider.py` — 不复权日K + 多源实时 + 维表
  - 配置: 环境变量 `QUANTX_PACKAGES_PATH` + `QUANTDATA_ROOT`
  - QuantX 标准日K必须满足 `volume=手、amount=元`。旧快照单位混杂时先审计：
    `PYTHONPATH=$QUANTX_PACKAGES_PATH python backend/scripts/repair_quantx_bar_units.py --data-root $QUANTDATA_ROOT`
    确认报告后加 `--apply`；脚本会逐文件备份并原子替换。

## 数据增强与实时链路 (role=enhancer/both)

增强源与主数据源解耦, 不会覆盖主数据:

- **历史增强**: 启用后, 盘后管道(Step 1.8)与手动「数据页 → 增强补全」调用
  `app/services/data_enhancement.py` 的 `run_enhancement()`, 按 `(symbol, date)` 补缺
  (`merge_daily_asset(conflict="keep_existing")`), 再由现有 `indicators.pipeline` 统一复权。
- **实时链路**: 设置「实时链路」(如 `QuantX → TickFlow`)后, 盘中行情走
  `app/services/provider_chain.py` 的 `fetch_realtime_chain()`: 前序 provider 优先, 覆盖率
  不足回退后续 provider。QuoteService 不再特判任何插件。
- 两者偏好分别在 `data_enhancers` 与 `realtime_provider_chain`(preferences.json),
  通过「设置 → 数据源」页面配置。

## 路由机制(无需关心, 仅参考)

后端启动时, `loader.py` 的 `_load_builtin_plugins()` 扫描 `plugins/` 目录:
1. 读每个子目录的 `plugin.yaml`
2. 调 `check` 函数检测可用性
3. 可用 → 动态 import `entry` 指向的 Provider 类 → 注册进 `_PROVIDERS`
4. 不可用 → 记录状态, 设置页显示但不可切换

注册后, 插件和用户 YAML 自定义源走**完全相同的路由路径**(services 层的
`provider_has_dataset` / `get_provider` 调用), 无需额外集成代码。
