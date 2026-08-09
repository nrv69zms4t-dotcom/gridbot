"""In-memory Binance signed-client simulator — NO network, for tests.

Mimics the trading surface of ``BinanceSignedClient`` used by
``LiveTrader`` and the app API, plus test helpers to fill / partially
fill / externally cancel orders.  Semantics follow real Binance:

* ``clientOrderId`` must be unique among OPEN orders only (reuse after
  a terminal state is allowed, like on the real exchange);
* ``get_open_orders`` lists NEW and PARTIALLY_FILLED orders;
* ``get_order`` returns the latest known order for a clientOrderId;
* every state change bumps a monotonically increasing ``updateTime``.
"""

from __future__ import annotations

from gridbot.exchange import BookTicker, SymbolFilters, format_decimal
from gridbot.live import split_symbol


class FakeSignedClient:
    def __init__(self, symbol: str = "TESTUSDT", price: float = 105.0,
                 balances: dict[str, float] | None = None,
                 price_step: float = 0.01, qty_step: float = 0.001,
                 min_notional: float = 1.0):
        self.symbol = symbol
        self.price = price
        self.base_asset, self.quote_asset = split_symbol(symbol)
        self.balances: dict[str, float] = (
            dict(balances) if balances is not None
            else {self.quote_asset: 100_000.0})
        self._filters = SymbolFilters(symbol, price_step, qty_step,
                                      min_notional)
        self.open: dict[str, dict] = {}     # open orders by clientOrderId
        self.orders: dict[str, dict] = {}   # latest order per clientOrderId
        self.market_orders: list[dict] = []
        self._seq = 0
        self._auto_id = 0
        # failure injection
        self.fail_next_place = 0            # fail the next N place_limit
        self.fail_market = False

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------- read endpoints

    def get_book_ticker(self, symbol: str) -> BookTicker:
        return BookTicker(symbol, bid=self.price - 0.01,
                          ask=self.price + 0.01)

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        return self._filters

    def get_server_time(self) -> int:
        return 1_700_000_000_000

    def get_balances(self) -> dict[str, float]:
        return {a: v for a, v in self.balances.items() if v > 0}

    # ---------------------------------------------------- trading endpoints

    def place_limit(self, symbol: str, side: str, price: float, qty: float,
                    client_order_id: str | None = None,
                    price_step: float | None = None,
                    qty_step: float | None = None) -> dict:
        if self.fail_next_place > 0:
            self.fail_next_place -= 1
            raise RuntimeError("fake: place_limit rejected")
        if client_order_id is None:
            client_order_id = f"auto{self._auto_id}"
            self._auto_id += 1
        assert client_order_id not in self.open, (
            f"duplicate clientOrderId among open orders: {client_order_id}")
        od = {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "price": format_decimal(
                price, price_step or self._filters.price_step),
            "origQty": format_decimal(
                qty, qty_step or self._filters.qty_step),
            "executedQty": "0",
            "status": "NEW",
            "updateTime": self._next_seq(),
        }
        self.open[client_order_id] = od
        self.orders[client_order_id] = od
        return dict(od)

    def place_market_quote(self, symbol: str, side: str, quote_qty: float,
                           client_order_id: str | None = None) -> dict:
        if self.fail_market:
            raise RuntimeError("fake: market order rejected")
        qty = quote_qty / self.price
        sign = 1.0 if side == "BUY" else -1.0
        self.balances[self.quote_asset] = \
            self.balances.get(self.quote_asset, 0.0) - sign * quote_qty
        self.balances[self.base_asset] = \
            self.balances.get(self.base_asset, 0.0) + sign * qty
        od = {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "side": side,
            "type": "MARKET",
            "status": "FILLED",
            "executedQty": f"{qty:.8f}",
            "cummulativeQuoteQty": f"{quote_qty:.8f}",
            "updateTime": self._next_seq(),
        }
        self.market_orders.append(od)
        if client_order_id:
            self.orders[client_order_id] = od
        return dict(od)

    def cancel(self, symbol: str, orig_client_order_id: str) -> dict:
        od = self.open.pop(orig_client_order_id, None)
        if od is None:
            raise RuntimeError(
                f"fake: order {orig_client_order_id} is not open")
        od["status"] = "CANCELED"
        od["updateTime"] = self._next_seq()
        return dict(od)

    def get_open_orders(self, symbol: str) -> list:
        return [dict(o) for o in self.open.values()]

    def get_order(self, symbol: str, orig_client_order_id: str) -> dict:
        od = self.orders.get(orig_client_order_id)
        if od is None:
            raise RuntimeError(
                f"fake: unknown order {orig_client_order_id}")
        return dict(od)

    # --------------------------------------------------------- test helpers

    def fill_order(self, client_order_id: str) -> dict:
        """Fully fill one open order (helper for tests)."""
        od = self.open.pop(client_order_id)
        od["status"] = "FILLED"
        od["executedQty"] = od["origQty"]
        od["updateTime"] = self._next_seq()
        qty, px = float(od["origQty"]), float(od["price"])
        sign = 1.0 if od["side"] == "BUY" else -1.0
        self.balances[self.quote_asset] = \
            self.balances.get(self.quote_asset, 0.0) - sign * qty * px
        self.balances[self.base_asset] = \
            self.balances.get(self.base_asset, 0.0) + sign * qty
        return dict(od)

    def fill_at_price(self, price: float) -> list[str]:
        """Fill every open order a trade at ``price`` would cross."""
        self.price = price
        crossed = [
            coid for coid, od in sorted(self.open.items(),
                                        key=lambda kv: kv[1]["updateTime"])
            if (od["side"] == "BUY" and price <= float(od["price"]) + 1e-12)
            or (od["side"] == "SELL" and price >= float(od["price"]) - 1e-12)
        ]
        for coid in crossed:
            self.fill_order(coid)
        return crossed

    def partial_fill(self, client_order_id: str,
                     fraction: float = 0.5) -> None:
        od = self.open[client_order_id]
        od["status"] = "PARTIALLY_FILLED"
        od["executedQty"] = f"{float(od['origQty']) * fraction:.8f}"
        od["updateTime"] = self._next_seq()

    def cancel_external(self, client_order_id: str) -> dict:
        """Simulate the user cancelling an order on the exchange."""
        return self.cancel(self.symbol, client_order_id)

    def find_open(self, side: str, price: float) -> str:
        """clientOrderId of the open order at ``side``/``price``."""
        for coid, od in self.open.items():
            if od["side"] == side and \
                    abs(float(od["price"]) - price) < 1e-9:
                return coid
        raise AssertionError(f"no open {side} @ {price}")
