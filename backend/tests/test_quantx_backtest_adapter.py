"""quantx_backtest_adapter 测试。

不依赖真实 QuantX 服务: 用 httpx.MockTransport 模拟 POST /api/strategies/
execute-config 响应, 验证翻译失败/未配置/超时/HTTP错误/成功归一化五条路径。
"""
from __future__ import annotations

import httpx

from app.config import settings
from app.services import quantx_backtest_adapter as adapter

_RealAsyncClient = httpx.AsyncClient


def _patch_client(monkeypatch, handler):
    def _make(*, timeout):
        return _RealAsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    monkeypatch.setattr(adapter.httpx, "AsyncClient", _make)


_KWARGS = dict(
    strategy_id="trend_breakout",
    scoring={"momentum_60d": 1.0},
    limit=10,
    basic_filter={"exclude_st": True},
    stop_loss=-0.08,
    max_hold_days=20,
    start_date="2024-01-01",
    end_date="2024-12-31",
)


async def test_translation_failure_short_circuits_before_network_call(monkeypatch):
    monkeypatch.setattr(settings, "quantx_api_base_url", "http://127.0.0.1:9999")
    kwargs = dict(_KWARGS)
    kwargs["scoring"] = {"change_pct": 1.0}  # 不在白名单

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    _patch_client(monkeypatch, handler)
    result = await adapter.run_rigorous_backtest(**kwargs)
    assert result.ok is False
    assert "change_pct" in result.error
    assert called is False


async def test_unavailable_when_base_url_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "quantx_api_base_url", "")
    result = await adapter.run_rigorous_backtest(**_KWARGS)
    assert result.ok is False
    assert "未配置" in result.error
    # 翻译本身仍然成功, warnings 应该照常返回供前端展示
    assert any("filter()" in w for w in result.translation_warnings)


async def test_success_normalizes_engine_result(monkeypatch):
    monkeypatch.setattr(settings, "quantx_api_base_url", "http://127.0.0.1:9999")
    monkeypatch.setattr(settings, "quantx_backtest_timeout", 300.0)

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = request.content
        return httpx.Response(200, json={
            "engine": "rqalpha",
            "strategy_name": "tickflow_trend_breakout",
            "dialect": "quantx_config",
            "benchmark": "000300.SH",
            "daily_nav": [
                {"date": "2024-01-02", "nav": 1.0, "total_value": 1000000.0, "cash": 0.0, "market_value": 1000000.0, "benchmark_nav": 1.0},
                {"date": "2024-01-03", "nav": 1.05, "total_value": 1050000.0, "cash": 0.0, "market_value": 1050000.0, "benchmark_nav": 1.01},
            ],
            "trades": [
                {"date": "2024-01-02", "symbol": "600519.SH", "name": "贵州茅台", "action": "buy", "quantity": 100, "price": 1700.0, "notional": 170000.0, "fee": 51.0},
            ],
            "summary": {"total_return": 0.05, "max_drawdown": -0.02, "sharpe": 1.8},
            "artifacts": {},
            "logs": ["回测完成"],
        })

    _patch_client(monkeypatch, handler)
    result = await adapter.run_rigorous_backtest(**_KWARGS)

    assert result.ok is True
    assert captured["path"] == "/api/strategies/execute-config"
    assert result.result["engine"] == "rqalpha"
    assert result.result["equity_curve"] == [
        {"date": "2024-01-02", "value": 1.0},
        {"date": "2024-01-03", "value": 1.05},
    ]
    assert result.result["summary"]["sharpe"] == 1.8
    assert result.result["trade_count"] == 1


async def test_http_error_returns_unavailable_with_detail(monkeypatch):
    monkeypatch.setattr(settings, "quantx_api_base_url", "http://127.0.0.1:9999")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": {"errors": ["universe.pool_id 'a_share' not found"], "warnings": []}})

    _patch_client(monkeypatch, handler)
    result = await adapter.run_rigorous_backtest(**_KWARGS)
    assert result.ok is False
    assert "422" in result.error


async def test_timeout_returns_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "quantx_api_base_url", "http://127.0.0.1:9999")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    _patch_client(monkeypatch, handler)
    result = await adapter.run_rigorous_backtest(**_KWARGS)
    assert result.ok is False
    assert "超时" in result.error
