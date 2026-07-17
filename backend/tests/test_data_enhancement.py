"""Repository merge_daily_asset + instruments coalesce 测试 (§19.2 / §19.4)。

用临时 data_dir 真实跑 KlineRepository, 不依赖 TickFlow SDK。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """临时 data_dir 的 KlineRepository。"""
    monkeypatch.setattr("app.config.settings.data_dir", tmp_path)
    store = DataStore()
    return KlineRepository(store)


def _bars(symbol, dt, close):
    return pl.DataFrame([{
        "symbol": symbol, "date": date.fromisoformat(dt),
        "open": close, "high": close, "low": close, "close": close,
        "volume": 100.0, "amount": close * 100 * 100.0,
    }])


# ================================================================
# merge_daily_asset (§19.2)
# ================================================================

def test_keep_existing_adds_missing_symbols_to_existing_date(repo):
    # 已有日期只有 600519.SH
    repo.merge_daily_asset("stock", _bars("600519.SH", "2025-07-10", 1500.0), conflict="replace")
    # QuantX 返回三只 (含已有的 + 两只缺失)
    incoming = pl.concat([
        _bars("600519.SH", "2025-07-10", 9999.0),  # 冲突, 应被保留原值
        _bars("000001.SZ", "2025-07-10", 15.0),
        _bars("000002.SZ", "2025-07-10", 10.0),
    ])
    result = repo.merge_daily_asset("stock", incoming, conflict="keep_existing")
    assert result.inserted_rows == 2
    assert result.inserted_symbols == frozenset({"000001.SZ", "000002.SZ"})
    # 最终三只, 直接读分区
    part = Path(repo.store.data_dir) / "kline_daily" / "date=2025-07-10" / "part.parquet"
    merged = pl.read_parquet(part)
    assert sorted(merged["symbol"].to_list()) == ["000001.SZ", "000002.SZ", "600519.SH"]
    # 600519.SH 保留 TickFlow 原值 (不被 9999 覆盖)
    assert merged.filter(pl.col("symbol") == "600519.SH")["close"][0] == 1500.0


def test_keep_existing_preserves_conflicting_key(repo):
    repo.merge_daily_asset("stock", _bars("600519.SH", "2025-07-10", 1500.0), conflict="replace")
    incoming = _bars("600519.SH", "2025-07-10", 1600.0)
    result = repo.merge_daily_asset("stock", incoming, conflict="keep_existing")
    assert result.inserted_rows == 0  # 已有 key, 不插入
    part = Path(repo.store.data_dir) / "kline_daily" / "date=2025-07-10" / "part.parquet"
    assert pl.read_parquet(part)["close"][0] == 1500.0  # 保留原值


def test_replace_overwrites_existing_key(repo):
    repo.merge_daily_asset("stock", _bars("600519.SH", "2025-07-10", 1500.0), conflict="replace")
    incoming = _bars("600519.SH", "2025-07-10", 1600.0)
    result = repo.merge_daily_asset("stock", incoming, conflict="replace")
    assert result.replaced_rows >= 1
    part = Path(repo.store.data_dir) / "kline_daily" / "date=2025-07-10" / "part.parquet"
    assert pl.read_parquet(part)["close"][0] == 1600.0


def test_new_date_creates_partition(repo):
    incoming = _bars("000001.SZ", "2025-07-11", 15.0)
    result = repo.merge_daily_asset("stock", incoming, conflict="keep_existing")
    assert result.inserted_rows == 1
    assert date(2025, 7, 11) in result.inserted_dates
    part = Path(repo.store.data_dir) / "kline_daily" / "date=2025-07-11" / "part.parquet"
    assert part.exists()


def test_no_change_does_not_rewrite_mtime(repo):
    repo.merge_daily_asset("stock", _bars("600519.SH", "2025-07-10", 1500.0), conflict="replace")
    part = Path(repo.store.data_dir) / "kline_daily" / "date=2025-07-10" / "part.parquet"
    mtime1 = part.stat().st_mtime
    import time as _t
    _t.sleep(0.05)
    # 再传一个已有 key (keep_existing), 应不改 mtime
    result = repo.merge_daily_asset("stock", _bars("600519.SH", "2025-07-10", 9999.0), conflict="keep_existing")
    assert result.inserted_rows == 0
    assert part.stat().st_mtime == mtime1


def test_merge_result_counts(repo):
    # 两个新日期各一只 + 一个已有日期补两只
    incoming = pl.concat([
        _bars("600519.SH", "2025-07-09", 1500.0),
        _bars("600519.SH", "2025-07-10", 1510.0),
    ])
    repo.merge_daily_asset("stock", incoming, conflict="replace")
    second = pl.concat([
        _bars("600519.SH", "2025-07-10", 9999.0),  # 已有, 保留
        _bars("000001.SZ", "2025-07-10", 15.0),    # 新 symbol, 已有日期
    ])
    result = repo.merge_daily_asset("stock", second, conflict="keep_existing")
    assert result.input_rows == 2
    assert result.inserted_rows == 1
    assert "000001.SZ" in result.inserted_symbols
    assert date(2025, 7, 10) in result.existing_dates_changed
    assert result.inserted_dates == frozenset()  # 无全新分区


def test_duplicate_incoming_keys_are_written_once(repo):
    incoming = pl.concat([
        _bars("000001.SZ", "2025-07-11", 15.0),
        _bars("000001.SZ", "2025-07-11", 16.0),
    ])
    result = repo.merge_daily_asset("stock", incoming, conflict="keep_existing")
    part = Path(repo.store.data_dir) / "kline_daily" / "date=2025-07-11" / "part.parquet"
    stored = pl.read_parquet(part)
    assert stored.height == 1
    assert stored["close"][0] == 16.0
    assert result.inserted_rows == 1


def test_etf_and_index_asset_dirs(repo):
    repo.merge_daily_asset("etf", _bars("510300.SH", "2025-07-10", 4.0), conflict="keep_existing")
    repo.merge_daily_asset("index", _bars("000001.SH", "2025-07-10", 3400.0), conflict="keep_existing")
    assert (Path(repo.store.data_dir) / "kline_etf_daily" / "date=2025-07-10" / "part.parquet").exists()
    assert (Path(repo.store.data_dir) / "kline_index_daily" / "date=2025-07-10" / "part.parquet").exists()


# ================================================================
# instruments coalesce (§19.4)
# ================================================================

def test_instruments_merge_keeps_tickflow_fields_and_appends_new():
    import sys
    import types
    # instrument_sync 依赖 tickflow client; stub 它
    if "tickflow" not in sys.modules:
        m = types.ModuleType("tickflow")
        m.AsyncTickFlow = type("A", (), {})
        m.TickFlow = type("T", (), {})
        sys.modules["tickflow"] = m
    from app.services.instrument_sync import merge_instrument_frames

    primary = pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"],
        "name": ["贵州茅台", "平安银行"],
        "total_shares": [1.2e9, None],
        "limit_up": [1661.0, 16.5],
        "limit_down": [1359.0, None],
    })
    enh = pl.DataFrame({
        "symbol": ["600519.SH", "430047.BJ"],
        "name": ["X-MOUTAI", "诺思兰德"],
        "exchange": ["SH", "BJ"],
    })
    merged = merge_instrument_frames(primary, enh)
    by_sym = {r["symbol"]: r for r in merged.to_dicts()}

    # TickFlow 股本保留
    assert by_sym["600519.SH"]["total_shares"] == 1.2e9
    # TickFlow 涨跌停价保留
    assert by_sym["600519.SH"]["limit_up"] == 1661.0
    assert by_sym["600519.SH"]["limit_down"] == 1359.0
    # 空 exchange 被 QuantX 补上
    assert by_sym["600519.SH"]["exchange"] == "SH"
    # TickFlow name 优先 (不被 X-MOUTAI 覆盖)
    assert by_sym["600519.SH"]["name"] == "贵州茅台"
    # QuantX 新 symbol 被追加
    assert "430047.BJ" in by_sym
    assert by_sym["430047.BJ"]["exchange"] == "BJ"


# ================================================================
# 复权契约 (§19.3): raw 不复权进入, pipeline 只复权一次, 无二次复权
# ================================================================

def test_enriched_uses_raw_unadjusted_and_single_adjustment():
    """构造一个发生除权的股票, 验证:
      - enriched.raw_close == 输入不复权 close (不被复权覆盖)
      - enriched.close 为单次前复权结果 (latest 不变, 历史被向下调整)
      - 不存在二次复权 (raw_close 始终是原始价)
    """
    import sys
    import types
    if "tickflow" not in sys.modules:
        m = types.ModuleType("tickflow")
        m.AsyncTickFlow = type("A", (), {})
        m.TickFlow = type("T", (), {})
        sys.modules["tickflow"] = m
    from app.indicators.pipeline import compute_enriched

    raw = pl.DataFrame([
        {"symbol": "600519.SH", "date": date(2025, 7, 9), "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0, "amount": 1e8},
        {"symbol": "600519.SH", "date": date(2025, 7, 10), "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.0, "volume": 10000.0, "amount": 1e8},
    ])
    # 10:1 拆股: ex_factor(pre/post) = 10
    factors = pl.DataFrame([
        {"symbol": "600519.SH", "trade_date": date(2025, 7, 10), "ex_factor": 10.0},
    ])
    enriched = compute_enriched(raw, factors=factors, instruments=None)
    by_date = {row["date"]: row for row in enriched.to_dicts()}

    # 1) raw_close 保留不复权原始价 (不被二次复权)
    assert by_date[date(2025, 7, 9)]["raw_close"] == 100.0
    assert by_date[date(2025, 7, 10)]["raw_close"] == 10.0
    # 2) 最新日 close == raw (前复权保持最新不变)
    assert by_date[date(2025, 7, 10)]["close"] == 10.0
    # 3) 历史日 close 被单次前复权 (向下调整, 不再等于 raw 100)
    adj_hist = by_date[date(2025, 7, 9)]["close"]
    assert adj_hist < 100.0, f"历史日应被前复权向下调整, 实际={adj_hist}"
    # 4) 无二次复权: 调整后历史日 close 应为 100/10=10 (单次拆股)
    assert abs(adj_hist - 10.0) < 1e-6, f"单次前复权后历史 close 应=10, 实际={adj_hist}"


def test_run_enhancement_splits_long_ranges(repo, monkeypatch):
    from app.services import data_enhancement as service

    class Provider:
        name = "fake"

        def __init__(self):
            self.calls = []

        def get_instruments(self, asset_type):
            return [{"symbol": "000001.SZ", "name": "A", "type": asset_type}]

        def get_daily(self, symbols, start_time, end_time, asset_type):
            self.calls.append((start_time.date(), end_time.date()))
            return _bars("000001.SZ", end_time.date().isoformat(), 15.0)

    provider = Provider()
    monkeypatch.setattr(
        service,
        "_resolve_provider",
        lambda name: (provider, {"asset_types": ["stock"]}),
    )
    summary = service.run_enhancement(
        repo,
        service.EnhancementRequest(
            provider="fake",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 7, 1),
            asset_types=("stock",),
        ),
        skip_enriched_recompute=True,
    )
    assert len(provider.calls) == 2
    assert all((end - start).days < 180 for start, end in provider.calls)
    assert summary.inserted_rows == 2


def test_run_enhancement_propagates_provider_failure(repo, monkeypatch):
    from app.services import data_enhancement as service

    class Provider:
        name = "fake"

        def get_instruments(self, asset_type):
            return [{"symbol": "000001.SZ", "name": "A", "type": asset_type}]

        def get_daily(self, symbols, start_time, end_time, asset_type):
            raise ValueError("source broken")

    monkeypatch.setattr(
        service,
        "_resolve_provider",
        lambda name: (Provider(), {"asset_types": ["stock"]}),
    )
    with pytest.raises(RuntimeError, match="source broken"):
        service.run_enhancement(
            repo,
            service.EnhancementRequest(
                provider="fake",
                start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 2),
                asset_types=("stock",),
            ),
            skip_enriched_recompute=True,
        )
