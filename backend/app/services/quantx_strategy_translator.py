"""TickFlow 策略 → QuantX StrategyConfig 翻译器 (严谨回测适配用)。

只翻译可声明式表达的子集: scoring(打分排名) + limit(选股数) +
stop_loss/max_hold_days(退出规则) + basic_filter.exclude_st。

明确不翻译 (拒绝或提示, 不做静默近似):
  - filter_fn / entry_signals / exit_signals 里的自定义技术形态判断
    (如 60 日新高、放量突破) —— QuantX FilterSpec 只支持简单 field/op/value
    比较, 无法表达任意布尔组合逻辑, 也无法翻译任意 Polars 表达式。
  - basic_filter 里的 price_min/max、market_cap_min、amount_min、
    exclude_new_days —— 未验证 QuantX FilterSpec.field 能否直接引用原始
    bars/daily_basic 字段(只验证了 alpha_model.factors 声明的因子别名可用),
    宁可跳过并提示, 也不猜测拼错配置。
  - scoring 中不在 ALLOWED_SCORE_FACTORS 白名单内的字段 —— 直接拒绝整个翻译。

因此严谨回测结果是 TickFlow 策略「打分排名」部分的复核, 不是逐条件精确
复刻; 调用方必须展示 TranslationResult.warnings, 不能把结果当作与 TickFlow
原策略等价的验证。

universe 固定用 QuantX 内置 "a_share" 股票池 (全A股, 剔除 ST/科创板/北交所,
见 QuantX configs/pools/a_share.json), 与 TickFlow 全市场口径存在已知差异。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# TickFlow scoring 字段 → QuantX 已验证的因子注册表映射 (factor name + params)。
# 来源: QuantX packages/quantx_factors/registry.py 人工核对 (2026-07)。
# 仅收录语义明确一致的因子; turnover_rate/change_pct/amplitude 等因两侧计算口径
# 存疑 (如 QuantX amplitude 是滚动窗口指标, TickFlow 是单日振幅) 而未收录。
ALLOWED_SCORE_FACTORS: dict[str, dict] = {
    "momentum_5d":  {"name": "momentum", "params": {"window": 5}},
    "momentum_10d": {"name": "momentum", "params": {"window": 10}},
    "momentum_20d": {"name": "momentum", "params": {"window": 20}},
    "momentum_30d": {"name": "momentum", "params": {"window": 30}},
    "momentum_60d": {"name": "momentum", "params": {"window": 60}},
    "rsi_6":  {"name": "rsi", "params": {"window": 6}},
    "rsi_14": {"name": "rsi", "params": {"window": 14}},
    "rsi_24": {"name": "rsi", "params": {"window": 24}},
    "vol_ratio_5d": {"name": "volume_ratio", "params": {"short_window": 5, "long_window": 20}},
    "atr_14": {"name": "atr", "params": {"window": 14}},
    "macd_hist": {"name": "macd_hist", "params": {"fast": 12, "slow": 26, "signal": 9}},
    "kdj_k": {"name": "kdj_k", "params": {"window": 9, "m1": 3}},
}

# QuantX configs/pools/a_share.json: All A-shares (excluding ST/STAR/BSE)
_UNIVERSE_POOL_ID = "a_share"

_UNTRANSLATED_FILTER_KEYS = ("price_min", "price_max", "market_cap_min", "amount_min", "exclude_new_days")


@dataclass
class TranslationResult:
    ok: bool
    config: dict | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def translate_strategy_to_quantx_config(
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
) -> TranslationResult:
    """翻译失败时 ok=False + errors 说明原因, 绝不返回可能误导的部分配置。"""
    errors: list[str] = []
    warnings: list[str] = []
    basic_filter = basic_filter or {}

    if not scoring:
        return TranslationResult(ok=False, errors=["策略未定义 scoring(打分权重), 无法翻译为 QuantX 排名策略"])

    unmapped = sorted(f for f in scoring if f not in ALLOWED_SCORE_FACTORS)
    if unmapped:
        return TranslationResult(ok=False, errors=[
            f"scoring 字段 {unmapped} 不在已验证的可翻译因子白名单内 "
            f"(支持: {sorted(ALLOWED_SCORE_FACTORS)}), 拒绝翻译以避免产出误导性结果"
        ])

    factors = []
    score_terms = []
    for f, weight in scoring.items():
        spec = ALLOWED_SCORE_FACTORS[f]
        factors.append({"name": spec["name"], "alias": f, "params": spec["params"]})
        score_terms.append({"field": f, "direction": "desc", "weight": float(weight)})

    risk_rules = []
    if basic_filter.get("exclude_st"):
        risk_rules.append({"type": "exclude_st"})

    skipped_filter_keys = [k for k in _UNTRANSLATED_FILTER_KEYS if basic_filter.get(k)]
    if skipped_filter_keys:
        warnings.append(
            f"basic_filter 中 {skipped_filter_keys} 未翻译(QuantX 侧字段映射未验证), "
            "严谨回测的候选池比原策略更宽松"
        )
    warnings.append(
        "仅翻译 scoring(打分排名) + 止损/持有期上限; 策略自定义技术形态过滤条件"
        "(均线/新高/信号判断等 filter() 逻辑)不参与严谨回测, 两侧结果不可直接等同比较"
    )

    n = max(1, int(limit) if limit else 1)
    exit_rules: dict = {}
    if stop_loss is not None:
        exit_rules["max_loss_pct"] = abs(float(stop_loss))
    if max_hold_days:
        exit_rules["time_limit_days"] = int(max_hold_days)

    config: dict = {
        "schema_version": "1.0",
        "name": f"tickflow_{strategy_id}",
        "asset_type": "stock",
        "universe": {"type": "pool", "pool_id": _UNIVERSE_POOL_ID, "symbols": []},
        "alpha_model": {
            "type": "top_n_rank",
            "factors": factors,
            "filters": [],
            "score_method": "linear_rank",
            "score_terms": score_terms,
            "select": {"method": "top_n", "n": n},
        },
        "portfolio": {
            "method": "equal_weight",
            "max_total_exposure": 0.95,
            "max_position_pct": min(1.0, round(1.0 / n, 4)),
            "cash_buffer": 0.05,
        },
        "risk": {"rules": risk_rules},
        "execution": {
            "rebalance_frequency": "weekly",
            "order_method": "order_target_percent",
            "backtest_engine": "rqalpha_native",
        },
        "backtest": {
            "snapshot_id": "",
            "start_date": start_date,
            "end_date": end_date,
            "benchmark": benchmark,
            "initial_cash": float(initial_capital),
            "commission": 0.0003,
            "slippage": 0.001,
            "stamp_duty_rate": 0.001,
        },
    }
    if exit_rules:
        config["exit_rules"] = exit_rules
    return TranslationResult(ok=True, config=config, errors=errors, warnings=warnings)
