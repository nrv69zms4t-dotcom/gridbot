"""Paper module: state persist/restore round trip, dry-run cycle. NO network."""

import json

import pytest

from gridbot.engine import GridEngine
from gridbot.exchange import BookTicker
from gridbot.paper import PaperTrader, StaticTickerClient, main, make_logger
from gridbot.tests.conftest import make_config


def make_trader(config, tmp_path, tickers, name="papertest"):
    client = StaticTickerClient(tickers)
    logger = make_logger(config.symbol, log_dir=tmp_path / "logs")
    return PaperTrader(config, client, tmp_path / f"{name}.json", logger,
                       poll_seconds=0.0)


class TestEngineStateRoundTrip:
    def test_serialize_restore_identical_evolution(self, config):
        eng = GridEngine(config)
        eng.start("t0", 105.0)
        eng.on_price("t1", high=104.5, low=103.0, close=103.5)  # 2 buy fills

        restored = GridEngine.from_dict(
            json.loads(json.dumps(eng.to_dict())))  # via real JSON
        assert restored.snapshot(103.5) == eng.snapshot(103.5)

        # same future candle => identical fills and identical state
        f1 = eng.on_price("t2", high=106.5, low=104.8, close=105.0)
        f2 = restored.on_price("t2", high=106.5, low=104.8, close=105.0)
        assert [(f.side, f.price, f.qty) for f in f1] == \
               [(f.side, f.price, f.qty) for f in f2]
        assert restored.snapshot(105.0) == eng.snapshot(105.0)


class TestPaperPersistence:
    def test_restart_resumes_cleanly(self, config, tmp_path):
        t_start = BookTicker("TESTUSDT", bid=104.99, ask=105.01)
        t_dip = BookTicker("TESTUSDT", bid=103.99, ask=104.0)  # ask hits 104

        trader = make_trader(config, tmp_path, [t_start, t_dip])
        trader.step(t_start)                 # starts the grid, no fills
        fills = trader.step(t_dip)           # buy @ 104 fills
        assert [(f.side, f.price) for f in fills] == [("BUY", 104.0)]
        state_before = trader.engine.to_dict()

        # simulate restart: new trader object, same state file
        trader2 = make_trader(config, tmp_path, [t_dip])
        assert trader2.engine.to_dict() == state_before
        assert trader2.engine.started

        # both evolve identically from here
        t_up = BookTicker("TESTUSDT", bid=105.0, ask=105.02)
        fa = trader.step(t_up)
        fb = trader2.step(t_up)
        assert [(f.side, f.price) for f in fa] == \
               [(f.side, f.price) for f in fb] == [("SELL", 105.0)]

    def test_state_written_after_every_event(self, config, tmp_path):
        t = BookTicker("TESTUSDT", bid=104.99, ask=105.01)
        trader = make_trader(config, tmp_path, [t])
        trader.step(t)
        assert trader.state_file.exists()
        payload = json.loads(trader.state_file.read_text(encoding="utf-8"))
        assert payload["engine"]["state"]["started"] is True


class TestBidAskFillMapping:
    def test_buy_needs_ask_sell_needs_bid(self, config, tmp_path):
        trader = make_trader(
            config, tmp_path,
            [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
        trader.step(BookTicker("TESTUSDT", bid=104.99, ask=105.01))
        # bid touches 104 but ask stays above -> BUY must NOT fill
        no_fill = trader.step(BookTicker("TESTUSDT", bid=104.0, ask=104.5))
        assert no_fill == []
        # ask crosses down to 104 -> BUY fills
        fill = trader.step(BookTicker("TESTUSDT", bid=103.9, ask=104.0))
        assert [(f.side, f.price) for f in fill] == [("BUY", 104.0)]


class TestDryRunCLI:
    def test_dry_run_one_cycle_offline(self, tmp_path):
        state = tmp_path / "dry.json"
        trader = main(["--symbol", "DRYTEST", "--dry-run",
                       "--state-file", str(state),
                       "--lower", "100", "--upper", "110",
                       "--levels", "11", "--budget", "1100"])
        assert trader.engine.started
        assert state.exists()
        assert trader.engine.status() == "in_range"
