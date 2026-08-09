"""Grid state machine: placement, fills, pairing, PnL, accounting identity."""

import random

import pytest

from gridbot.engine import GridEngine
from gridbot.tests.conftest import make_config


def started_engine(config, p0=105.0):
    eng = GridEngine(config)
    eng.start("t0", p0)
    return eng


class TestInitialPlacement:
    def test_orders_around_p0(self, config):
        eng = started_engine(config, 105.0)
        buys = sorted(o.price for o in eng.orders.values() if o.side == "BUY")
        sells = sorted(o.price for o in eng.orders.values()
                       if o.side == "SELL")
        assert buys == [100.0, 101.0, 102.0, 103.0, 104.0]
        assert sells == [106.0, 107.0, 108.0, 109.0, 110.0]
        # level exactly at P0 (105) is skipped
        assert 105.0 not in buys + sells

    def test_initial_base_purchase_covers_sells(self, config):
        eng = GridEngine(config)
        info = eng.start("t0", 105.0)
        sell_qty = sum(o.qty for o in eng.orders.values()
                       if o.side == "SELL")
        assert info["initial_base"] == pytest.approx(sell_qty)
        assert eng.base == pytest.approx(sell_qty)
        # cash accounting of the market buy, fee modelled
        cost = sell_qty * 105.0
        fee = cost * config.fee_rate
        assert info["cost"] == pytest.approx(cost)
        assert eng.cash == pytest.approx(config.quote_budget - cost - fee)
        assert eng.fees_paid == pytest.approx(fee)

    def test_p0_above_range_means_all_buys_no_purchase(self, config):
        eng = GridEngine(config)
        info = eng.start("t0", 120.0)
        assert info["initial_base"] == 0.0
        assert eng.base == 0.0
        assert all(o.side == "BUY" for o in eng.orders.values())
        assert len(eng.orders) == 11

    def test_start_twice_raises(self, config):
        eng = started_engine(config)
        with pytest.raises(RuntimeError):
            eng.start("t1", 105.0)


class TestFillMechanics:
    def test_buy_fill_places_sell_one_level_up(self, config):
        eng = started_engine(config, 105.0)
        fills = eng.on_price("t1", high=104.5, low=104.0, close=104.2)
        assert len(fills) == 1
        f = fills[0]
        assert (f.side, f.price) == ("BUY", 104.0)
        # paired sell appears at 105 with same qty and basis 104
        sell = [o for o in eng.orders.values()
                if o.side == "SELL" and o.price == 105.0]
        assert len(sell) == 1
        assert sell[0].qty == pytest.approx(f.qty)
        assert sell[0].basis == pytest.approx(104.0)

    def test_sell_fill_places_buy_one_level_down(self, config):
        eng = started_engine(config, 105.0)
        fills = eng.on_price("t1", high=106.0, low=105.5, close=105.8)
        assert [(f.side, f.price) for f in fills] == [("SELL", 106.0)]
        buy = [o for o in eng.orders.values()
               if o.side == "BUY" and o.price == 105.0]
        assert len(buy) == 1
        assert buy[0].qty == pytest.approx(eng.level_qtys[5])

    def test_full_round_trip_realized_pnl_exact(self, config):
        eng = started_engine(config, 105.0)
        [buy] = eng.on_price("t1", high=104.5, low=104.0, close=104.2)
        q = buy.qty
        [sell] = eng.on_price("t2", high=105.0, low=104.8, close=105.0)
        assert (sell.side, sell.price) == ("SELL", 105.0)
        # realized PnL = level_gap * qty - buy fee - sell fee, EXACTLY
        gap = 105.0 - 104.0
        expected = (gap * q
                    - 104.0 * q * config.fee_rate
                    - 105.0 * q * config.fee_rate)
        assert sell.realized_net == pytest.approx(expected, abs=1e-12)
        assert eng.realized_pnl == pytest.approx(expected, abs=1e-12)
        assert eng.n_round_trips == 1
        assert eng.n_trades == 2

    def test_lowest_buys_and_highest_sells_first(self, config):
        eng = started_engine(config, 105.0)
        fills = eng.on_price("t1", high=108.0, low=102.0, close=105.0)
        buy_prices = [f.price for f in fills if f.side == "BUY"]
        sell_prices = [f.price for f in fills if f.side == "SELL"]
        assert buy_prices == sorted(buy_prices)            # lowest first
        assert sell_prices == sorted(sell_prices, reverse=True)  # highest 1st


class TestConservativeSameCandleRule:
    def test_sell_created_this_candle_does_not_fill_this_candle(self, config):
        eng = started_engine(config, 105.0)
        # one candle spans 104..106: buy@104 and initial sell@106 fill,
        # but the NEW sell@105 (spawned by the buy) must survive the candle
        fills = eng.on_price("t1", high=106.0, low=104.0, close=105.0)
        assert sorted((f.side, f.price) for f in fills) == [
            ("BUY", 104.0), ("SELL", 106.0)]
        open_new_sell = [o for o in eng.orders.values()
                         if o.side == "SELL" and o.price == 105.0]
        open_new_buy = [o for o in eng.orders.values()
                        if o.side == "BUY" and o.price == 105.0]
        assert len(open_new_sell) == 1   # not filled despite high >= 105
        assert len(open_new_buy) == 1    # not filled despite low <= 105

        # ...and it DOES fill on the next candle
        fills2 = eng.on_price("t2", high=106.0, low=104.5, close=105.0)
        assert ("SELL", 105.0) in [(f.side, f.price) for f in fills2]


class TestOutOfRange:
    def test_above_range(self, config):
        eng = started_engine(config, 105.0)
        eng.on_price("t1", high=120.0, low=119.0, close=120.0)
        assert eng.status() == "out_of_range_above"
        # nothing left to fill while price stays above the ladder
        assert eng.on_price("t2", high=121.0, low=119.5, close=120.5) == []

    def test_below_range(self, config):
        eng = started_engine(config, 105.0)
        eng.on_price("t1", high=91.0, low=90.0, close=90.0)
        assert eng.status() == "out_of_range_below"
        assert eng.on_price("t2", high=90.5, low=89.0, close=89.5) == []
        # engine holds accumulated base; orders simply stopped filling
        assert eng.base > 0

    def test_back_in_range(self, config):
        eng = started_engine(config, 105.0)
        eng.on_price("t1", high=91.0, low=90.0, close=90.0)
        eng.on_price("t2", high=105.0, low=90.0, close=105.0)
        assert eng.status() == "in_range"


class TestAccountingIdentity:
    def test_identity_holds_on_every_step(self, config):
        """cash + base*close == budget + realized_gross + unrealized - fees,
        at EVERY step of a seeded synthetic path (in and out of range)."""
        eng = started_engine(config, 105.0)
        rng = random.Random(42)
        close = 105.0
        for t in range(600):
            close = min(max(close + rng.uniform(-1.5, 1.5), 95.0), 115.0)
            hi = close + rng.uniform(0.0, 1.0)
            lo = close - rng.uniform(0.0, 1.0)
            eng.on_price(t, high=hi, low=lo, close=close)
            s = eng.snapshot(close)
            lhs = s["cash"] + s["base"] * close
            rhs = (config.quote_budget + s["realized_pnl_gross"]
                   + s["unrealized_pnl"] - s["fees_paid"])
            assert lhs == pytest.approx(rhs, abs=1e-6), f"step {t}"
            assert s["cash"] >= -1e-9  # sizing guarantees no negative cash
            assert s["base"] >= -1e-9

    def test_equity_snapshot_fields(self, config):
        eng = started_engine(config, 105.0)
        s = eng.snapshot(105.0)
        assert s["equity"] == pytest.approx(eng.cash + eng.base * 105.0)
        assert s["status"] == "in_range"
        assert s["n_open_orders"] == len(s["open_orders"]) == 10
