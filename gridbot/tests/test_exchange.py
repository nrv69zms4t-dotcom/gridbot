"""Exchange abstraction: live-trading stubs must refuse, offline defaults."""

import pytest

from gridbot.exchange import (BinancePublicClient, BinanceTradeClient,
                              BookTicker, default_filters)


class TestLiveTradingIntentionallyStubbed:
    def test_trade_client_place_order_raises(self):
        client = BinanceTradeClient()
        with pytest.raises(NotImplementedError,
                           match="intentionally not implemented"):
            client.place_order("BTCUSDT", "BUY", "LIMIT", 100.0, 1.0)

    def test_trade_client_cancel_order_raises(self):
        with pytest.raises(NotImplementedError,
                           match="verify on paper first"):
            BinanceTradeClient().cancel_order("BTCUSDT", 1)

    def test_public_client_cannot_trade_either(self):
        with pytest.raises(NotImplementedError):
            BinancePublicClient().place_order("BTCUSDT", "SELL", "LIMIT",
                                              100.0, 1.0)


class TestOfflineDefaults:
    def test_default_filters_sane(self):
        f = default_filters("BTCUSDT")
        assert f.symbol == "BTCUSDT"
        assert f.price_step > 0
        assert f.qty_step > 0
        assert f.min_notional >= 0

    def test_book_ticker_mid(self):
        t = BookTicker("X", bid=100.0, ask=102.0)
        assert t.mid == 101.0
