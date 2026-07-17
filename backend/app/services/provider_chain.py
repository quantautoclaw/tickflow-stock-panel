"""实时行情 provider chain (§14)。

按配置的链顺序依次请求 provider, 前序 provider 优先, 覆盖率达阈值后停止。
QuantX 失败或覆盖不足时回退 TickFlow (按 TickFlow 能力门控)。

返回 RealtimeChainResult: 合并记录 + 来源统计 + 覆盖率 + 错误。
QuoteService 不再特判 QuantX, 统一通过本模块路由。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import polars as pl

logger = logging.getLogger(__name__)

# 默认停止阈值: 覆盖率达到 95% 后不再请求后续 provider
REALTIME_COVERAGE_STOP_RATIO = 0.95


@dataclass
class RealtimeChainResult:
    records: list[dict]
    sources: list[str]
    source_counts: dict[str, int]
    expected_symbols: int
    received_symbols: int
    coverage_ratio: float
    errors: dict[str, str]
    elapsed_ms: float
    fallback_used: bool = False  # 是否发生回退


@dataclass
class _ChainState:
    records_by_symbol: dict[str, dict] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    fallback_used: bool = False


def _expected_symbol_set(repo) -> set[str]:
    """从 repo 收集期望的 symbol 集合 (股票/ETF/指数, 按启用类型)。"""
    expected: set[str] = set()
    if repo is None:
        return expected
    from app.services import preferences
    try:
        if preferences.get_realtime_pull_stock():
            df = repo.get_instruments()
            if not df.is_empty() and "symbol" in df.columns:
                expected.update(df["symbol"].cast(pl.Utf8).to_list())
    except Exception as e:
        logger.debug("expected stocks failed: %s", e)
    try:
        if preferences.get_realtime_pull_etf():
            df = repo.get_etf_instruments()
            if not df.is_empty() and "symbol" in df.columns:
                expected.update(df["symbol"].cast(pl.Utf8).to_list())
    except Exception as e:
        logger.debug("expected etfs failed: %s", e)
    try:
        if preferences.get_realtime_pull_index():
            if preferences.get_realtime_index_mode() == "all":
                df = repo.get_index_instruments()
                if not df.is_empty() and "symbol" in df.columns:
                    df = df.filter(pl.col("asset_type") != "etf") if "asset_type" in df.columns else df
                    expected.update(df["symbol"].cast(pl.Utf8).drop_nulls().to_list())
            else:
                from app.services.quote_service import QuoteService

                expected.update(
                    preferences.get_realtime_index_symbols() or QuoteService.CORE_INDEX_SYMBOLS
                )
    except Exception as e:
        logger.debug("expected indices failed: %s", e)
    expected.discard(None)
    expected.discard("")
    return expected


def _fetch_from_plugin(name: str) -> list[dict] | None:
    """从插件 provider 拉实时行情, 返回 records 或 None (失败)。"""
    try:
        from app.data_providers import custom as custom_sources
        if name not in custom_sources.names():
            return None
        provider = custom_sources.get_provider(name)
        if not hasattr(provider, "get_realtime"):
            return None
        return provider.get_realtime() or []
    except Exception as e:
        logger.warning("provider_chain %s get_realtime 失败: %s", name, e)
        return None


def _fetch_from_tickflow(capset, expected: set[str]) -> list[dict] | None:
    """TickFlow 实时行情: 遵守 capability 门控 (§14.3)。

    - none: 不可用。
    - free: 不执行全市场 fallback，但可按 symbol 补最多 5 个核心指数。
    - Starter+: 允许全市场 fallback。
    """
    try:
        from app.tickflow.capabilities import Cap
    except Exception:
        return None
    tier = ""
    try:
        from app.services.quote_service import QuoteService
        tier = QuoteService._current_tier()
    except Exception:
        pass
    if tier == "none":
        return None
    if tier == "free":
        # 第三方全市场源可能不返回指数。Free 虽不能调用 get_by_universes，
        # 但 quote.by_symbol 单批支持 5 个，足够补首页 4 个核心指数。
        from app.services import preferences
        if not preferences.get_realtime_pull_index():
            return None
        configured = preferences.get_realtime_index_symbols()
        symbols = [s for s in dict.fromkeys(configured) if s in expected][:5]
        if not symbols:
            return None
        from app.tickflow.client import get_paid_realtime_client
        tf = get_paid_realtime_client()
        if tf is None:
            return None
        try:
            return _tickflow_resp_to_records(tf.quotes.get(symbols=symbols))
        except Exception as e:
            logger.warning("provider_chain tickflow core indices 失败: %s", e)
            return None
    if capset is None or not capset.has(Cap.QUOTE_POOL):
        return None
    # 调用现有 QuoteService 的 TickFlow 全市场拉取路径
    from app.tickflow.client import get_paid_realtime_client
    tf = get_paid_realtime_client()
    if tf is None:
        return None
    from app.services import preferences
    universes: list[str] = []
    if preferences.get_realtime_pull_stock():
        universes.append("CN_Equity_A")
    if preferences.get_realtime_pull_etf():
        universes.append("CN_ETF")
    if preferences.get_realtime_pull_index() and preferences.get_realtime_index_mode() == "all":
        universes.append("CN_Index")
    if not universes:
        return None
    try:
        resp = tf.quotes.get_by_universes(universes=universes)
    except Exception as e:
        logger.warning("provider_chain tickflow get_by_universes 失败: %s", e)
        return None
    return _tickflow_resp_to_records(resp)


def _tickflow_resp_to_records(resp) -> list[dict]:
    """把 TickFlow quotes 响应转为标准 record (last_price 字段)。"""
    records: list[dict] = []
    if resp is None:
        return records
    import math
    items: list = []
    if isinstance(resp, pl.DataFrame):
        items = resp.to_dicts()
    elif hasattr(resp, "to_dict"):
        items = resp.to_dict("records")
    elif isinstance(resp, list):
        items = resp
    for q in items:
        item = q if isinstance(q, dict) else {}
        sym = item.get("symbol")
        if not sym:
            continue
        last = item.get("last") or item.get("last_price") or item.get("price")
        try:
            last = float(last) if last is not None else 0.0
        except (TypeError, ValueError):
            last = 0.0
        if last <= 0 or math.isnan(last):
            continue
        ext = item.get("ext") or {}
        prev_close = _safe_float(item.get("prev_close"))
        high = _safe_float(item.get("high"))
        low = _safe_float(item.get("low"))
        change_amount = _safe_float(ext.get("change_amount"), default=last - prev_close if prev_close else 0.0)
        change_pct = _safe_float(ext.get("change_pct"), default=change_amount / prev_close if prev_close else 0.0)
        amplitude = _safe_float(ext.get("amplitude"), default=(high - low) / prev_close if prev_close else 0.0)
        records.append({
            "symbol": str(sym),
            "name": ext.get("name") or item.get("name") or "",
            "last_price": last,
            "prev_close": prev_close,
            "open": _safe_float(item.get("open")),
            "high": high,
            "low": low,
            "volume": _safe_float(item.get("volume")),
            "amount": _safe_float(item.get("amount")),
            "change_pct": change_pct,
            "change_amount": change_amount,
            "amplitude": amplitude,
            "_source": "tickflow",
        })
    return records


def _safe_float(v, default=0.0) -> float:
    try:
        f = float(v) if v is not None else default
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _is_valid_record(rec: dict) -> bool:
    sym = rec.get("symbol")
    last = _safe_float(rec.get("last_price"), default=0.0)
    return bool(sym) and last > 0


def fetch_realtime_chain(
    provider_names: list[str],
    repo,
    capset,
    *,
    stop_ratio: float = REALTIME_COVERAGE_STOP_RATIO,
) -> RealtimeChainResult:
    """按链顺序请求 provider, 合并记录, 返回统计。

    provider_names 为空时由调用方决定回退 (通常用旧 realtime_data_provider)。
    """
    t0 = time.perf_counter()
    expected = _expected_symbol_set(repo)
    expected_count = len(expected)
    state = _ChainState()

    if not provider_names:
        return RealtimeChainResult(
            records=[], sources=[], source_counts={},
            expected_symbols=expected_count, received_symbols=0,
            coverage_ratio=0.0, errors={},
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    def _coverage() -> float:
        if expected_count == 0:
            return 1.0 if state.records_by_symbol else 0.0
        return len(state.records_by_symbol) / expected_count

    for idx, name in enumerate(provider_names):
        if expected_count > 0 and _coverage() >= stop_ratio:
            logger.debug("provider_chain: coverage %.2f >= %.2f, 停止后续 provider", _coverage(), stop_ratio)
            break

        records = None
        if name == "tickflow":
            records = _fetch_from_tickflow(capset, expected)
        else:
            records = _fetch_from_plugin(name)

        if records is None:
            state.errors[name] = "fetch_failed_or_unavailable"
            if idx > 0:
                state.fallback_used = True
            continue

        # 合并: 前序 provider 优先 (setdefault)
        added = 0
        source_tag = name
        for rec in records:
            if not _is_valid_record(rec):
                continue
            sym = str(rec["symbol"])
            # expected 非空时只接收已启用资产类型/范围内的 symbol, 避免 ETF/指数
            # 污染股票覆盖率并被写入未启用的数据目录。
            if expected and sym not in expected:
                continue
            rec["symbol"] = sym
            tag = rec.get("_source") or source_tag
            if sym not in state.records_by_symbol:
                state.records_by_symbol[sym] = rec
                state.source_counts[tag] = state.source_counts.get(tag, 0) + 1
                if tag not in state.sources:
                    state.sources.append(tag)
                added += 1

        if idx > 0 and added > 0:
            state.fallback_used = True
        logger.info(
            "provider_chain: %s +symbol=%d (total=%d/%d, coverage=%.2f)",
            name, added, len(state.records_by_symbol), expected_count, _coverage(),
        )

    received = len(state.records_by_symbol)
    coverage = received / expected_count if expected_count else (1.0 if received else 0.0)
    return RealtimeChainResult(
        records=list(state.records_by_symbol.values()),
        sources=state.sources,
        source_counts=state.source_counts,
        expected_symbols=expected_count,
        received_symbols=received,
        coverage_ratio=round(coverage, 4),
        errors=state.errors,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        fallback_used=state.fallback_used,
    )


def has_available_full_market_provider_chain() -> bool:
    """是否存在可提供全市场实时行情的 chain (§14.4 QuoteService.realtime_mode 用)。

    chain 非空且至少有一个可用 provider (非 tickflow 或 tickflow 付费档) 时返回 True。
    """
    from app.services import preferences
    chain = preferences.get_realtime_provider_chain()
    if not chain:
        return False
    candidates = preferences._realtime_chain_candidates()
    # 本函数只负责判断“可绕过 TickFlow 档位限制”的第三方全市场源。
    # chain 仅含 tickflow 时仍应沿用 QuoteService 原有 none/free/paid 模式。
    return any(n != "tickflow" and n in candidates for n in chain)
