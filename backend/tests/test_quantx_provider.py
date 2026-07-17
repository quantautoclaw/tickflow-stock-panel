"""QuantXProvider 归一化与契约测试。

不依赖真实 QuantX 数据目录: 用 fake store / fake http chain, 只验证 Python 侧的
归一化、不复权约定、asset_type 分流、单位审计、实时映射、无效过滤与 close 释放。
"""
from __future__ import annotations

import datetime as dt

import polars as pl

from app.plugins.quantx import bridge
from app.plugins.quantx.provider import QuantXProvider


class _FakeStore:
    """模拟 quantx_data DataStore 的 get_bars/get_index_bars/get_instruments。"""

    def __init__(self, bars=None, etf_bars=None, index_bars=None, instruments=None):
        self._bars = bars if bars is not None else pl.DataFrame()
        self._etf_bars = etf_bars if etf_bars is not None else pl.DataFrame()
        self._index_bars = index_bars if index_bars is not None else pl.DataFrame()
        self._instruments = instruments if instruments is not None else pl.DataFrame()
        self.bars_calls: list[dict] = []

    def get_bars(self, symbols=None, start_date=None, end_date=None, adjust="pre"):
        self.bars_calls.append({"symbols": symbols, "adjust": adjust})
        # bars 自带 asset_type 列时返回合并表
        if not self._bars.is_empty():
            return self._bars
        return pl.DataFrame()

    def get_index_bars(self, symbols=None, start_date=None, end_date=None):
        return self._index_bars

    def get_instruments(self, symbols=None):
        return self._instruments


def _bars_df(rows, with_asset_type=True):
    cols = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
    if with_asset_type:
        cols.append("asset_type")
    return pl.DataFrame(rows, schema={c: pl.Utf8 if c in ("date", "symbol", "asset_type") else pl.Float64 for c in cols})


def _patch_store(monkeypatch, store: _FakeStore):
    monkeypatch.setattr(bridge, "_store", store)
    monkeypatch.setattr(bridge, "_imported", True)
    monkeypatch.setattr(bridge, "get_store", lambda: store)


# ================================================================
# get_daily 契约
# ================================================================

def test_get_daily_returns_unadjusted_and_filters_stock(monkeypatch):
    """stock 只返回 CS; 且调用 get_bars(adjust='')绝不复权。"""
    store = _FakeStore(bars=_bars_df([
        {"date": "2025-07-10", "symbol": "600519.SH", "open": 1500, "high": 1520,
         "low": 1495, "close": 1510, "volume": 300.0, "amount": 45300000.0, "asset_type": "CS"},
        {"date": "2025-07-10", "symbol": "510300.SH", "open": 4.0, "high": 4.05,
         "low": 3.98, "close": 4.02, "volume": 500000.0, "amount": 20100000.0, "asset_type": "ETF"},
    ]))
    _patch_store(monkeypatch, store)

    df = QuantXProvider().get_daily(["600519.SH"], dt.datetime(2025, 7, 1), dt.datetime(2025, 7, 15), asset_type="stock")
    assert df.columns == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert df.height == 1
    assert df["symbol"].to_list() == ["600519.SH"]
    assert df.schema["date"] == pl.Date
    assert df.schema["close"] == pl.Float64
    # adjust 必须为 "" (不复权)
    assert store.bars_calls[-1]["adjust"] == ""


def test_get_daily_etf_only(monkeypatch):
    store = _FakeStore(bars=_bars_df([
        {"date": "2025-07-10", "symbol": "600519.SH", "open": 1500, "high": 1520,
         "low": 1495, "close": 1510, "volume": 300.0, "amount": 45300000.0, "asset_type": "CS"},
        {"date": "2025-07-10", "symbol": "510300.SH", "open": 4.0, "high": 4.05,
         "low": 3.98, "close": 4.02, "volume": 500000.0, "amount": 20100000.0, "asset_type": "ETF"},
    ]))
    _patch_store(monkeypatch, store)
    df = QuantXProvider().get_daily(None, None, None, asset_type="etf")
    assert df["symbol"].to_list() == ["510300.SH"]


def test_get_daily_index_uses_get_index_bars(monkeypatch):
    idx = _bars_df([
        {"date": "2025-07-10", "symbol": "000001.SH", "open": 3400, "high": 3420,
         "low": 3390, "close": 3410, "volume": 0.0, "amount": 0.0},
    ], with_asset_type=False)
    store = _FakeStore(index_bars=idx)
    _patch_store(monkeypatch, store)
    df = QuantXProvider().get_daily(["000001.SH"], None, None, asset_type="index")
    assert df.height == 1
    assert df["symbol"].to_list() == ["000001.SH"]
    assert store.bars_calls == []  # 指数不应调 get_bars


def test_get_daily_filters_halt_days(monkeypatch):
    store = _FakeStore(bars=_bars_df([
        {"date": "2025-07-10", "symbol": "600519.SH", "open": 1500, "high": 1520,
         "low": 1495, "close": 1510, "volume": 300.0, "amount": 45300000.0, "asset_type": "CS"},
        {"date": "2025-07-11", "symbol": "600519.SH", "open": 0, "high": 0,
         "low": 0, "close": 1510, "volume": 0.0, "amount": 0.0, "asset_type": "CS"},  # 停牌
    ]))
    _patch_store(monkeypatch, store)
    df = QuantXProvider().get_daily(["600519.SH"], None, None, asset_type="stock")
    assert df.height == 1
    assert df["date"].dt.strftime("%Y-%m-%d").to_list() == ["2025-07-10"]


def test_get_daily_unit_audit_rejects_bad_units(monkeypatch):
    # volume 单位错成「股」(未除以100): implied_ratio = amount/(close*volume) ≈ 1
    store = _FakeStore(bars=_bars_df([
        {"date": "2025-07-10", "symbol": "600519.SH", "open": 1500, "high": 1520,
         "low": 1495, "close": 1510, "volume": 30000.0, "amount": 45300000.0, "asset_type": "CS"},
    ]))
    _patch_store(monkeypatch, store)
    import pytest
    with pytest.raises(ValueError, match="单位审计异常"):
        QuantXProvider().get_daily(["600519.SH"], None, None, asset_type="stock")


def test_get_daily_normalizes_symbol_and_filters_requested_range(monkeypatch):
    store = _FakeStore(bars=_bars_df([
        {"date": "2025-06-30", "symbol": "600519.SSE", "open": 1500, "high": 1520,
         "low": 1495, "close": 1510, "volume": 300.0, "amount": 45300000.0, "asset_type": "CS"},
        {"date": "2025-07-10", "symbol": "600519.SSE", "open": 1500, "high": 1520,
         "low": 1495, "close": 1510, "volume": 300.0, "amount": 45300000.0, "asset_type": "CS"},
    ]))
    _patch_store(monkeypatch, store)
    df = QuantXProvider().get_daily(
        ["600519.SH"],
        dt.datetime(2025, 7, 1),
        dt.datetime(2025, 7, 15),
        asset_type="stock",
    )
    assert df.height == 1
    assert df["symbol"].to_list() == ["600519.SH"]
    assert df["date"].to_list() == [dt.date(2025, 7, 10)]


def test_get_daily_dedup_keep_last(monkeypatch):
    store = _FakeStore(bars=_bars_df([
        {"date": "2025-07-10", "symbol": "600519.SH", "open": 1500, "high": 1520,
         "low": 1495, "close": 1510, "volume": 300.0, "amount": 45300000.0, "asset_type": "CS"},
        {"date": "2025-07-10", "symbol": "600519.SH", "open": 1500, "high": 1520,
         "low": 1495, "close": 1515, "volume": 300.0, "amount": 45450000.0, "asset_type": "CS"},
    ]))
    _patch_store(monkeypatch, store)
    df = QuantXProvider().get_daily(["600519.SH"], None, None, asset_type="stock")
    assert df.height == 1
    assert df["close"][0] == 1515  # keep last


# ================================================================
# get_realtime 契约
# ================================================================

class _FakeChain:
    def __init__(self, quotes, source="sina"):
        self._quotes = quotes
        self._source = source
        self.stopped = False

    def get_realtime_quotes(self, symbols):
        return self._quotes, self._source

    def stop(self):
        self.stopped = True


def test_get_realtime_maps_last_to_last_price(monkeypatch):
    inst = pl.DataFrame({
        "symbol": ["600519.SH"], "name": ["贵州茅台"], "asset_type": ["CS"],
    })
    store = _FakeStore(instruments=inst)
    _patch_store(monkeypatch, store)
    chain = _FakeChain({
        "600519.SH": {"last": "1510.5", "prev_close": "1500.0", "open": "1505.0",
                      "high": "1520.0", "low": "1498.0", "volume": "300.0",
                      "amount": "45300000.0", "name": "贵州茅台", "source": "sina"},
    })
    monkeypatch.setattr(bridge, "get_http_quote_chain", lambda: chain)
    records = QuantXProvider().get_realtime()
    assert len(records) == 1
    r = records[0]
    assert r["symbol"] == "600519.SH"
    assert r["last_price"] == 1510.5
    assert r["_source"] == "quantx:sina"
    # 衍生字段回算
    assert abs(r["change_amount"] - 10.5) < 1e-6
    assert abs(r["change_pct"] - 10.5 / 1500.0) < 1e-9
    assert r["amplitude"] > 0


def test_get_realtime_filters_invalid(monkeypatch):
    inst = pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"], "name": ["A", "B"], "asset_type": ["CS", "CS"],
    })
    store = _FakeStore(instruments=inst)
    _patch_store(monkeypatch, store)
    chain = _FakeChain({
        "600519.SH": {"last": "0", "prev_close": "0", "open": "0", "high": "0", "low": "0", "volume": "0", "amount": "0"},  # last<=0 过滤
        "000001.SZ": {"last": "15.0", "prev_close": "14.5", "open": "14.6", "high": "15.2", "low": "14.4", "volume": "100.0", "amount": "1500.0"},
        "999999.XX": {"last": "10.0", "prev_close": "9.0", "open": "9.5", "high": "10.5", "low": "9.5", "volume": "5.0", "amount": "50.0"},  # 非标的过滤
    })
    monkeypatch.setattr(bridge, "get_http_quote_chain", lambda: chain)
    records = QuantXProvider().get_realtime()
    assert [r["symbol"] for r in records] == ["000001.SZ"]


# ================================================================
# get_instruments 契约
# ================================================================

def test_get_instruments_stock_shape(monkeypatch):
    inst = pl.DataFrame({
        "symbol": ["600519.SH", "510300.SH"],
        "name": ["贵州茅台", "沪深300ETF"],
        "asset_type": ["CS", "ETF"],
    })
    store = _FakeStore(instruments=inst)
    _patch_store(monkeypatch, store)
    rows = QuantXProvider().get_instruments("stock")
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "600519.SH"
    assert r["code"] == "600519"
    assert r["exchange"] == "SH"
    assert r["region"] == "CN"
    assert r["type"] == "stock"
    # QuantX 不存在的字段为 None
    assert r["ext"]["total_shares"] is None
    assert r["ext"]["limit_up"] is None


# ================================================================
# close 释放 HttpQuoteChain
# ================================================================

def test_close_releases_http_chain(monkeypatch):
    fake_chain = _FakeChain({})
    monkeypatch.setattr(bridge, "_http_chain", fake_chain)
    monkeypatch.setattr(bridge, "get_http_quote_chain", lambda: fake_chain)
    QuantXProvider().close()
    assert fake_chain.stopped is True
