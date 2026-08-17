"""tp-mode (buy-ladder + TP/RR) — app backend, live trader, CLI wiring.

NO network anywhere: injected offline clients only.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from gridbot.app.api import DEFAULT_CONFIG, Api
from gridbot.backtest import build_parser as backtest_parser
from gridbot.exchange import BookTicker
from gridbot.live import LiveTrader, build_parser as live_parser
from gridbot.paper import StaticTickerClient, build_parser as paper_parser
from gridbot.tests.conftest import make_config
from gridbot.tests.fake_signed import FakeSignedClient

CFG_TP = {
    "symbol": "TESTUSDT",
    "lower": 100.0,
    "upper": 110.0,
    "levels": 11,
    "spacing": "arithmetic",
    "budget": 1100.0,
    "fee": 0.001,
    "poll": 1.0,
    "logic": "tp",
    "rr": 3.0,
}

_logger_seq = 0


def make_test_logger() -> logging.Logger:
    global _logger_seq
    _logger_seq += 1
    logger = logging.getLogger(f"test.gridbot.tp.{_logger_seq}")
    logger.setLevel(logging.INFO)
    return logger


def price_client(mid: float) -> StaticTickerClient:
    return StaticTickerClient(
        [BookTicker("TESTUSDT", bid=mid - 0.01, ask=mid + 0.01)])


def wait_until(cond, timeout=8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def make_tp_trader(tmp_path, client=None) -> tuple[LiveTrader,
                                                   FakeSignedClient]:
    client = client or FakeSignedClient()
    trader = LiveTrader(make_config(logic="tp", tp_mult=3.0), client,
                        tmp_path / "live.json", make_test_logger(),
                        poll_seconds=0.0)
    return trader, client


def engine_oid(trader: LiveTrader, side: str, price: float) -> int:
    for oid, o in trader.engine.orders.items():
        if o.side == side and o.price == pytest.approx(price):
            return oid
    raise AssertionError(f"no open engine {side} @ {price}")


# ---------------------------------------------------------------------------
# app config: defaults, roundtrip, validation (Russian errors)
# ---------------------------------------------------------------------------

class TestTpAppConfig:
    def test_defaults_include_logic_and_rr(self, tmp_path):
        assert DEFAULT_CONFIG["logic"] == "classic"
        assert DEFAULT_CONFIG["rr"] == 3.0
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        c = api.get_defaults()["config"]
        assert c["logic"] == "classic" and c["rr"] == 3.0

    def test_roundtrip_tp_mode(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        assert api.start_paper(CFG_TP)["ok"] is True
        assert wait_until(lambda: api.get_status()["price"] is not None)
        assert api.stop_paper()["ok"] is True

        api2 = Api(client=price_client(105.0), data_dir=tmp_path)
        c = api2.get_defaults()["config"]
        assert c["logic"] == "tp"
        assert c["rr"] == 3.0
        assert c["symbol"] == "TESTUSDT"

    def test_old_config_without_new_keys_gets_defaults(self, tmp_path):
        old = {"symbol": "ETHUSDT", "lower": 1000.0, "upper": 2000.0,
               "levels": 15, "spacing": "geometric", "budget": 500.0,
               "fee": 0.001, "poll": 5.0}
        (tmp_path / "config.json").write_text(json.dumps(old),
                                              encoding="utf-8")
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        c = api.get_defaults()["config"]
        assert c["symbol"] == "ETHUSDT"          # old values kept
        assert c["logic"] == "classic"           # new keys defaulted
        assert c["rr"] == 3.0

    def test_rr_validation_russian(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        for bad in (0.2, 100.0, 0.0, -3.0, None, "abc"):
            r = api.start_paper(dict(CFG_TP, rr=bad))
            assert r["ok"] is False, bad
            assert "RR" in r["error"], bad
            assert "0.5" in r["error"] and "50" in r["error"], bad

    def test_bad_logic_value_russian(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        r = api.start_paper(dict(CFG_TP, logic="martingale"))
        assert r["ok"] is False
        assert "Логика" in r["error"]

    def test_classic_ignores_rr(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        r = api.start_paper(dict(CFG_TP, logic="classic", rr=999.0))
        assert r["ok"] is True
        try:
            assert wait_until(
                lambda: api.get_status()["price"] is not None)
            assert api.get_status()["logic"] == "classic"
        finally:
            api.stop_paper()


# ---------------------------------------------------------------------------
# get_status carries "logic"; tp start = pure cash, buys only
# ---------------------------------------------------------------------------

class TestTpStatus:
    def test_idle_status_logic_is_none(self, tmp_path):
        st = Api(client=price_client(105.0), data_dir=tmp_path).get_status()
        assert st["ok"] is True and st["logic"] is None

    def test_running_tp_status(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        assert api.start_paper(CFG_TP)["ok"] is True
        assert wait_until(lambda: api.get_status()["price"] is not None)
        try:
            st = api.get_status()
            assert st["logic"] == "tp"
            assert st["equity"] == pytest.approx(1100.0)  # no initial buy
            assert st["base"] == 0.0
            assert st["in_range"] is True
            assert len(st["open_orders"]) == 5            # buys below 105
            assert all(o["side"] == "BUY" for o in st["open_orders"])
            json.dumps(st)
        finally:
            api.stop_paper()


# ---------------------------------------------------------------------------
# compute_range with logic="tp": levels built DOWN from the price
# ---------------------------------------------------------------------------

class TestComputeRangeTp:
    def test_arithmetic_hand_computed(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 1.0, 10, "arithmetic", "tp")
        assert r["ok"] is True and r["logic"] == "tp"
        # price 105, step 1% -> step_abs 1.05; lower = 105 - 1.05*10
        assert r["price"] == 105.0
        assert r["step_abs"] == pytest.approx(1.05)
        assert r["upper"] == pytest.approx(105.0)
        assert r["lower"] == pytest.approx(94.5)
        json.dumps(r)

    def test_geometric_hand_computed(self, tmp_path):
        api = Api(client=price_client(100.0), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 1.0, 5, "geometric", "tp")
        assert r["ok"] is True
        # r = 1.01: lower = 100 / 1.01**5, upper = price
        assert r["upper"] == pytest.approx(100.0)
        assert r["lower"] == pytest.approx(100.0 / 1.01 ** 5, abs=5e-4)

    def test_lower_below_zero_rejected(self, tmp_path):
        # step 10% x 10 levels down from the price -> lower = 105 - 105 = 0
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 10.0, 10, "arithmetic", "tp")
        assert r["ok"] is False and "нижнюю" in r["error"]
        # geometric never crosses zero: the same inputs are fine
        r2 = api.compute_range("TESTUSDT", 10.0, 10, "geometric", "tp")
        assert r2["ok"] is True and r2["lower"] > 0

    def test_default_logic_param_stays_classic(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 1.0, 11, "arithmetic")
        assert r["ok"] is True and r["logic"] == "classic"
        assert r["lower"] == pytest.approx(99.75)   # symmetric, unchanged
        assert r["upper"] == pytest.approx(110.25)

    def test_bad_logic_value_russian(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 1.0, 11, "arithmetic", "x")
        assert r["ok"] is False and "Логика" in r["error"]


# ---------------------------------------------------------------------------
# live trader in tp mode: no market buy, TP sells, recovery
# ---------------------------------------------------------------------------

class TestLiveTp:
    def test_start_places_buys_only_no_market_order(self, tmp_path):
        trader, fake = make_tp_trader(tmp_path)
        trader.start()
        assert fake.market_orders == []          # NO initial market buy
        assert trader.market_cost == 0.0
        assert len(fake.open) == len(trader.engine.orders) == 5
        assert all(od["side"] == "BUY" for od in fake.open.values())

    def test_buy_fill_places_real_tp_sell(self, tmp_path):
        trader, fake = make_tp_trader(tmp_path)
        trader.start()
        buy_id = engine_oid(trader, "BUY", 104.0)
        fake.fill_order(trader.placed[buy_id])

        fills = trader.poll_once()
        assert [(f.side, f.price) for f in fills] == [("BUY", 104.0)]
        sell_id = engine_oid(trader, "SELL", 107.0)
        coid = trader.placed[sell_id]
        assert fake.open[coid]["side"] == "SELL"
        assert fake.open[coid]["price"] == "107.00"

    def test_tp_fill_rearms_real_buy(self, tmp_path):
        trader, fake = make_tp_trader(tmp_path)
        trader.start()
        fake.fill_order(trader.placed[engine_oid(trader, "BUY", 104.0)])
        trader.poll_once()
        fake.fill_order(trader.placed[engine_oid(trader, "SELL", 107.0)])

        fills = trader.poll_once()
        assert [(f.side, f.price) for f in fills] == [("SELL", 107.0)]
        assert trader.engine.n_round_trips == 1
        buy_id = engine_oid(trader, "BUY", 104.0)   # re-armed
        assert fake.open[trader.placed[buy_id]]["side"] == "BUY"
        assert trader.pending == []

    def test_recovery_places_tp_sell_after_offline_buy(self, tmp_path):
        trader1, fake = make_tp_trader(tmp_path)
        trader1.start()
        buy_id = engine_oid(trader1, "BUY", 104.0)
        fake.fill_order(trader1.placed[buy_id])     # fills while "offline"

        trader2, _ = make_tp_trader(tmp_path, client=fake)
        assert trader2.engine.config.logic == "tp"  # state keeps the logic
        trader2.start()                             # recovery path
        assert fake.market_orders == []             # still no market buy
        sell_id = engine_oid(trader2, "SELL", 107.0)
        s = trader2.engine.orders[sell_id]
        assert s.basis == pytest.approx(104.0) and s.level == 4
        assert fake.open[trader2.placed[sell_id]]["price"] == "107.00"
        assert trader2.pending == []

    def test_recovery_rearms_buy_after_offline_tp(self, tmp_path):
        trader1, fake = make_tp_trader(tmp_path)
        trader1.start()
        fake.fill_order(trader1.placed[engine_oid(trader1, "BUY", 104.0)])
        trader1.poll_once()                         # real TP sell placed
        fake.fill_order(trader1.placed[engine_oid(trader1, "SELL", 107.0)])

        trader2, _ = make_tp_trader(tmp_path, client=fake)
        trader2.start()
        assert trader2.engine.n_round_trips == 1
        buy_id = engine_oid(trader2, "BUY", 104.0)  # re-armed on recovery
        assert fake.open[trader2.placed[buy_id]]["side"] == "BUY"
        assert trader2.pending == []
        # every engine order is mirrored on the exchange again
        for oid in trader2.engine.orders:
            assert trader2.placed[oid] in fake.open


# ---------------------------------------------------------------------------
# CLI wiring: --logic / --rr on backtest, paper, live
# ---------------------------------------------------------------------------

class TestCliWiring:
    @pytest.mark.parametrize("parser", [backtest_parser, paper_parser])
    def test_logic_rr_args(self, parser):
        args = parser().parse_args(["--logic", "tp", "--rr", "2.5"])
        assert args.logic == "tp" and args.rr == 2.5
        defaults = parser().parse_args([])
        assert defaults.logic == "classic" and defaults.rr == 3.0

    def test_live_logic_rr_args(self):
        args = live_parser().parse_args(
            ["--lower", "100", "--upper", "110",
             "--logic", "tp", "--rr", "2.5"])
        assert args.logic == "tp" and args.rr == 2.5
