"""历史数据增强服务 (§11)。

按 (symbol, date) 用增强源(QuantX 等)补缺, 不覆盖主数据源已有 key。
主入口 run_enhancement: 校验 → instruments → universe → get_daily(不复权) →
merge_daily_asset(keep_existing) → 重算 enriched → 刷新视图/缓存 → 返回汇总。

设计要点:
  - 日K统一「不复权」进入 pipeline, 复权只算一次 (由 indicators.pipeline 完成)。
  - enriched 重算策略 (§11.5):
      股票: 新日期分区 → run_pipeline(new_dates_only=True); 已有日期补 symbol → run_pipeline(symbols=...)
      指数: recompute_asset_enriched(index)
      ETF:  读 adj_factor_etf, compute_enriched 后 merge 回分区
  - 正确性优先于首期性能。
"""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.custom.loader import get_provider, is_builtin, list_plugins
from app.tickflow.repository import KlineRepository, MergeResult

logger = logging.getLogger(__name__)

_VALID_ASSET_TYPES = ("stock", "etf", "index")


@dataclass(frozen=True)
class EnhancementRequest:
    """增强任务请求。"""

    provider: str
    start_date: date
    end_date: date
    asset_types: tuple[str, ...] = ("stock", "etf", "index")
    conflict: str = "keep_existing"

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError(f"start_date({self.start_date}) > end_date({self.end_date})")
        if self.conflict != "keep_existing":
            raise ValueError("首期历史增强只允许 conflict=keep_existing, 避免覆盖主数据")
        if not self.asset_types:
            raise ValueError("asset_types 不能为空")
        bad = [a for a in self.asset_types if a not in _VALID_ASSET_TYPES]
        if bad:
            raise ValueError(f"未知 asset_type: {bad} (合法: {_VALID_ASSET_TYPES})")


@dataclass
class _AssetResult:
    input: int = 0
    inserted: int = 0
    inserted_symbols: list[str] = field(default_factory=list)
    inserted_dates: list[str] = field(default_factory=list)
    existing_dates_changed: list[str] = field(default_factory=list)


@dataclass
class EnhancementSummary:
    provider: str
    started_at: str
    finished_at: str
    assets: dict[str, dict]
    inserted_rows: int
    affected_symbols: list[str]
    affected_dates: list[str]
    # 细粒度日期信息 (供盘后管道 union 到统一 enriched 重算):
    new_dates: list[str] = field(default_factory=list)              # 全新日期分区
    existing_dates_changed: list[str] = field(default_factory=list)  # 已有日期被补 symbol


# ================================================================
# 校验
# ================================================================

def _resolve_provider(provider_name: str):
    """校验 provider 已加载、available、role 合法。返回 (provider, manifest)。"""
    if not is_builtin(provider_name):
        raise ValueError(f"provider '{provider_name}' 不是已注册的内置插件")
    manifest = next((p for p in list_plugins() if p["name"] == provider_name), None)
    if manifest is None:
        raise ValueError(f"插件 '{provider_name}' 未注册")
    if not manifest.get("available"):
        raise ValueError(f"插件 '{provider_name}' 当前不可用: {manifest.get('status', '')}")
    role = str(manifest.get("role", "provider")).lower()
    if role not in {"enhancer", "both"}:
        raise ValueError(f"插件 '{provider_name}' role={role}, 不可作为增强源")
    provider = get_provider(provider_name)
    if not hasattr(provider, "get_daily"):
        raise ValueError(f"插件 '{provider_name}' 不支持 get_daily, 无法做历史增强")
    return provider, manifest


# ================================================================
# instruments 合并 (§10)
# ================================================================

def _enhance_instruments(
    repo: KlineRepository,
    provider,
    manifest,
    requested_asset_types: tuple[str, ...],
) -> int:
    """获取 provider instruments 并与 TickFlow 维表字段级 coalesce 合并。

    返回新增/更新的 symbol 数 (粗略)。provider 无 get_instruments 时跳过。
    """
    if not hasattr(provider, "get_instruments"):
        return 0
    supported = set(manifest.get("asset_types", ["stock"]) or ["stock"])
    asset_types = [asset for asset in requested_asset_types if asset in supported]
    total = 0
    for at in asset_types:
        try:
            enh_rows = provider.get_instruments(at)
        except Exception as e:
            logger.warning("enhance instruments(%s) from %s 失败: %s", at, provider.name, e)
            continue
        if not enh_rows:
            continue
        from app.services.instrument_sync import _flatten_instruments, merge_instrument_frames

        enh = pl.DataFrame(_flatten_instruments(enh_rows))
        # 取主表
        if at == "stock":
            primary = repo.get_instruments()
        elif at == "index":
            primary = repo.get_index_instruments()
        else:
            primary = repo.get_etf_instruments()
        merged = merge_instrument_frames(primary, enh)
        if merged.is_empty():
            continue
        repo.save_instruments(at, merged)
        total += merged.height
        logger.info("enhance instruments(%s): merged %d rows", at, merged.height)
    return total


# ================================================================
# symbol universe (§11.4)
# ================================================================

def _resolve_symbols(repo: KlineRepository, provider, asset_type: str) -> list[str]:
    """优先级: repo instruments → provider.get_instruments。失败返回空。"""
    try:
        if asset_type == "stock":
            df = repo.get_instruments()
        elif asset_type == "index":
            df = repo.get_index_instruments()
            if not df.is_empty() and "asset_type" in df.columns:
                df = df.filter(pl.col("asset_type") != "etf")
        else:
            df = repo.get_etf_instruments()
        if not df.is_empty() and "symbol" in df.columns:
            return sorted(set(df["symbol"].cast(pl.Utf8).to_list()))
    except Exception as e:
        logger.debug("resolve symbols from repo(%s) failed: %s", asset_type, e)
    # 回退: provider instruments
    if hasattr(provider, "get_instruments"):
        try:
            rows = provider.get_instruments(asset_type)
            return sorted({str(r.get("symbol", "")) for r in rows if r.get("symbol")})
        except Exception as e:
            logger.warning("resolve symbols from provider(%s) failed: %s", asset_type, e)
    return []


# ================================================================
# enriched 重算 (§11.5)
# ================================================================

def recompute_asset_enriched(
    repo: KlineRepository,
    asset_type: str = "index",
    symbols: list[str] | None = None,
    target_dates: list[date] | None = None,
) -> int:
    """指数/ETF enriched 重算。返回写入行数。

    - 新日期: 读至少 120 个日历日历史预热, 只写 target_dates。
    - 已有日期补 symbol: 为 affected symbols 读完整可用历史后重算, merge 回分区。
    正确性优先于首期性能。
    """
    from app.indicators.pipeline import compute_enriched

    data_dir = repo.store.data_dir
    table = {"index": "kline_index_daily", "etf": "kline_etf_daily"}.get(asset_type)
    enrich_table = {"index": "kline_index_enriched", "etf": "kline_etf_enriched"}.get(asset_type)
    if not table:
        return 0

    def _read_daily(
        start: date,
        end: date,
        selected_symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        # 扫描日K目录, 按日期范围过滤
        import glob as _glob
        files = []
        for d in _glob.glob(str(data_dir / table / "date=*")):
            from pathlib import Path
            ds = Path(d).name.split("=", 1)[1]
            try:
                dd = date.fromisoformat(ds)
            except ValueError:
                continue
            if start <= dd <= end:
                files.append(f"{d}/part.parquet")
        if not files:
            return pl.DataFrame()
        try:
            scan = pl.scan_parquet(files)
            if selected_symbols:
                scan = scan.filter(pl.col("symbol").is_in(selected_symbols))
            return scan.collect()
        except Exception as e:
            logger.warning("read %s daily for enriched recompute failed: %s", asset_type, e)
            return pl.DataFrame()

    today = date.today()
    total = 0

    # 因子 (ETF 用)
    factors = pl.DataFrame()
    if asset_type == "etf":
        fpath = data_dir / "adj_factor_etf" / "all.parquet"
        if fpath.exists():
            try:
                factors = pl.read_parquet(fpath)
            except Exception as e:
                logger.warning("ETF adj_factor 读取失败: %s", e)

    if target_dates:
        # 新日期分区: 预热 120 日, 只写 target_dates
        earliest = min(target_dates) - timedelta(days=120)
        raw = _read_daily(earliest, today)
        if raw.is_empty():
            return 0
        for td in target_dates:
            td_str = td.isoformat()
            # 给目标日期重算需该日全部 symbol 的历史; 但预热窗口内所有日期都参与计算,
            # 只把目标日期的 enriched 结果写回。
            batch_factors = factors if factors.is_empty() else factors.filter(
                pl.col("trade_date") <= td_str
            ) if "trade_date" in factors.columns else factors
            sub = raw.filter(pl.col("date") <= td)
            if sub.is_empty():
                continue
            enriched = compute_enriched(sub, factors=batch_factors, instruments=None)
            # 只保留目标日期的行
            enriched_td = enriched.filter(pl.col("date") == td)
            if enriched_td.is_empty():
                continue
            _merge_enriched_partition(repo, enrich_table, enriched_td, td)
            total += enriched_td.height
    elif symbols:
        # 已有日期补 symbol: 一次读取受影响 symbol 的完整历史并统一重算, 避免
        # 对每个 symbol 重复扫描全量 parquet。
        symbol_set = set(symbols)
        raw = _read_daily(date(2000, 1, 1), today, sorted(symbol_set))
        if raw.is_empty():
            return 0
        if raw.is_empty():
            return 0
        batch_factors = (
            factors
            if factors.is_empty() or "symbol" not in factors.columns
            else factors.filter(pl.col("symbol").is_in(sorted(symbol_set)))
        )
        enriched = compute_enriched(raw, factors=batch_factors, instruments=None)
        for td in sorted({d for d in enriched["date"].to_list() if hasattr(d, "isoformat")}):
            date_frame = enriched.filter(pl.col("date") == td)
            _merge_enriched_partition(repo, enrich_table, date_frame, td)
            total += date_frame.height
    return total


def _merge_enriched_partition(repo: KlineRepository, enrich_table: str, df: pl.DataFrame, dt: date) -> None:
    """把单日 enriched merge 回分区 (replace, keep last)。"""
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS
    if df.is_empty():
        return
    storage_cols = [c for c in ENRICHED_STORAGE_COLS if c in df.columns]
    df_storage = df.select(storage_cols)
    out = repo.store.data_dir / enrich_table / f"date={dt.isoformat()}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    with repo._write_lock:
        if out.exists():
            existing = pl.read_parquet(out)
            df_storage = pl.concat([existing, df_storage], how="diagonal_relaxed").unique(
                subset=["symbol", "date"], keep="last"
            )
        repo._atomic_write_parquet(df_storage.sort(["symbol"]), out)


# ================================================================
# 主入口
# ================================================================

def _date_windows(start: date, end: date, days: int = 180):
    """把闭区间拆成有限窗口, 避免五年全市场数据一次性驻留内存。"""
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=days - 1))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def _combine_merge_results(results: list[MergeResult]) -> MergeResult:
    return MergeResult(
        input_rows=sum(r.input_rows for r in results),
        inserted_rows=sum(r.inserted_rows for r in results),
        replaced_rows=sum(r.replaced_rows for r in results),
        unchanged_rows=sum(r.unchanged_rows for r in results),
        inserted_symbols=frozenset().union(*(r.inserted_symbols for r in results)),
        inserted_dates=frozenset().union(*(r.inserted_dates for r in results)),
        existing_dates_changed=frozenset().union(
            *(r.existing_dates_changed for r in results)
        ),
    )


def run_enhancement(
    repo: KlineRepository,
    request: EnhancementRequest,
    on_progress=None,
    skip_enriched_recompute: bool = False,
) -> EnhancementSummary:
    """执行一次历史数据增强。

    skip_enriched_recompute=True 时只做 merge, 不重算 enriched/不刷新缓存
    (供盘后管道复用: 由主管道统一 enriched 计算)。返回的 summary 仍含 symbols/dates。
    """
    started = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="seconds")

    def _emit(stage: str, pct: int, msg: str) -> None:
        if on_progress:
            with contextlib.suppress(Exception):
                on_progress(stage, pct, msg)

    _emit("validate", 2, "校验增强源…")
    provider, manifest = _resolve_provider(request.provider)

    _emit("enhance_instruments", 5, "增强标的维表…")
    try:
        _enhance_instruments(repo, provider, manifest, request.asset_types)
    except Exception as e:
        logger.warning("enhance instruments failed (继续): %s", e)

    all_inserted = 0
    all_symbols: set[str] = set()
    all_dates: set[date] = set()
    new_date_partitions: set[date] = set()
    existing_dates_with_changes: set[date] = set()
    assets_out: dict[str, dict] = {}
    n_assets = len(request.asset_types)

    for idx, at in enumerate(request.asset_types):
        base_pct = 8 + int(70 * idx / max(1, n_assets))
        span_pct = int(70 / max(1, n_assets))
        _emit(f"enhance_{at}_daily", base_pct, f"获取 {at} 日K…")

        symbols = _resolve_symbols(repo, provider, at)
        if not symbols:
            logger.info("enhance %s: symbol universe 为空, 跳过", at)
            assets_out[at] = {"input": 0, "inserted": 0, "symbols": 0, "skipped": "empty_universe"}
            continue

        windows = list(_date_windows(request.start_date, request.end_date))
        batch_results: list[MergeResult] = []
        for batch_idx, (window_start, window_end) in enumerate(windows, start=1):
            batch_pct = base_pct + int((span_pct * 0.55) * batch_idx / len(windows))
            _emit(
                f"enhance_{at}_daily",
                batch_pct,
                f"获取 {at} 日K {batch_idx}/{len(windows)} "
                f"[{window_start} ~ {window_end}]…",
            )
            try:
                df = provider.get_daily(
                    symbols=symbols,
                    start_time=datetime.combine(window_start, datetime.min.time()),
                    end_time=datetime.combine(window_end, datetime.min.time()),
                    asset_type=at,
                )
            except Exception as e:
                raise RuntimeError(
                    f"增强源 {request.provider} 获取 {at} 日K失败 "
                    f"[{window_start} ~ {window_end}]: {e}"
                ) from e
            if df.is_empty():
                continue

            # 日期范围二次过滤 (§16.4)
            df = df.filter(
                (pl.col("date") >= window_start) & (pl.col("date") <= window_end)
            )
            if df.is_empty():
                continue
            batch_results.append(
                repo.merge_daily_asset(at, df, conflict="keep_existing")
            )

        if not batch_results:
            assets_out[at] = {
                "input": 0,
                "inserted": 0,
                "symbols": len(symbols),
                "skipped": "empty_data",
            }
            continue

        result = _combine_merge_results(batch_results)
        assets_out[at] = {
            "input": result.input_rows,
            "inserted": result.inserted_rows,
            "symbols": len(symbols),
            "batches": len(windows),
        }
        all_inserted += result.inserted_rows
        all_symbols.update(result.inserted_symbols)
        all_dates.update(result.inserted_dates)
        all_dates.update(result.existing_dates_changed)
        new_date_partitions.update(result.inserted_dates)
        existing_dates_with_changes.update(result.existing_dates_changed)

        # enriched 重算 (§11.5) — 跳过时由调用方(盘后管道)统一处理
        if not skip_enriched_recompute:
            _emit(f"compute_{at}_enriched", base_pct + int(span_pct * 0.7), f"重算 {at} enriched…")
            _recompute_enriched_for_merge(repo, at, result)

        logger.info(
            "quantx enhancement: asset=%s range=%s..%s input=%d inserted=%d existing_changed=%d",
            at, request.start_date, request.end_date,
            result.input_rows, result.inserted_rows, len(result.existing_dates_changed),
        )

    _emit("refresh_views", 90, "刷新视图与缓存…")
    if not skip_enriched_recompute:
        repo.rebuild_views()
        repo.clear_cache()
        repo.refresh_cache()

    finished_at = datetime.now().isoformat(timespec="seconds")
    elapsed = time.perf_counter() - started
    logger.info(
        "enhancement done: provider=%s inserted=%d elapsed=%.1fs",
        request.provider, all_inserted, elapsed,
    )
    _emit("done", 100, f"完成, 新增 {all_inserted} 行")

    return EnhancementSummary(
        provider=request.provider,
        started_at=started_at,
        finished_at=finished_at,
        assets=assets_out,
        inserted_rows=all_inserted,
        affected_symbols=sorted(all_symbols),
        affected_dates=sorted(d.isoformat() for d in all_dates),
        new_dates=sorted(d.isoformat() for d in new_date_partitions),
        existing_dates_changed=sorted(d.isoformat() for d in existing_dates_with_changes),
    )


def _recompute_enriched_for_merge(repo: KlineRepository, asset_type: str, result: MergeResult) -> None:
    """根据 MergeResult 选择 enriched 重算策略 (§11.5)。"""
    if result.inserted_rows == 0 and not result.existing_dates_changed:
        return  # 无变化

    if asset_type == "stock":
        from app.indicators.pipeline import run_pipeline
        new_dates = result.inserted_dates
        new_symbols = [s for s in result.inserted_symbols]
        if new_dates:
            # 有全新日期分区
            run_pipeline(new_dates_only=True, symbols=new_symbols or None)
        elif new_symbols:
            # 仅给已有日期补了 symbol
            run_pipeline(symbols=new_symbols)
    else:
        # 指数 / ETF: 新日期与已有日期补 symbol 可能同时发生, 两类都要重算。
        target_dates = sorted(result.inserted_dates)
        if target_dates:
            recompute_asset_enriched(
                repo,
                asset_type=asset_type,
                target_dates=target_dates,
            )
        if result.existing_dates_changed and result.inserted_symbols:
            recompute_asset_enriched(
                repo,
                asset_type=asset_type,
                symbols=sorted(result.inserted_symbols),
            )
