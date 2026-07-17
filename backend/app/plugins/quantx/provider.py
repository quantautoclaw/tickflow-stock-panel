"""QuantX 数据增强 Provider。

通过 quantx_data public API 读取 (DataStore + HttpQuoteChain), 归一化到项目内部 schema。
定位为「增强源」(role=enhancer): 历史日K仅补缺, 实时行情按 chain 路由。

契约:
  - get_daily(): 返回「不复权 OHLCV」(adjust=""), 由项目 pipeline 统一前复权。
  - get_realtime(): 多源实时快照, 标准化字段。
  - get_instruments(): 标的维表 (字段兼容 instrument_sync._flatten_instruments)。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime

import polars as pl

from app.plugins.quantx import bridge

logger = logging.getLogger(__name__)

# 日K标准输出列 (顺序固定)
_DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]

# QuantX asset_type → 项目 asset_type 过滤标记
_ASSET_TYPE_MAP = {
    "stock": {"CS"},
    "etf": {"ETF"},
}

# symbol 后缀白名单 (QuantX 数据已是 .SH/.SZ/.BJ canonical 格式)
_VALID_SUFFIXES = {".SH", ".SZ", ".BJ"}
_SUFFIX_ALIASES = {
    ".SSE": ".SH",
    ".XSHG": ".SH",
    ".SZSE": ".SZ",
    ".XSHE": ".SZ",
    ".BSE": ".BJ",
    ".XBJ": ".BJ",
}


@dataclass
class _QuantXConfig:
    """轻量 config shim, 让 custom loader 的 list_sources/provider_has_dataset 识别本 provider。"""

    name: str = "quantx"
    display_name: str = "QuantX 数据增强"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(("daily", "realtime", "instruments")))
    path: None = None
    builtin: bool = True
    role: str = "enhancer"


def _yyyymmdd(d: date | datetime | None) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime("%Y-%m-%d")


class QuantXProvider:
    """QuantX 数据增强 provider。"""

    name = "quantx"
    builtin = True

    def __init__(self) -> None:
        self.config = _QuantXConfig()

    def close(self) -> None:  # loader.load_all 会对每个 provider 调 close
        bridge.close()

    # ================================================================
    # 辅助: symbol / asset_type 处理
    # ================================================================

    @staticmethod
    def _validate_symbols(df: pl.DataFrame) -> pl.DataFrame:
        """统一交易所后缀并拒绝无法识别的 symbol。"""
        if df.is_empty() or "symbol" not in df.columns:
            return df

        def canonical(value: object) -> str:
            symbol = str(value or "").strip().upper()
            for old, new in _SUFFIX_ALIASES.items():
                if symbol.endswith(old):
                    return f"{symbol[:-len(old)]}{new}"
            return symbol

        df = df.with_columns(
            pl.col("symbol").map_elements(canonical, return_dtype=pl.Utf8).alias("symbol")
        )
        suffix = pl.col("symbol").str.slice(-3)
        return df.filter(suffix.is_in(sorted(_VALID_SUFFIXES)))

    def _asset_type_map(self, symbols: list[str] | None = None) -> dict[str, str]:
        """构建 symbol → asset_type(CS/ETF/INDX) 映射。bars 不一定带 asset_type 列时用。"""
        try:
            inst = bridge.get_store().get_instruments(symbols=symbols or None)
        except Exception as e:
            logger.warning("quantx get_instruments (asset_type map) 失败: %s", e)
            return {}
        if inst.is_empty() or "symbol" not in inst.columns:
            return {}
        at_col = inst["asset_type"] if "asset_type" in inst.columns else None
        if at_col is None:
            return {}
        return {
            str(s): str(a or "")
            for s, a in zip(inst["symbol"].to_list(), at_col.to_list(), strict=True)
        }

    @staticmethod
    def _audit_bar_units(df: pl.DataFrame) -> None:
        """单位审计: implied_ratio = amount / (close * volume) 中位数应接近 100。

        volume=手、amount=元时, 每手 100 股, 故 amount/(close*volume)≈100。
        超出 [10, 1000] 视为「股/手混用」, 抛错拒绝写盘 (§16.6)。
        """
        if df.is_empty():
            return
        needed = ("amount", "close", "volume")
        if not all(c in df.columns for c in needed):
            return
        audit = df.with_columns(
            (pl.col("amount") / (pl.col("close") * pl.col("volume"))).alias("_ratio")
        ).filter(
            (pl.col("close") > 0)
            & (pl.col("volume") > 0)
            & (pl.col("amount") > 0)
            & pl.col("_ratio").is_finite()
        )
        if audit.is_empty():
            return
        med = audit.select(pl.col("_ratio").median()).item()
        if not (10.0 <= med <= 1000.0):
            raise ValueError(
                f"QuantX volume/amount 单位审计异常: implied_ratio 中位数={med:.2f} "
                f"(预期≈100, 合理区间 [10, 1000]), 可能股/手混用, 拒绝写盘"
            )

    @classmethod
    def _finalize_daily(cls, df: pl.DataFrame) -> pl.DataFrame:
        """公共收尾: 类型、symbol、数值关系、唯一性和 schema 校验。"""
        schema = {
            c: (pl.Date if c == "date" else pl.Utf8 if c == "symbol" else pl.Float64)
            for c in _DAILY_COLS
        }
        if df.is_empty():
            return pl.DataFrame(schema=schema)
        missing = [c for c in _DAILY_COLS if c not in df.columns]
        if missing:
            raise ValueError(f"QuantX 日K缺少必填列: {missing}")

        df = df.select(_DAILY_COLS).with_columns(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("date").cast(pl.Date, strict=False),
            *[
                pl.col(col).cast(pl.Float64, strict=False)
                for col in ("open", "high", "low", "close", "volume", "amount")
            ],
        )
        df = cls._validate_symbols(df).drop_nulls(_DAILY_COLS)
        # 过滤停牌行 (open=0 && high=0), 再校验剩余行情。
        df = df.filter(~((pl.col("open") == 0) & (pl.col("high") == 0)))
        invalid = df.filter(
            (pl.col("open") < 0)
            | (pl.col("high") < 0)
            | (pl.col("low") < 0)
            | (pl.col("close") < 0)
            | (pl.col("volume") < 0)
            | (pl.col("amount") < 0)
            | (pl.col("high") + 1e-9 < pl.max_horizontal("open", "low", "close"))
            | (pl.col("low") - 1e-9 > pl.min_horizontal("open", "high", "close"))
        )
        if not invalid.is_empty():
            sample = invalid.select("symbol", "date").head(5).to_dicts()
            raise ValueError(f"QuantX 日K数值关系异常: {invalid.height} 行, sample={sample}")
        return df.unique(subset=["symbol", "date"], keep="last").sort(["date", "symbol"])

    # ================================================================
    # get_daily — 不复权 OHLCV
    # ================================================================

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """获取不复权日K。asset_type: stock / etf / index。

        stock/etf 走 get_bars(adjust=""), 指数走 get_index_bars()。
        强制 adjust="" : 复权由项目 pipeline 统一完成, 避免二次复权。
        """
        store = bridge.get_store()
        start_str = _yyyymmdd(start_time.date() if isinstance(start_time, datetime) else start_time)
        end_str = _yyyymmdd(end_time.date() if isinstance(end_time, datetime) else end_time)

        if asset_type == "index":
            df = self._get_index_daily(store, symbols, start_str, end_str)
            if on_chunk_done:
                on_chunk_done(1, 1)
            return df

        # stock / etf: get_bars(adjust="") + asset_type 过滤
        raw = store.get_bars(
            symbols=symbols or None,
            start_date=start_str,
            end_date=end_str,
            adjust="",
        )
        if raw.is_empty():
            return pl.DataFrame()
        raw = self._normalize_bars_for_asset(raw, store, asset_type, symbols)
        df = self._finalize_daily(raw)
        if start_time is not None:
            start_date = start_time.date() if isinstance(start_time, datetime) else start_time
            df = df.filter(pl.col("date") >= start_date)
        if end_time is not None:
            end_date = end_time.date() if isinstance(end_time, datetime) else end_time
            df = df.filter(pl.col("date") <= end_date)
        self._audit_bar_units(df)
        if on_chunk_done:
            on_chunk_done(1, 1)
        return df

    def _get_index_daily(self, store, symbols, start_str, end_str) -> pl.DataFrame:
        """指数日K走 get_index_bars (不复权, 指数无除权概念)。"""
        try:
            raw = store.get_index_bars(
                symbols=symbols or None,
                start_date=start_str,
                end_date=end_str,
            )
        except Exception as e:
            logger.warning("quantx get_index_bars 失败: %s", e)
            return pl.DataFrame()
        if raw.is_empty():
            return pl.DataFrame()
        df = self._finalize_daily(raw)
        if start_str:
            df = df.filter(pl.col("date") >= date.fromisoformat(start_str))
        if end_str:
            df = df.filter(pl.col("date") <= date.fromisoformat(end_str))
        return df

    def _normalize_bars_for_asset(
        self, raw: pl.DataFrame, store, asset_type: str, requested_symbols: list[str] | None
    ) -> pl.DataFrame:
        """按 asset_type 过滤 bars。

        bars 若带 asset_type 列直接过滤; 否则用 instruments 构建映射。
        """
        target = _ASSET_TYPE_MAP.get(asset_type)
        if target is None:
            return raw  # 未知 asset_type 不过滤
        # 1) bars 自带 asset_type 列
        if "asset_type" in raw.columns:
            mask = pl.col("asset_type").cast(pl.Utf8).is_in(list(target))
            return raw.filter(mask)
        # 2) 无列 → 用 instruments 映射
        at_map = self._asset_type_map(requested_symbols)
        if not at_map:
            # 映射拿不到 (instruments 也空), 无法可靠分流, 返回空避免污染
            logger.warning("quantx: 无法确定 asset_type (instruments 为空), %s 返回空", asset_type)
            return raw.head(0)
        wanted = {s for s, a in at_map.items() if a in target}
        return raw.filter(pl.col("symbol").is_in(list(wanted)))

    # ================================================================
    # get_realtime — 多源实时快照
    # ================================================================

    def get_realtime(self) -> list[dict]:
        """从 QuantX HttpQuoteChain 拉实时行情 → 标准化 records。

        从 instruments 提取 CS/ETF/INDX symbol 集合, 调 HttpQuoteChain。
        无效记录过滤 (§6.6): symbol 空 / last_price<=0 / 非标的 / 价格非有限值。
        衍生字段回算: change_amount/change_pct/amplitude。
        """
        chain = bridge.get_http_quote_chain()
        if chain is None:
            return []
        symbols = self._collect_universe_symbols()
        if not symbols:
            return []
        t0 = __import__("time").perf_counter()
        try:
            quotes, source = chain.get_realtime_quotes(symbols)
        except Exception as e:
            logger.warning("quantx HttpQuoteChain 拉取失败: %s", e)
            return []
        valid_set = set(symbols)
        records: list[dict] = []
        for sym, q in quotes.items():
            if not q:
                continue
            rec = self._build_realtime_record(sym, q, source, valid_set)
            if rec is not None:
                records.append(rec)
        logger.info(
            "quantx realtime: %d/%d (源=%s, %.2fs)",
            len(records), len(symbols), source, __import__("time").perf_counter() - t0,
        )
        return records

    def _collect_universe_symbols(self) -> list[str]:
        """从 instruments 收集 CS/ETF/INDX symbol 集合。"""
        try:
            inst = bridge.get_store().get_instruments()
        except Exception as e:
            logger.warning("quantx get_instruments (realtime universe) 失败: %s", e)
            return []
        if inst.is_empty() or "symbol" not in inst.columns:
            return []
        at_col = inst["asset_type"] if "asset_type" in inst.columns else None
        if at_col is not None:
            inst = inst.filter(pl.col("asset_type").cast(pl.Utf8).is_in(["CS", "ETF", "INDX"]))
        return [str(s) for s in inst["symbol"].to_list()]

    @staticmethod
    def _build_realtime_record(
        sym: str, q: dict, source: str, valid_set: set[str]
    ) -> dict | None:
        """把 QuantX quote dict 标准化为 record, 无效返回 None。"""
        if not sym or sym not in valid_set:
            return None

        def _f(key, default=0.0) -> float:
            v = q.get(key, default)
            try:
                parsed = float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default
            return parsed if math.isfinite(parsed) else default

        last_price = _f("last")
        if last_price <= 0:
            return None
        prev_close = _f("prev_close")
        open_ = _f("open")
        high = _f("high")
        low = _f("low")
        volume = _f("volume")
        amount = _f("amount")
        name = q.get("name", "") or ""

        change_amount = last_price - prev_close if prev_close > 0 else 0.0
        change_pct = (change_amount / prev_close) if prev_close > 0 else 0.0
        amplitude = ((high - low) / prev_close) if prev_close > 0 else 0.0

        return {
            "symbol": sym,
            "name": name,
            "last_price": last_price,
            "prev_close": prev_close,
            "open": open_,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "change_pct": change_pct,
            "change_amount": change_amount,
            "amplitude": amplitude,
            "turnover_rate": None,
            "_source": f"quantx:{source}",
        }

    # ================================================================
    # get_instruments — 标的维表
    # ================================================================

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        """返回兼容 instrument_sync._flatten_instruments 的行。

        asset_type: stock → CS, etf → ETF, index → INDX。
        QuantX 不存在的字段返回 None, 由维表合并保留 TickFlow 值。
        """
        at_filter = {
            "stock": "CS",
            "etf": "ETF",
            "index": "INDX",
        }.get(asset_type)
        try:
            inst = bridge.get_store().get_instruments()
        except Exception as e:
            logger.warning("quantx get_instruments 失败: %s", e)
            return []
        if inst.is_empty() or "symbol" not in inst.columns:
            return []
        if at_filter and "asset_type" in inst.columns:
            inst = inst.filter(pl.col("asset_type").cast(pl.Utf8) == at_filter)
        rows: list[dict] = []
        for r in inst.to_dicts():
            sym = str(r.get("symbol") or "")
            if not sym:
                continue
            code, _, exchange = sym.partition(".")
            rows.append({
                "symbol": sym,
                "name": r.get("name"),
                "code": code,
                "exchange": exchange or None,
                "region": "CN",
                "type": asset_type,
                "ext": {
                    "listing_date": r.get("listing_date") or r.get("list_date"),
                    "total_shares": None,
                    "float_shares": None,
                    "limit_up": None,
                    "limit_down": None,
                },
            })
        return rows

    # ================================================================
    # test_dataset — 设置页试拉
    # ================================================================

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        symbols = symbols or ["600519.SH"]
        if dataset == "daily":
            df = self.get_daily(symbols, None, None)
            return _preview("daily", df)
        if dataset == "realtime":
            rows = self.get_realtime()
            head = rows[:5]
            return {
                "provider": self.name,
                "dataset": "realtime",
                "rows": len(rows),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        if dataset == "instruments":
            rows = self.get_instruments("stock")
            head = rows[:5]
            return {
                "provider": self.name,
                "dataset": "instruments",
                "rows": len(rows),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        raise ValueError(f"quantx 不支持数据集: {dataset}")


def _preview(dataset: str, df: pl.DataFrame) -> dict:
    return {
        "provider": "quantx",
        "dataset": dataset,
        "rows": df.height,
        "columns": df.columns,
        "preview": df.head(5).to_dicts() if not df.is_empty() else [],
    }
