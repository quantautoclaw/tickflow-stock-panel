from __future__ import annotations

from app.services import preferences
from app.services.quote_service import QuoteService


class _FakeQuotes:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def get(self, *, symbols: list[str]):
        self.calls.append(list(symbols))
        return [
            {
                "symbol": symbol,
                "name": symbol,
                "last_price": 101.0,
                "prev_close": 100.0,
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "volume": 1000.0,
                "amount": 101000.0,
                "ext": {},
            }
            for symbol in symbols
        ]


class _FakeClient:
    def __init__(self) -> None:
        self.quotes = _FakeQuotes()


def test_free_watchlist_fetches_core_indices_in_separate_batch(monkeypatch):
    """Free 模式不能因股票自选占满 5 个额度而丢失首页核心指数。"""
    stocks = [f"00000{i}.SZ" for i in range(1, 6)]
    indices = ["000001.SH", "399001.SZ", "399006.SZ", "000680.SH"]
    client = _FakeClient()

    monkeypatch.setattr(preferences, "get_realtime_watchlist_symbols", lambda: stocks)
    monkeypatch.setattr(preferences, "get_realtime_pull_index", lambda: True)
    monkeypatch.setattr(preferences, "get_realtime_index_symbols", lambda: indices)
    monkeypatch.setattr("app.tickflow.client.get_paid_realtime_client", lambda: client)

    service = QuoteService()
    service._fetch_watchlist_quotes()

    assert client.quotes.calls == [stocks, indices]
    assert service._symbol_count == 5
    assert service._index_symbol_count == 4

    index_quotes = service.get_index_quotes()
    assert index_quotes.height == 4
    # Quote API 是小数制 0.01，指数前端契约是百分比制 1.0。
    assert set(index_quotes["change_pct"].to_list()) == {1.0}


def test_stop_does_not_clear_persisted_realtime_preference(monkeypatch):
    service = QuoteService()
    saved: list[bool] = []
    monkeypatch.setattr(service, "_save_enabled", saved.append)

    service.stop()  # FastAPI shutdown/reload
    assert saved == []

    service.disable()  # 用户显式关闭
    assert saved == [False]


def test_free_watchlist_still_fetches_indices_without_stock_symbols(monkeypatch):
    indices = ["000001.SH", "399001.SZ"]
    client = _FakeClient()

    monkeypatch.setattr(preferences, "get_realtime_watchlist_symbols", lambda: [])
    monkeypatch.setattr(preferences, "get_realtime_pull_index", lambda: True)
    monkeypatch.setattr(preferences, "get_realtime_index_symbols", lambda: indices)
    monkeypatch.setattr("app.tickflow.client.get_paid_realtime_client", lambda: client)

    service = QuoteService()
    service._fetch_watchlist_quotes()

    assert client.quotes.calls == [indices]
    assert service.get_index_quotes().height == 2
