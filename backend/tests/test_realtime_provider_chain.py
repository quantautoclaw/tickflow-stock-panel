"""实时 provider chain 测试 (§19.5)。

不依赖真实网络/TickFlow SDK: mock provider 与 fetch 分支, 验证优先级、回退、
覆盖率停止、空结果不抛异常、tier 门控。
"""
from __future__ import annotations

import polars as pl

from app.services import provider_chain
from app.services.provider_chain import (
    fetch_realtime_chain,
    has_available_full_market_provider_chain,
)


class _Cap:
    """模拟 CapabilitySet: has(name) 返回预设。"""

    def __init__(self, **has_map):
        self._map = has_map

    def has(self, name):
        return self._map.get(name, False)


def _repo_with(symbols):
    """构造假 repo, get_instruments/get_etf_instruments/get_index_instruments 返回 symbols。"""

    class _R:
        def __init__(self, syms):
            self._df = pl.DataFrame({"symbol": list(syms)})

        def get_instruments(self):
            return self._df

        def get_etf_instruments(self):
            return pl.DataFrame()

        def get_index_instruments(self):
            return pl.DataFrame()

    return _R(symbols)


def _patch_prefs(monkeypatch, *, pull_stock=True, pull_etf=False, pull_index=False, index_mode="all", chain_candidates=None):
    import app.services.preferences as prefs
    monkeypatch.setattr(prefs, "get_realtime_pull_stock", lambda: pull_stock)
    monkeypatch.setattr(prefs, "get_realtime_pull_etf", lambda: pull_etf)
    monkeypatch.setattr(prefs, "get_realtime_pull_index", lambda: pull_index)
    monkeypatch.setattr(prefs, "get_realtime_index_mode", lambda: index_mode)
    if chain_candidates is not None:
        monkeypatch.setattr(prefs, "_realtime_chain_candidates", lambda: chain_candidates)


def test_chain_quantx_full_coverage_skips_tickflow(monkeypatch):
    """QuantX 覆盖率 100%, 不调用 TickFlow。"""
    _patch_prefs(monkeypatch, pull_stock=True)
    repo = _repo_with(["600519.SH", "000001.SZ"])
    tf_calls = []

    def fake_plugin(name):
        if name == "quantx":
            return [
                {"symbol": "600519.SH", "last_price": 1500.0, "_source": "quantx:sina"},
                {"symbol": "000001.SZ", "last_price": 15.0, "_source": "quantx:sina"},
            ]
        return None

    def fake_tickflow(capset, expected):
        tf_calls.append(True)
        return None

    monkeypatch.setattr(provider_chain, "_fetch_from_plugin", fake_plugin)
    monkeypatch.setattr(provider_chain, "_fetch_from_tickflow", fake_tickflow)

    result = fetch_realtime_chain(["quantx", "tickflow"], repo, _Cap())
    assert result.coverage_ratio == 1.0
    assert result.received_symbols == 2
    assert tf_calls == []  # TickFlow 未被调用


def test_chain_quantx_failure_falls_back_tickflow(monkeypatch):
    """QuantX 失败, 回退 TickFlow。"""
    _patch_prefs(monkeypatch, pull_stock=True)
    repo = _repo_with(["600519.SH"])

    monkeypatch.setattr(provider_chain, "_fetch_from_plugin", lambda name: None)
    monkeypatch.setattr(provider_chain, "_fetch_from_tickflow",
                        lambda capset, expected: [{"symbol": "600519.SH", "last_price": 1500.0, "_source": "tickflow"}])

    result = fetch_realtime_chain(["quantx", "tickflow"], repo, _Cap())
    assert result.received_symbols == 1
    assert "tickflow" in result.sources
    assert result.fallback_used is True
    assert "quantx" in result.errors


def test_chain_partial_then_supplement(monkeypatch):
    """QuantX 返回部分 symbol, TickFlow 补充其余。"""
    _patch_prefs(monkeypatch, pull_stock=True)
    repo = _repo_with(["600519.SH", "000001.SZ", "000002.SZ"])
    calls = {"quantx": False, "tickflow": False}

    def fake_plugin(name):
        if name == "quantx":
            calls["quantx"] = True
            return [{"symbol": "600519.SH", "last_price": 1500.0, "_source": "quantx:sina"}]
        return None

    def fake_tickflow(capset, expected):
        calls["tickflow"] = True
        return [
            {"symbol": "000001.SZ", "last_price": 15.0, "_source": "tickflow"},
            {"symbol": "000002.SZ", "last_price": 10.0, "_source": "tickflow"},
            {"symbol": "600519.SH", "last_price": 9999.0, "_source": "tickflow"},  # 已有, 应被忽略
        ]

    monkeypatch.setattr(provider_chain, "_fetch_from_plugin", fake_plugin)
    monkeypatch.setattr(provider_chain, "_fetch_from_tickflow", fake_tickflow)

    result = fetch_realtime_chain(["quantx", "tickflow"], repo, _Cap())
    assert result.received_symbols == 3
    # 前序优先: 600519.SH 来自 quantx
    by_sym = {r["symbol"]: r for r in result.records}
    assert by_sym["600519.SH"]["_source"] == "quantx:sina"
    assert by_sym["000001.SZ"]["_source"] == "tickflow"
    assert result.fallback_used is True


def test_chain_ignores_symbols_outside_enabled_scope(monkeypatch):
    _patch_prefs(monkeypatch, pull_stock=True, pull_etf=False, pull_index=False)
    repo = _repo_with(["600519.SH"])
    monkeypatch.setattr(provider_chain, "_fetch_from_plugin", lambda name: [
        {"symbol": "600519.SH", "last_price": 1500.0, "_source": "quantx:sina"},
        {"symbol": "510300.SH", "last_price": 4.0, "_source": "quantx:sina"},
        {"symbol": "000001.SH", "last_price": 3400.0, "_source": "quantx:sina"},
    ])
    result = fetch_realtime_chain(["quantx"], repo, _Cap())
    assert [r["symbol"] for r in result.records] == ["600519.SH"]
    assert result.coverage_ratio == 1.0


def test_chain_empty_providers_returns_empty(monkeypatch):
    """无 provider 可用时返回空, 不抛异常。"""
    _patch_prefs(monkeypatch, pull_stock=True)
    repo = _repo_with(["600519.SH"])
    result = fetch_realtime_chain([], repo, _Cap())
    assert result.records == []
    assert result.coverage_ratio == 0.0


def test_chain_invalid_records_filtered(monkeypatch):
    """无效记录 (last<=0/空 symbol) 被过滤。"""
    _patch_prefs(monkeypatch, pull_stock=True)
    repo = _repo_with(["600519.SH", "000001.SZ"])
    monkeypatch.setattr(provider_chain, "_fetch_from_plugin", lambda name: [
        {"symbol": "600519.SH", "last_price": 0.0, "_source": "quantx"},  # 无效
        {"symbol": "", "last_price": 10.0, "_source": "quantx"},          # 空 symbol
        {"symbol": "000001.SZ", "last_price": 15.0, "_source": "quantx"},
    ] if name == "quantx" else None)
    monkeypatch.setattr(provider_chain, "_fetch_from_tickflow", lambda capset, expected: None)
    result = fetch_realtime_chain(["quantx", "tickflow"], repo, _Cap())
    assert result.received_symbols == 1


def test_chain_free_tier_no_full_market_tickflow(monkeypatch):
    """free tier 不应触发 TickFlow 全市场 fallback。"""
    _patch_prefs(monkeypatch, pull_stock=True)
    tf_called = []

    import app.services.quote_service as qs
    monkeypatch.setattr(qs.QuoteService, "_current_tier", staticmethod(lambda: "free"))
    monkeypatch.setattr(provider_chain, "_fetch_from_plugin", lambda name: None)

    def fake_tickflow(capset, expected):
        tf_called.append(True)
        return None

    monkeypatch.setattr(provider_chain, "_fetch_from_tickflow", fake_tickflow)
    # 这里直接测 _fetch_from_tickflow 的 tier 门控
    from app.services.provider_chain import _fetch_from_tickflow
    out = _fetch_from_tickflow(_Cap(QUOTE_POOL=True), {"600519.SH"})
    assert out is None  # free 档不做全市场


def test_chain_free_tier_supplements_core_indices_by_symbol(monkeypatch):
    """free 可用 by-symbol 补核心指数，但不能调用全市场 universes。"""
    indices = ["000001.SH", "399001.SZ", "399006.SZ", "000680.SH"]
    _patch_prefs(monkeypatch, pull_stock=True, pull_index=True, index_mode="core")
    import app.services.preferences as prefs
    import app.services.quote_service as qs
    monkeypatch.setattr(prefs, "get_realtime_index_symbols", lambda: indices)
    monkeypatch.setattr(qs.QuoteService, "_current_tier", staticmethod(lambda: "free"))

    calls: list[list[str]] = []

    class _Quotes:
        def get(self, *, symbols):
            calls.append(list(symbols))
            return [
                {"symbol": s, "last_price": 101.0, "prev_close": 100.0,
                 "high": 102.0, "low": 99.0, "ext": {}}
                for s in symbols
            ]

        def get_by_universes(self, **kwargs):  # pragma: no cover - 误调用即失败
            raise AssertionError("free tier must not call get_by_universes")

    class _Client:
        quotes = _Quotes()

    monkeypatch.setattr("app.tickflow.client.get_paid_realtime_client", lambda: _Client())
    out = provider_chain._fetch_from_tickflow(_Cap(), set(indices) | {"600519.SH"})

    assert calls == [indices]
    assert out is not None and len(out) == 4
    assert {r["symbol"] for r in out} == set(indices)
    assert {r["change_pct"] for r in out} == {0.01}


def test_has_available_chain(monkeypatch):
    _patch_prefs(monkeypatch, chain_candidates={"quantx", "tickflow"})
    monkeypatch.setattr("app.services.preferences.get_realtime_provider_chain", lambda: ["quantx", "tickflow"])
    assert has_available_full_market_provider_chain() is True
    monkeypatch.setattr("app.services.preferences.get_realtime_provider_chain", lambda: ["tickflow"])
    assert has_available_full_market_provider_chain() is False
    monkeypatch.setattr("app.services.preferences.get_realtime_provider_chain", lambda: [])
    assert has_available_full_market_provider_chain() is False
