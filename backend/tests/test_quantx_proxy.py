"""QuantX apps/api 只读代理测试。

不依赖真实 QuantX 服务: 用 httpx.MockTransport 模拟响应, 只验证代理层的
契约 — 未配置/超时/连接失败/HTTP 错误一律降级为 available=False 且不抛异常,
成功时正确解析真实 QuantX 端点返回的字段结构 (与 apps/api/routes/trading.py、
monitoring.py 的 _serialize_position / position_decisions 实际实现核对一致)。
"""
from __future__ import annotations

import httpx
import pytest

from app.config import settings
from app.services import quantx_proxy


_RealAsyncClient = httpx.AsyncClient


def _patch_client(monkeypatch, handler):
    def _make(*, timeout):
        return _RealAsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    monkeypatch.setattr(quantx_proxy.httpx, "AsyncClient", _make)


@pytest.fixture(autouse=True)
def _quantx_api_configured(monkeypatch):
    monkeypatch.setattr(settings, "quantx_api_base_url", "http://127.0.0.1:9999")
    monkeypatch.setattr(settings, "quantx_api_timeout", 5.0)


async def test_get_positions_unavailable_when_base_url_empty(monkeypatch):
    monkeypatch.setattr(settings, "quantx_api_base_url", "")
    result = await quantx_proxy.get_positions()
    assert result.available is False
    assert "未配置" in result.error


async def test_get_positions_merges_positions_and_asset(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/trading/positions":
            return httpx.Response(200, json={
                "positions": [{
                    "stock_code": "600519.SH", "name": "贵州茅台", "volume": 100,
                    "can_use_volume": 100, "avg_price": 1700.0,
                    "market_value": 180000.0, "unrealized_pnl": 10000.0,
                }],
                "server_fetched_at": "2026-07-17T10:00:00",
            })
        if request.url.path == "/api/trading/asset":
            return httpx.Response(200, json={
                "asset": {"cash": 50000.0, "frozen_cash": 0.0, "market_value": 180000.0, "total_asset": 230000.0},
                "server_fetched_at": "2026-07-17T10:00:00",
            })
        return httpx.Response(404)

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_positions()
    assert result.available is True
    assert result.data["positions"][0]["stock_code"] == "600519.SH"
    assert result.data["asset"]["total_asset"] == 230000.0


async def test_get_positions_still_returns_when_asset_fails(monkeypatch):
    """positions 成功但 asset 失败: 仍返回 available=True, asset=None (逐字段降级)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/trading/positions":
            return httpx.Response(200, json={"positions": [], "server_fetched_at": "x"})
        return httpx.Response(503, json={"detail": "connection cooldown"})

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_positions()
    assert result.available is True
    assert result.data["asset"] is None


async def test_get_positions_unavailable_when_positions_endpoint_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "Trading runner is temporarily unavailable (connection cooldown active)."})

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_positions()
    assert result.available is False
    assert "cooldown" in result.error


async def test_get_decisions_passes_query_params(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "success": True,
            "decisions": [{
                "symbol": "600519.SH", "action": "EXIT", "score": 90, "price": 1800.0,
                "pnl_pct": -0.08, "strategy": "position_monitor", "reasons": "跌破止损位",
                "llm_note": "建议止损", "severity": "error", "created_at": "2026-07-17T10:00:00",
            }],
            "count": 1, "minutes": 120,
        })

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_decisions(minutes=120, action="EXIT")
    assert result.available is True
    assert captured["path"] == "/api/monitoring/position-decisions"
    assert captured["params"] == {"minutes": "120", "action": "EXIT"}
    assert result.data["decisions"][0]["action"] == "EXIT"


async def test_get_alerts_success(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "alerts": [], "count": 0, "minutes": 60})

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_alerts(minutes=60)
    assert result.available is True
    assert result.data["count"] == 0


async def test_get_status_timeout_returns_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_status()
    assert result.available is False
    assert "超时" in result.error


async def test_get_status_connect_error_returns_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_status()
    assert result.available is False
    assert "无法连接" in result.error


async def test_get_market_review_latest_only_hits_latest_endpoint(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"date": "2026-07-16", "report": "今日大盘...", "generated_at": "2026-07-16T15:05:00"})

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_market_review_latest()
    assert result.available is True
    # 必须只打 /latest, 不能打会触发 LLM 生成的 /api/insight/market-review
    assert captured["path"] == "/api/insight/market-review/latest"
    assert result.data["report"] == "今日大盘..."


async def test_get_runs_passes_strategy_and_limit(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"runs": [], "total": 0})

    _patch_client(monkeypatch, handler)
    result = await quantx_proxy.get_runs(strategy="trend_breakout", limit=20)
    assert result.available is True
    assert captured["path"] == "/api/runs"
    assert captured["params"] == {"limit": "20", "strategy": "trend_breakout"}


async def test_get_runs_omits_strategy_param_when_none(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"runs": [], "total": 0})

    _patch_client(monkeypatch, handler)
    await quantx_proxy.get_runs()
    assert "strategy" not in captured["params"]
