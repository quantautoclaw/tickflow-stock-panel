"""market_overview_builder._north_flow (QuantX 北向资金) 测试。

北向资金 (std_hsgt) 是大盘聚合指标, 无 symbol 维度, 不走 ext_data JOIN,
直接作为 build_market_overview 结果的一个字段。QuantX 不可用/数据缺失时
必须优雅降级为 available=False, 不能让整个大盘总览报错。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.plugins.quantx import bridge
from app.services.market_overview_builder import _north_flow


class _FakeStore:
    def __init__(self, hsgt=None):
        self._hsgt = hsgt if hsgt is not None else pl.DataFrame()

    def get_hsgt(self, start_date=None, end_date=None):
        return self._hsgt


def test_north_flow_unavailable_when_quantx_not_configured(monkeypatch):
    monkeypatch.setattr(bridge, "availability", lambda: (False, "未配置 QUANTDATA_ROOT"))
    result = _north_flow(date(2026, 7, 15))
    assert result == {"available": False}


def test_north_flow_unavailable_when_hsgt_empty(monkeypatch):
    monkeypatch.setattr(bridge, "availability", lambda: (True, "ok"))
    monkeypatch.setattr(bridge, "get_store", lambda: _FakeStore(hsgt=pl.DataFrame()))
    result = _north_flow(date(2026, 7, 15))
    assert result == {"available": False}


def test_north_flow_returns_latest_north_money_in_yi(monkeypatch):
    df = pl.DataFrame({
        "date": ["2026-07-14", "2026-07-15"],
        "north_money": [12.3, 45.6],
    })
    monkeypatch.setattr(bridge, "availability", lambda: (True, "ok"))
    monkeypatch.setattr(bridge, "get_store", lambda: _FakeStore(hsgt=df))
    result = _north_flow(date(2026, 7, 15))
    assert result["available"] is True
    assert result["date"] == "2026-07-15"
    assert result["north_money_yi"] == 45.6


def test_north_flow_survives_quantx_exception(monkeypatch):
    def _boom():
        raise RuntimeError("quantx init failed")
    monkeypatch.setattr(bridge, "availability", _boom)
    result = _north_flow(date(2026, 7, 15))
    assert result == {"available": False}
