from __future__ import annotations

import math

import pytest

from app.services.ext_presets import (
    _concept_preset,
    _flatten_industry_rows,
    _industry_preset,
    _unwrap_rows,
)


def test_builtin_presets_use_current_market_flow_exports():
    assert _concept_preset().pull.url.endswith("/exports/ths-concepts")
    assert _industry_preset().pull.url.endswith("/exports/ths-industries")


def test_unwrap_rows_supports_direct_and_enveloped_arrays():
    rows = [{"symbol": "600000.SH"}]
    assert _unwrap_rows(rows) is rows
    assert _unwrap_rows({"data": rows}) is rows
    assert _unwrap_rows({"payload": rows}) is rows


def test_unwrap_rows_reports_non_array_payload_preview():
    with pytest.raises(ValueError, match="type=dict") as exc:
        _unwrap_rows({"detail": "upstream task failed"})
    assert "upstream task failed" in str(exc.value)


def test_flatten_industry_rows_drops_empty_and_nan_labels():
    rows = _flatten_industry_rows([
        {
            "symbol": "600000.SH",
            "name": "浦发银行",
            "industries": ["金融", None, " ", math.nan, "银行"],
        }
    ])
    assert rows == [{
        "股票代码": "600000.SH",
        "股票简称": "浦发银行",
        "所属同花顺行业": "金融-银行",
        "symbol": "600000.SH",
        "code": "600000",
    }]
