"""QuantX 只读代理 API — 持仓/决策建议/告警/运行健康。

边界声明: 全部为 GET, 只读展示。TickFlow 不执行交易, 不代理任何下单/撤单/
止损单创建等写入端点 (见 app.services.quantx_proxy 模块注释)。
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.services import quantx_proxy

router = APIRouter(prefix="/api/quantx", tags=["quantx"])


def _payload(result: quantx_proxy.QuantxApiResult) -> dict:
    if not result.available:
        return {"available": False, "error": result.error}
    return {"available": True, **(result.data or {})}


@router.get("/positions")
async def positions() -> dict:
    """持仓 + 资产 (只读, 代理 QuantX /api/trading/positions + /api/trading/asset)。"""
    return _payload(await quantx_proxy.get_positions())


@router.get("/decisions")
async def decisions(
    minutes: int = Query(240, ge=1, le=1440),
    action: str = Query("", description="可选: HOLD/REDUCE/EXIT/TAKE_PROFIT/ADD"),
) -> dict:
    """盘中持仓决策建议 (只读, 不代表已下单)。"""
    return _payload(await quantx_proxy.get_decisions(minutes=minutes, action=action))


@router.get("/alerts")
async def alerts(minutes: int = Query(60, ge=1, le=1440)) -> dict:
    """最近告警 (含风控类)。"""
    return _payload(await quantx_proxy.get_alerts(minutes=minutes))


@router.get("/status")
async def status(deep: bool = False, alert_minutes: int = Query(60, ge=1, le=1440)) -> dict:
    """实盘运行健康总览。"""
    return _payload(await quantx_proxy.get_status(deep=deep, alert_minutes=alert_minutes))
