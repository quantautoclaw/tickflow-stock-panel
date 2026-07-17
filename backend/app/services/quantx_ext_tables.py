"""QuantX 个性化扩展表 — 资金流 / 涨停明细 / 龙虎榜。

QuantX DataStore 有 TickFlow 数据源没有的三张表 (均为个股维度, 天然适合走
既有 ext_data 机制 JOIN 进分析页/Screener 扩展列):

  - std_moneyflow  → ext_moneyflow_qx  (个股资金流向)
  - std_limit_list → ext_limit_list_qx (涨停板明细)
  - std_top_list   → ext_top_list_qx   (龙虎榜)

北向资金 (std_hsgt) 是大盘聚合指标, 无 symbol 维度, 不适用 ext_data 的
按 symbol JOIN 模型, 不在此模块处理 (走 market_overview_builder 单独接入)。

设计与 ext_presets.py 的 ths 概念/行业预设保持一致:
  - 启动时只注册 config.json (声明字段结构), 不拉取数据。
  - 数据获取由用户在扩展数据页手动触发, 或盘后管道按 (symbol, date) 增量同步。
  - 已存在的 config 绝不覆盖 (老用户 / 用户自定义过的同 id 配置保持不动)。

单位换算 (与 QuantX packages/quantx_data/store.py 文档一致):
  - moneyflow 的 buy/sell_*_amount、net_mf_amount 原始单位为「万元」, 统一转换为「元」。
  - limit_list 的 fd_amount 原始单位为「万元」, 统一转换为「元」。
  - top_list 的 l_buy/l_sell/net_amount/amount 原始单位已是「元」, 不转换。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.plugins.quantx import bridge
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet

logger = logging.getLogger(__name__)

MONEYFLOW_ID = "ext_moneyflow_qx"
LIMIT_LIST_ID = "ext_limit_list_qx"
TOP_LIST_ID = "ext_top_list_qx"

_WAN_TO_YUAN = 10_000.0


# ---------------------------------------------------------------------------
# 预设定义
# ---------------------------------------------------------------------------

def _moneyflow_preset() -> ExtConfig:
    return ExtConfig(
        id=MONEYFLOW_ID,
        label="QuantX 资金流",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("date", "string", "日期"),
            ExtField("buy_sm_amount", "float", "小单买入额(元)"),
            ExtField("sell_sm_amount", "float", "小单卖出额(元)"),
            ExtField("buy_md_amount", "float", "中单买入额(元)"),
            ExtField("sell_md_amount", "float", "中单卖出额(元)"),
            ExtField("buy_lg_amount", "float", "大单买入额(元)"),
            ExtField("sell_lg_amount", "float", "大单卖出额(元)"),
            ExtField("buy_elg_amount", "float", "特大单买入额(元)"),
            ExtField("sell_elg_amount", "float", "特大单卖出额(元)"),
            ExtField("net_mf_amount", "float", "净流入额(元)"),
        ],
        description="个股资金流向 (来源: QuantX DataStore std_moneyflow), 按 (symbol, date) 补缺",
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=None,
    )


def _limit_list_preset() -> ExtConfig:
    return ExtConfig(
        id=LIMIT_LIST_ID,
        label="QuantX 涨停明细",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("date", "string", "日期"),
            ExtField("name", "string", "股票简称"),
            ExtField("close", "float", "收盘价"),
            ExtField("pct_chg", "float", "涨跌幅(%)"),
            ExtField("fd_amount", "float", "封单金额(元)"),
            ExtField("first_time", "string", "首次涨停时间"),
            ExtField("last_time", "string", "最后涨停时间"),
            ExtField("open_times", "int", "炸板次数"),
            ExtField("up_stat", "string", "涨停统计(如3/5)"),
            ExtField("limit_times", "int", "连板数"),
        ],
        description="涨停板明细 (来源: QuantX DataStore std_limit_list), 按 (symbol, date) 补缺",
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=None,
    )


def _top_list_preset() -> ExtConfig:
    return ExtConfig(
        id=TOP_LIST_ID,
        label="QuantX 龙虎榜",
        mode="timeseries",
        fields=[
            ExtField("symbol", "string", "标的代码"),
            ExtField("date", "string", "日期"),
            ExtField("name", "string", "股票简称"),
            ExtField("close", "float", "收盘价"),
            ExtField("pct_change", "float", "涨跌幅(%)"),
            ExtField("turnover_rate", "float", "换手率(%)"),
            ExtField("amount", "float", "成交额(元)"),
            ExtField("l_buy", "float", "龙虎榜买入额(元)"),
            ExtField("l_sell", "float", "龙虎榜卖出额(元)"),
            ExtField("net_amount", "float", "龙虎榜净买入额(元)"),
            ExtField("reason", "string", "上榜原因"),
        ],
        description="龙虎榜 (来源: QuantX DataStore std_top_list), 按 (symbol, date) 补缺",
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        pull=None,
    )


def _presets() -> list[ExtConfig]:
    return [_moneyflow_preset(), _limit_list_preset(), _top_list_preset()]


_PRESET_BUILDERS = {
    MONEYFLOW_ID: _moneyflow_preset,
    LIMIT_LIST_ID: _limit_list_preset,
    TOP_LIST_ID: _top_list_preset,
}


def get_quantx_ext_preset(config_id: str) -> ExtConfig | None:
    builder = _PRESET_BUILDERS.get(config_id)
    return builder() if builder else None


def is_quantx_ext_preset(config_id: str) -> bool:
    return config_id in _PRESET_BUILDERS


# ---------------------------------------------------------------------------
# 启动注册 (只写 config.json, 不拉数据)
# ---------------------------------------------------------------------------

def ensure_quantx_ext_presets(data_dir: Path) -> None:
    """启动时为缺失的 QuantX 扩展表预设创建 config.json。绝不覆盖已有配置。"""
    store = ExtConfigStore(data_dir)
    for config in _presets():
        if store.get(config.id) is not None:
            continue
        try:
            store.upsert(config)
            logger.info("QuantX 扩展表 %s 配置已就绪 (待同步数据)", config.id)
        except Exception as e:
            logger.warning("QuantX 扩展表 %s 配置写入失败 (不影响启动): %s", config.id, e)


# ---------------------------------------------------------------------------
# 单位换算
# ---------------------------------------------------------------------------

_MONEYFLOW_WAN_COLS = (
    "buy_sm_amount", "sell_sm_amount",
    "buy_md_amount", "sell_md_amount",
    "buy_lg_amount", "sell_lg_amount",
    "buy_elg_amount", "sell_elg_amount",
    "net_mf_amount",
)


def _convert_moneyflow_units(df: pl.DataFrame) -> pl.DataFrame:
    exprs = [
        (pl.col(c).cast(pl.Float64, strict=False) * _WAN_TO_YUAN).alias(c)
        for c in _MONEYFLOW_WAN_COLS
        if c in df.columns
    ]
    return df.with_columns(exprs) if exprs else df


def _convert_limit_list_units(df: pl.DataFrame) -> pl.DataFrame:
    if "fd_amount" not in df.columns:
        return df
    return df.with_columns(
        (pl.col("fd_amount").cast(pl.Float64, strict=False) * _WAN_TO_YUAN).alias("fd_amount")
    )


# ---------------------------------------------------------------------------
# 同步 (从 QuantX DataStore 读取 → 按日分区写入 ext_data)
# ---------------------------------------------------------------------------

def sync_quantx_ext_table(
    config_id: str,
    data_dir: Path,
    start_date: str,
    end_date: str,
) -> tuple[int, str]:
    """从 QuantX DataStore 拉取指定日期区间数据, 按 (symbol, date) 写入 ext_data。

    Returns:
        (写入总行数, 最新写入日期字符串); 区间内无数据时返回 (0, "")。

    Raises:
        ValueError: config_id 不是本模块的预设。
        Exception: QuantX 不可用 / DataStore 读取失败, 由调用方兜底处理。
    """
    config = get_quantx_ext_preset(config_id)
    if config is None:
        raise ValueError(f"未知的 QuantX 扩展表预设: {config_id}")

    store = bridge.get_store()
    if config_id == MONEYFLOW_ID:
        raw = store.get_moneyflow(start_date=start_date, end_date=end_date)
        raw = _convert_moneyflow_units(raw)
    elif config_id == LIMIT_LIST_ID:
        raw = store.get_limit_list(start_date=start_date, end_date=end_date)
        raw = _convert_limit_list_units(raw)
    else:  # TOP_LIST_ID
        raw = store.get_top_list(start_date=start_date, end_date=end_date)

    if raw.is_empty() or "date" not in raw.columns or "symbol" not in raw.columns:
        return 0, ""

    field_names = [f.name for f in config.fields]
    keep_cols = [c for c in field_names if c in raw.columns]
    raw = raw.select(keep_cols)

    cfg_store = ExtConfigStore(data_dir)
    if cfg_store.get(config_id) is None:
        cfg_store.upsert(config)

    total = 0
    last_date = ""
    for day_value in sorted(raw["date"].unique().to_list()):
        day_str = str(day_value)
        day_df = raw.filter(pl.col("date").cast(pl.Utf8) == day_str)
        if day_df.is_empty():
            continue
        n = write_ext_parquet(day_df, config, data_dir, snapshot_date=date.fromisoformat(day_str))
        total += n
        last_date = day_str

    logger.info("QuantX 扩展表 %s 同步完成: %d 行, 最新日期 %s", config_id, total, last_date)
    return total, last_date
