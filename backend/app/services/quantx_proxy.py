"""QuantX apps/api 只读代理。

边界声明 (与 docs/tickflow-quantx-integration-plan.md 第 7 节一致):
  TickFlow 不执行交易、不直连经纪商, 这里只代理 QuantX 已生产化的只读端点
  (持仓/资产/盘中决策建议/告警/运行健康), 供监控中心展示。绝不代理
  /api/trading/order、/api/trading/cancel、/api/trading/stop-orders(POST)
  等写入/下单端点 —— 本模块函数名与端点均只含 GET。

契约: 所有函数返回 QuantxApiResult, 从不抛异常。QuantX 未配置 / 网络失败 /
连接冷却期(503) 一律降级为 available=False + error 原因, 前端据此显示
"引擎离线" 卡片, 不影响监控中心其余功能。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class QuantxApiResult:
    available: bool
    data: Any = None
    error: str | None = None


def _base_url() -> str:
    return (settings.quantx_api_base_url or "").strip().rstrip("/")


async def _get(path: str, params: dict | None = None) -> QuantxApiResult:
    base = _base_url()
    if not base:
        return QuantxApiResult(available=False, error="QUANTX_API_BASE_URL 未配置")
    url = f"{base}{path}"
    try:
        async with httpx.AsyncClient(timeout=settings.quantx_api_timeout) as client:
            resp = await client.get(url, params=params)
        if resp.status_code >= 400:
            detail = resp.text[:200]
            try:
                body = resp.json()
                detail = body.get("detail", detail) if isinstance(body, dict) else detail
            except Exception:  # noqa: BLE001
                pass
            return QuantxApiResult(available=False, error=f"HTTP {resp.status_code}: {detail}")
        return QuantxApiResult(available=True, data=resp.json())
    except httpx.TimeoutException:
        return QuantxApiResult(available=False, error="QuantX API 请求超时")
    except httpx.ConnectError as e:
        return QuantxApiResult(available=False, error=f"QuantX API 无法连接: {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning("quantx_proxy: %s 请求失败: %s", path, e)
        return QuantxApiResult(available=False, error=str(e)[:200])


async def get_positions() -> QuantxApiResult:
    """持仓 + 资产, 合并 /api/trading/positions 与 /api/trading/asset。只读。"""
    pos_result = await _get("/api/trading/positions")
    if not pos_result.available:
        return pos_result
    asset_result = await _get("/api/trading/asset")
    return QuantxApiResult(
        available=True,
        data={
            "positions": (pos_result.data or {}).get("positions", []),
            "asset": (asset_result.data or {}).get("asset") if asset_result.available else None,
            "server_fetched_at": (pos_result.data or {}).get("server_fetched_at"),
        },
    )


async def get_decisions(minutes: int = 240, action: str = "") -> QuantxApiResult:
    """盘中持仓决策建议 (HOLD/REDUCE/EXIT/TAKE_PROFIT/ADD), 只读 (不代表已下单)。"""
    params: dict[str, Any] = {"minutes": minutes}
    if action:
        params["action"] = action
    return await _get("/api/monitoring/position-decisions", params=params)


async def get_alerts(minutes: int = 60) -> QuantxApiResult:
    """最近告警 (含风控类)。"""
    return await _get("/api/monitoring/alerts", params={"minutes": minutes})


async def get_status(deep: bool = False, alert_minutes: int = 60) -> QuantxApiResult:
    """实盘运行健康总览: 进程/网关/后台循环/告警统计/止损单/账本。shallow 默认零成本。"""
    return await _get("/api/monitoring/runtime-status", params={"deep": deep, "alert_minutes": alert_minutes})
