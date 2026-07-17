"""POST /api/backtest/strategy/rigorous 测试。

挂载真实 backtest.router 到最小 FastAPI app, 用假 StrategyEngine 提供
app.state.strategy_engine, monkeypatch 掉 quantx_backtest_adapter.
run_rigorous_backtest 避免真实网络请求, 只验证路由层的编排逻辑
(404/参数透传/结果透传)。
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as backtest_api
from app.services import quantx_backtest_adapter


@dataclass
class _FakeStrategyDef:
    meta: dict
    basic_filter: dict
    stop_loss: float | None
    max_hold_days: int | None


class _FakeStrategyEngine:
    def __init__(self, strategies: dict[str, _FakeStrategyDef]):
        self._strategies = strategies

    def has(self, strategy_id: str) -> bool:
        return strategy_id in self._strategies

    def get(self, strategy_id: str) -> _FakeStrategyDef:
        return self._strategies[strategy_id]


def _client(strategy_engine: _FakeStrategyEngine) -> TestClient:
    app = FastAPI()
    app.include_router(backtest_api.router)
    app.state.strategy_engine = strategy_engine
    app.state.repo = SimpleNamespace()
    return TestClient(app)


def test_returns_404_for_unknown_strategy():
    client = _client(_FakeStrategyEngine({}))
    resp = client.post("/api/backtest/strategy/rigorous", json={"strategy_id": "nope"})
    assert resp.status_code == 404


def test_passes_strategy_def_fields_to_adapter(monkeypatch):
    strategy = _FakeStrategyDef(
        meta={"scoring": {"momentum_60d": 1.0}, "limit": 30},
        basic_filter={"exclude_st": True},
        stop_loss=-0.08,
        max_hold_days=20,
    )
    captured = {}

    async def _fake_run(**kwargs):
        captured.update(kwargs)
        return quantx_backtest_adapter.RigorousBacktestResult(
            ok=True, translation_warnings=["w1"], result={"engine": "rqalpha", "equity_curve": []},
        )

    monkeypatch.setattr(quantx_backtest_adapter, "run_rigorous_backtest", _fake_run)

    client = _client(_FakeStrategyEngine({"trend_breakout": strategy}))
    resp = client.post("/api/backtest/strategy/rigorous", json={
        "strategy_id": "trend_breakout",
        "start": "2020-01-01",
        "end": "2024-01-01",
        "initial_capital": 200000.0,
        "benchmark": "000905.SH",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["translation_warnings"] == ["w1"]
    assert body["engine"] == "rqalpha"

    assert captured["strategy_id"] == "trend_breakout"
    assert captured["scoring"] == {"momentum_60d": 1.0}
    assert captured["limit"] == 30
    assert captured["basic_filter"] == {"exclude_st": True}
    assert captured["stop_loss"] == -0.08
    assert captured["max_hold_days"] == 20
    assert captured["start_date"] == "2020-01-01"
    assert captured["end_date"] == "2024-01-01"
    assert captured["initial_capital"] == 200000.0
    assert captured["benchmark"] == "000905.SH"


def test_translation_or_network_failure_returns_ok_false(monkeypatch):
    strategy = _FakeStrategyDef(
        meta={"scoring": {"change_pct": 1.0}, "limit": 30},
        basic_filter={}, stop_loss=None, max_hold_days=None,
    )

    async def _fake_run(**kwargs):
        return quantx_backtest_adapter.RigorousBacktestResult(
            ok=False, translation_warnings=[], error="scoring 字段 ['change_pct'] 不在白名单内",
        )

    monkeypatch.setattr(quantx_backtest_adapter, "run_rigorous_backtest", _fake_run)

    client = _client(_FakeStrategyEngine({"bad_strategy": strategy}))
    resp = client.post("/api/backtest/strategy/rigorous", json={"strategy_id": "bad_strategy"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "change_pct" in body["error"]
