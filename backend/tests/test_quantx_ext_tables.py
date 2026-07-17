"""QuantX 扩展表 (资金流/涨停明细/龙虎榜) 同步测试。

不依赖真实 QuantX 数据目录: 用 fake store 注入 bridge.get_store(), 只验证
单位换算、按日分区写入、config.json 注册幂等、ext_data 读取契约。
"""
from __future__ import annotations

import polars as pl

from app.plugins.quantx import bridge
from app.services.ext_data import ExtConfigStore
from app.services.quantx_ext_tables import (
    LIMIT_LIST_ID,
    MONEYFLOW_ID,
    TOP_LIST_ID,
    ensure_quantx_ext_presets,
    sync_quantx_ext_table,
)


class _FakeStore:
    def __init__(self, moneyflow=None, limit_list=None, top_list=None):
        self._moneyflow = moneyflow if moneyflow is not None else pl.DataFrame()
        self._limit_list = limit_list if limit_list is not None else pl.DataFrame()
        self._top_list = top_list if top_list is not None else pl.DataFrame()

    def get_moneyflow(self, symbols=None, start_date=None, end_date=None):
        return self._moneyflow

    def get_limit_list(self, symbols=None, start_date=None, end_date=None):
        return self._limit_list

    def get_top_list(self, symbols=None, start_date=None, end_date=None):
        return self._top_list


def _patch_store(monkeypatch, store: _FakeStore):
    monkeypatch.setattr(bridge, "_store", store)
    monkeypatch.setattr(bridge, "_imported", True)
    monkeypatch.setattr(bridge, "get_store", lambda: store)


def test_ensure_quantx_ext_presets_registers_config_without_overwrite(tmp_path):
    ensure_quantx_ext_presets(tmp_path)
    store = ExtConfigStore(tmp_path)
    assert store.get(MONEYFLOW_ID) is not None
    assert store.get(LIMIT_LIST_ID) is not None
    assert store.get(TOP_LIST_ID) is not None

    # 已存在则不覆盖: 手动改一个字段标签, 再次 ensure 不应还原
    cfg = store.get(MONEYFLOW_ID)
    cfg.label = "用户改过的名字"
    store.upsert(cfg)
    ensure_quantx_ext_presets(tmp_path)
    assert store.get(MONEYFLOW_ID).label == "用户改过的名字"


def test_sync_moneyflow_converts_wan_to_yuan_and_partitions_by_date(tmp_path, monkeypatch):
    df = pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "date": ["2026-07-15", "2026-07-15"],
        "buy_sm_amount": [1.0, 2.0],
        "sell_sm_amount": [0.5, 1.0],
        "buy_md_amount": [1.0, 1.0],
        "sell_md_amount": [1.0, 1.0],
        "buy_lg_amount": [1.0, 1.0],
        "sell_lg_amount": [1.0, 1.0],
        "buy_elg_amount": [1.0, 1.0],
        "sell_elg_amount": [1.0, 1.0],
        "net_mf_amount": [3.0, -1.0],
    })
    _patch_store(monkeypatch, _FakeStore(moneyflow=df))

    n, last_date = sync_quantx_ext_table(MONEYFLOW_ID, tmp_path, "2026-07-15", "2026-07-15")

    assert n == 2
    assert last_date == "2026-07-15"

    out_path = tmp_path / "ext_data" / MONEYFLOW_ID / "timeseries" / "date=2026-07-15" / "part.parquet"
    assert out_path.exists()
    written = pl.read_parquet(out_path)
    row = written.filter(pl.col("symbol") == "600519.SH").row(0, named=True)
    # 万元 → 元: 1.0 万元 = 10000 元
    assert row["buy_sm_amount"] == 10_000.0
    assert row["net_mf_amount"] == 30_000.0


def test_sync_limit_list_converts_fd_amount_and_keeps_other_units(tmp_path, monkeypatch):
    df = pl.DataFrame({
        "symbol": ["600519.SH"],
        "date": ["2026-07-15"],
        "name": ["贵州茅台"],
        "close": [1800.0],
        "pct_chg": [10.0],
        "fd_amount": [500.0],
        "first_time": ["09:30:00"],
        "last_time": ["14:57:00"],
        "open_times": [0],
        "up_stat": ["1/1"],
        "limit_times": [1],
    })
    _patch_store(monkeypatch, _FakeStore(limit_list=df))

    n, last_date = sync_quantx_ext_table(LIMIT_LIST_ID, tmp_path, "2026-07-15", "2026-07-15")

    assert n == 1
    out_path = tmp_path / "ext_data" / LIMIT_LIST_ID / "timeseries" / "date=2026-07-15" / "part.parquet"
    written = pl.read_parquet(out_path)
    row = written.row(0, named=True)
    # 500 万元 → 5,000,000 元
    assert row["fd_amount"] == 5_000_000.0
    assert row["limit_times"] == 1


def test_sync_top_list_does_not_convert_units(tmp_path, monkeypatch):
    df = pl.DataFrame({
        "symbol": ["600519.SH"],
        "date": ["2026-07-15"],
        "name": ["贵州茅台"],
        "close": [1800.0],
        "pct_change": [5.0],
        "turnover_rate": [1.2],
        "amount": [1_000_000.0],
        "l_buy": [600_000.0],
        "l_sell": [100_000.0],
        "net_amount": [500_000.0],
        "reason": ["日涨幅偏离值达7%"],
    })
    _patch_store(monkeypatch, _FakeStore(top_list=df))

    n, _ = sync_quantx_ext_table(TOP_LIST_ID, tmp_path, "2026-07-15", "2026-07-15")

    assert n == 1
    out_path = tmp_path / "ext_data" / TOP_LIST_ID / "timeseries" / "date=2026-07-15" / "part.parquet"
    written = pl.read_parquet(out_path)
    row = written.row(0, named=True)
    assert row["net_amount"] == 500_000.0
    assert row["reason"] == "日涨幅偏离值达7%"


def test_sync_splits_multi_day_range_into_separate_partitions(tmp_path, monkeypatch):
    df = pl.DataFrame({
        "symbol": ["600519.SH", "600519.SH"],
        "date": ["2026-07-14", "2026-07-15"],
        "name": ["贵州茅台", "贵州茅台"],
        "close": [1790.0, 1800.0],
        "pct_change": [1.0, 1.0],
        "turnover_rate": [1.0, 1.0],
        "amount": [1.0, 1.0],
        "l_buy": [1.0, 1.0],
        "l_sell": [1.0, 1.0],
        "net_amount": [1.0, 1.0],
        "reason": ["x", "y"],
    })
    _patch_store(monkeypatch, _FakeStore(top_list=df))

    n, last_date = sync_quantx_ext_table(TOP_LIST_ID, tmp_path, "2026-07-14", "2026-07-15")

    assert n == 2
    assert last_date == "2026-07-15"
    base = tmp_path / "ext_data" / TOP_LIST_ID / "timeseries"
    assert (base / "date=2026-07-14" / "part.parquet").exists()
    assert (base / "date=2026-07-15" / "part.parquet").exists()


def test_sync_empty_result_returns_zero(tmp_path, monkeypatch):
    _patch_store(monkeypatch, _FakeStore())
    n, last_date = sync_quantx_ext_table(MONEYFLOW_ID, tmp_path, "2026-07-15", "2026-07-15")
    assert n == 0
    assert last_date == ""
