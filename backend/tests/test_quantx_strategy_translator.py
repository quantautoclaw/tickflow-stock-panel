"""quantx_strategy_translator 测试。

覆盖: 白名单因子正确映射、未知字段拒绝翻译、exclude_st 映射到 risk.rules、
未翻译的 basic_filter 字段产生 warning 而非静默丢弃、止损/持有期映射。
"""
from __future__ import annotations

from app.services.quantx_strategy_translator import (
    ALLOWED_SCORE_FACTORS,
    translate_strategy_to_quantx_config,
)


def test_rejects_strategy_without_scoring():
    result = translate_strategy_to_quantx_config(
        strategy_id="x", scoring={}, limit=50, basic_filter={},
        stop_loss=None, max_hold_days=None, start_date="2024-01-01", end_date="2024-12-31",
    )
    assert result.ok is False
    assert "scoring" in result.errors[0]


def test_rejects_strategy_with_unmapped_score_field():
    result = translate_strategy_to_quantx_config(
        strategy_id="x", scoring={"change_pct": 0.5, "momentum_60d": 0.5}, limit=50,
        basic_filter={}, stop_loss=None, max_hold_days=None,
        start_date="2024-01-01", end_date="2024-12-31",
    )
    assert result.ok is False
    assert "change_pct" in result.errors[0]
    assert result.config is None


def test_translates_known_factors_to_alpha_model():
    result = translate_strategy_to_quantx_config(
        strategy_id="trend_breakout",
        scoring={"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "rsi_14": 0.3},
        limit=20,
        basic_filter={"exclude_st": True, "price_min": 5, "market_cap_min": 2e9},
        stop_loss=-0.08,
        max_hold_days=20,
        start_date="2020-01-01",
        end_date="2026-01-01",
        initial_capital=500_000.0,
        benchmark="000905.SH",
    )
    assert result.ok is True
    cfg = result.config
    assert cfg["name"] == "tickflow_trend_breakout"
    assert cfg["universe"] == {"type": "pool", "pool_id": "a_share", "symbols": []}

    factor_aliases = {f["alias"] for f in cfg["alpha_model"]["factors"]}
    assert factor_aliases == {"momentum_60d", "vol_ratio_5d", "rsi_14"}
    momentum_factor = next(f for f in cfg["alpha_model"]["factors"] if f["alias"] == "momentum_60d")
    assert momentum_factor["name"] == "momentum"
    assert momentum_factor["params"] == {"window": 60}

    score_by_field = {t["field"]: t["weight"] for t in cfg["alpha_model"]["score_terms"]}
    assert score_by_field == {"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "rsi_14": 0.3}
    assert cfg["alpha_model"]["select"] == {"method": "top_n", "n": 20}

    # exclude_st 映射到 risk.rules; price_min/market_cap_min 未翻译, 只产生 warning
    assert {"type": "exclude_st"} in cfg["risk"]["rules"]
    assert any("price_min" in w and "market_cap_min" in w for w in result.warnings)

    # 止损/持有期映射
    assert cfg["exit_rules"]["max_loss_pct"] == 0.08
    assert cfg["exit_rules"]["time_limit_days"] == 20

    # 引擎固定 rqalpha_native + snapshot_id 空字符串 (自动物化快照)
    assert cfg["execution"]["backtest_engine"] == "rqalpha_native"
    assert cfg["backtest"]["snapshot_id"] == ""
    assert cfg["backtest"]["initial_cash"] == 500_000.0
    assert cfg["backtest"]["benchmark"] == "000905.SH"

    # 通用告知性 warning 始终存在
    assert any("filter()" in w for w in result.warnings)


def test_no_exit_rules_key_when_neither_stop_loss_nor_max_hold_days():
    result = translate_strategy_to_quantx_config(
        strategy_id="x", scoring={"rsi_14": 1.0}, limit=10, basic_filter={},
        stop_loss=None, max_hold_days=None, start_date="2024-01-01", end_date="2024-12-31",
    )
    assert result.ok is True
    assert "exit_rules" not in result.config


def test_max_position_pct_derived_from_limit():
    result = translate_strategy_to_quantx_config(
        strategy_id="x", scoring={"rsi_14": 1.0}, limit=5, basic_filter={},
        stop_loss=None, max_hold_days=None, start_date="2024-01-01", end_date="2024-12-31",
    )
    assert result.config["portfolio"]["max_position_pct"] == 0.2


def test_allowed_score_factors_all_have_name_and_params():
    for field_name, spec in ALLOWED_SCORE_FACTORS.items():
        assert "name" in spec and "params" in spec, field_name
