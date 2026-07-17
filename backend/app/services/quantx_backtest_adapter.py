"""严谨回测适配器 — 把 TickFlow 策略翻译为 QuantX StrategyConfig, 通过 QuantX
apps/api 的 POST /api/strategies/execute-config 同步执行, 归一化结果。

不使用 subprocess: QuantX 已提供该端点同步跑完整回测并返回结果 (含结果落库),
复用与 P2 只读代理相同的 QUANTX_API_BASE_URL 配置和错误处理约定。

边界: 这是「打分排名逻辑」的严谨复核, 不是 TickFlow 策略的完整移植 —— 见
quantx_strategy_translator 模块注释。回测超时用独立的 QUANTX_BACKTEST_TIMEOUT
(默认 300s, 明显长于监控代理的 5s), 因为 rqalpha 事件驱动回测可能耗时数分钟。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.services.quantx_strategy_translator import TranslationResult, translate_strategy_to_quantx_config

logger = logging.getLogger(__name__)


@dataclass
class RigorousBacktestResult:
    ok: bool
    translation_warnings: list[str]
    result: dict | None = None
    error: str | None = None


def _base_url() -> str:
    return (settings.quantx_api_base_url or "").strip().rstrip("/")


def _normalize_engine_result(raw: dict) -> dict:
    """QuantX EngineResultResponse → TickFlow 友好的展示 schema。

    daily_nav[].nav (归一化净值, 起点 1.0) 映射为 equity_curve[].value, 与
    TickFlow 自有引擎的 SimResult.equity_curve 形状一致, 便于复用同一套图表。
    summary 直接透传 (字段名如 total_return/max_drawdown/sharpe 与 TickFlow
    口径基本一致, 但计算实现不同, 不保证数值逐位相同)。

    trades 保留 QuantX 原始逐笔成交记录 (按 fill 而非 TickFlow 的按持仓周期
    entry/exit 配对), 不做配对重建 —— 严谨回测的核心是 summary 统计指标,
    不是逐笔比对。
    """
    daily_nav = raw.get("daily_nav") or []
    equity_curve = [
        {"date": row.get("date"), "value": row.get("nav")}
        for row in daily_nav
        if row.get("date") is not None and row.get("nav") is not None
    ]
    return {
        "engine": raw.get("engine", ""),
        "dialect": raw.get("dialect", ""),
        "benchmark": raw.get("benchmark", ""),
        "equity_curve": equity_curve,
        "summary": raw.get("summary") or {},
        "trades": raw.get("trades") or [],
        "trade_count": len(raw.get("trades") or []),
        "logs": raw.get("logs") or [],
    }


async def run_rigorous_backtest(
    *,
    strategy_id: str,
    scoring: dict[str, float],
    limit: int,
    basic_filter: dict | None,
    stop_loss: float | None,
    max_hold_days: int | None,
    start_date: str,
    end_date: str,
    initial_capital: float = 1_000_000.0,
    benchmark: str = "000300.SH",
) -> RigorousBacktestResult:
    translation: TranslationResult = translate_strategy_to_quantx_config(
        strategy_id=strategy_id,
        scoring=scoring,
        limit=limit,
        basic_filter=basic_filter,
        stop_loss=stop_loss,
        max_hold_days=max_hold_days,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        benchmark=benchmark,
    )
    if not translation.ok:
        return RigorousBacktestResult(ok=False, translation_warnings=translation.warnings, error="; ".join(translation.errors))

    base = _base_url()
    if not base:
        return RigorousBacktestResult(ok=False, translation_warnings=translation.warnings, error="QUANTX_API_BASE_URL 未配置")

    payload: dict[str, Any] = {"config": translation.config}
    url = f"{base}/api/strategies/execute-config"
    try:
        async with httpx.AsyncClient(timeout=settings.quantx_backtest_timeout) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            detail = resp.text[:500]
            try:
                body = resp.json()
                if isinstance(body, dict):
                    detail = str(body.get("detail", detail))[:500]
            except Exception:  # noqa: BLE001
                pass
            return RigorousBacktestResult(
                ok=False, translation_warnings=translation.warnings,
                error=f"QuantX 回测失败 HTTP {resp.status_code}: {detail}",
            )
        normalized = _normalize_engine_result(resp.json())
        return RigorousBacktestResult(ok=True, translation_warnings=translation.warnings, result=normalized)
    except httpx.TimeoutException:
        return RigorousBacktestResult(
            ok=False, translation_warnings=translation.warnings,
            error=f"QuantX 回测请求超时 (>{settings.quantx_backtest_timeout:.0f}s)",
        )
    except httpx.ConnectError as e:
        return RigorousBacktestResult(ok=False, translation_warnings=translation.warnings, error=f"QuantX API 无法连接: {e}")
    except Exception as e:  # noqa: BLE001
        logger.warning("quantx_backtest_adapter: 执行失败: %s", e)
        return RigorousBacktestResult(ok=False, translation_warnings=translation.warnings, error=str(e)[:300])
