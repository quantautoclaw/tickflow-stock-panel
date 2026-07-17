"""GET /api/quantx/* 路由测试。

只挂载 quantx_monitor.router 到一个最小 FastAPI app(不触发主 app 的完整
lifespan), 验证响应 payload 的 available 展平契约, 服务层用 monkeypatch
替身, 不发真实网络请求。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import quantx_monitor
from app.services import quantx_proxy


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(quantx_monitor.router)
    return TestClient(app)


def test_positions_unavailable_payload(monkeypatch):
    async def _fake(*args, **kwargs):
        return quantx_proxy.QuantxApiResult(available=False, error="QUANTX_API_BASE_URL 未配置")
    monkeypatch.setattr(quantx_proxy, "get_positions", _fake)

    resp = _client().get("/api/quantx/positions")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"available": False, "error": "QUANTX_API_BASE_URL 未配置"}


def test_positions_available_payload_flattened(monkeypatch):
    async def _fake(*args, **kwargs):
        return quantx_proxy.QuantxApiResult(
            available=True,
            data={"positions": [{"stock_code": "600519.SH"}], "asset": None, "server_fetched_at": "x"},
        )
    monkeypatch.setattr(quantx_proxy, "get_positions", _fake)

    resp = _client().get("/api/quantx/positions")
    body = resp.json()
    assert body["available"] is True
    assert body["positions"][0]["stock_code"] == "600519.SH"


def test_decisions_forwards_query_params(monkeypatch):
    captured = {}

    async def _fake(minutes, action):
        captured["minutes"] = minutes
        captured["action"] = action
        return quantx_proxy.QuantxApiResult(available=True, data={"decisions": [], "count": 0})
    monkeypatch.setattr(quantx_proxy, "get_decisions", _fake)

    resp = _client().get("/api/quantx/decisions", params={"minutes": 120, "action": "EXIT"})
    assert resp.status_code == 200
    assert captured == {"minutes": 120, "action": "EXIT"}


def test_alerts_default_minutes(monkeypatch):
    async def _fake(minutes):
        assert minutes == 60
        return quantx_proxy.QuantxApiResult(available=True, data={"alerts": [], "count": 0})
    monkeypatch.setattr(quantx_proxy, "get_alerts", _fake)

    resp = _client().get("/api/quantx/alerts")
    assert resp.status_code == 200
    assert resp.json()["available"] is True


def test_status_deep_flag_forwarded(monkeypatch):
    captured = {}

    async def _fake(deep, alert_minutes):
        captured["deep"] = deep
        captured["alert_minutes"] = alert_minutes
        return quantx_proxy.QuantxApiResult(available=True, data={"mode": "deep" if deep else "shallow"})
    monkeypatch.setattr(quantx_proxy, "get_status", _fake)

    resp = _client().get("/api/quantx/status", params={"deep": True})
    assert resp.status_code == 200
    assert captured["deep"] is True
    assert resp.json()["mode"] == "deep"
