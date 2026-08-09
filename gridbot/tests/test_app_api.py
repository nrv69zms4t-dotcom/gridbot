"""App backend (gridbot.app.api.Api) — NO network anywhere.

Injection points used:
* ``client``       — ``StaticTickerClient`` from gridbot.paper
* ``kline_loader`` — synthetic DataFrame factory
* ``data_dir``     — pytest tmp_path
"""

from __future__ import annotations

import json
import time

import pandas as pd
import pytest

from gridbot.app.api import Api
from gridbot.app.main import run_selftest
from gridbot.exchange import BookTicker
from gridbot.paper import StaticTickerClient

CFG = {
    "symbol": "TESTUSDT",
    "lower": 100.0,
    "upper": 110.0,
    "levels": 11,
    "spacing": "arithmetic",
    "budget": 1100.0,
    "fee": 0.001,
    "poll": 1.0,
}


def make_api(tmp_path, tickers=None, loader=None) -> Api:
    client = StaticTickerClient(
        tickers or [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
    return Api(client=client, kline_loader=loader, data_dir=tmp_path)


def wait_until(cond, timeout=8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def synth_df(n: int = 400) -> pd.DataFrame:
    """Triangle wave 101..109 (step 0.5) as 1h candles — plenty of fills."""
    times = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    closes, opens, highs, lows = [], [], [], []
    p, prev, direction = 105.0, 105.0, -1
    for _ in range(n):
        p = round(p + direction * 0.5, 6)
        if p <= 101.0:
            direction = 1
        elif p >= 109.0:
            direction = -1
        opens.append(prev)
        closes.append(p)
        highs.append(max(prev, p) + 0.05)
        lows.append(min(prev, p) - 0.05)
        prev = p
    return pd.DataFrame({"open_time": times, "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": 1.0})


# ---------------------------------------------------------------------------
# config roundtrip
# ---------------------------------------------------------------------------

class TestConfigRoundtrip:
    def test_defaults_when_no_config(self, tmp_path):
        api = make_api(tmp_path)
        d = api.get_defaults()
        assert d["ok"] is True
        assert d["config"]["symbol"] == "BTCUSDT"
        assert d["config"]["levels"] == 20
        assert d["config"]["budget"] == 1000.0
        assert d["config"]["fee"] == 0.001
        assert d["config"]["spacing"] == "arithmetic"
        assert d["config"]["poll"] == 10.0
        assert d["has_saved_state"] is False

    def test_config_saved_and_reloaded(self, tmp_path):
        api = make_api(tmp_path)
        assert api.start_paper(CFG)["ok"] is True
        assert wait_until(lambda: api.get_status()["price"] is not None)
        assert api.stop_paper()["ok"] is True

        api2 = make_api(tmp_path)  # fresh instance, same data dir
        d = api2.get_defaults()
        assert d["ok"] is True
        c = d["config"]
        assert c["symbol"] == "TESTUSDT"
        assert c["levels"] == 11
        assert c["budget"] == 1100.0
        assert c["poll"] == 1.0
        # saved paper state is offered for restore
        assert d["has_saved_state"] is True
        assert d["saved_state_symbol"] == "TESTUSDT"
        # and can be discarded
        assert api2.discard_saved_state()["ok"] is True
        assert api2.get_defaults()["has_saved_state"] is False


# ---------------------------------------------------------------------------
# paper start / stop
# ---------------------------------------------------------------------------

class TestPaperLifecycle:
    def test_start_status_stop(self, tmp_path):
        api = make_api(tmp_path)
        r = api.start_paper(CFG)
        assert r["ok"] is True and r["running"] is True

        assert wait_until(lambda: api.get_status()["price"] is not None)
        st = api.get_status()
        assert st["running"] is True
        assert st["symbol"] == "TESTUSDT"
        assert st["equity"] == pytest.approx(1100.0, rel=0.02)
        assert st["in_range"] is True
        # 5 buys below 105 and 5 sells above (105 level skipped)
        assert len(st["open_orders"]) == 10
        assert len(st["grid_levels"]) == 11

        # double start must fail with a Russian message
        r2 = api.start_paper(CFG)
        assert r2["ok"] is False
        assert "запущен" in r2["error"]

        stop = api.stop_paper()
        assert stop["ok"] is True
        assert api.get_status()["running"] is False
        # stopping again -> polite error
        assert api.stop_paper()["ok"] is False

    def test_resume_from_saved_state(self, tmp_path):
        api = make_api(tmp_path)
        assert api.start_paper(CFG)["ok"] is True
        assert wait_until(lambda: api.get_status()["price"] is not None)
        equity_before = api.get_status()["equity"]
        assert api.stop_paper()["ok"] is True

        api2 = make_api(tmp_path)
        r = api2.resume_paper()
        assert r["ok"] is True and r.get("resumed") is True
        assert wait_until(lambda: api2.get_status()["price"] is not None)
        st = api2.get_status()
        assert st["running"] is True
        assert st["equity"] == pytest.approx(equity_before, rel=0.01)
        assert api2.stop_paper()["ok"] is True


# ---------------------------------------------------------------------------
# get_status: full shape and types, JSON-serializable
# ---------------------------------------------------------------------------

class TestStatusShape:
    REQUIRED = {"ok", "running", "symbol", "price", "equity", "cash", "base",
                "realized_pnl", "unrealized_pnl", "fees", "n_trades",
                "n_round_trips", "in_range", "status", "open_orders",
                "grid_levels", "recent_fills", "price_history", "log_tail"}

    def test_status_idle_shape(self, tmp_path):
        st = make_api(tmp_path).get_status()
        assert self.REQUIRED <= set(st)
        assert st["running"] is False and st["status"] == "stopped"
        json.dumps(st)  # must be JSON-serializable

    def test_status_running_shape_and_types(self, tmp_path):
        api = make_api(tmp_path)
        assert api.start_paper(CFG)["ok"] is True
        assert wait_until(lambda: api.get_status()["price"] is not None)
        st = api.get_status()
        try:
            assert self.REQUIRED <= set(st)
            assert isinstance(st["running"], bool)
            assert isinstance(st["symbol"], str)
            for key in ("price", "equity", "cash", "base", "realized_pnl",
                        "unrealized_pnl", "fees"):
                assert isinstance(st[key], float), key
            for key in ("n_trades", "n_round_trips"):
                assert isinstance(st[key], int), key
            assert isinstance(st["in_range"], bool)
            assert isinstance(st["status"], str)
            assert isinstance(st["open_orders"], list)
            for o in st["open_orders"]:
                assert {"price", "side"} <= set(o)
                assert o["side"] in ("BUY", "SELL")
            assert all(isinstance(x, float) for x in st["grid_levels"])
            assert isinstance(st["recent_fills"], list)
            assert isinstance(st["price_history"], list)
            for q in st["price_history"]:
                assert {"t", "p"} <= set(q)
            assert isinstance(st["log_tail"], list)
            assert len(st["log_tail"]) <= 20
            json.dumps(st)  # must be JSON-serializable
        finally:
            api.stop_paper()


# ---------------------------------------------------------------------------
# validation (Russian error messages)
# ---------------------------------------------------------------------------

class TestValidation:
    def test_lower_not_below_upper(self, tmp_path):
        api = make_api(tmp_path)
        bad = dict(CFG, lower=110.0, upper=100.0)
        r = api.start_paper(bad)
        assert r["ok"] is False
        assert "меньше верхней" in r["error"]

    def test_budget_too_small_for_min_notional(self, tmp_path):
        api = make_api(tmp_path)
        # 50 / 11 levels ~ 4.5 USDT per level < default minNotional 5
        r = api.start_paper(dict(CFG, budget=50.0))
        assert r["ok"] is False
        assert "юджет" in r["error"]  # "Бюджет ..."

    def test_levels_and_budget_and_missing_bounds(self, tmp_path):
        api = make_api(tmp_path)
        assert "уровней" in api.start_paper(dict(CFG, levels=1))["error"]
        assert "Бюджет" in api.start_paper(dict(CFG, budget=-5.0))["error"]
        r = api.start_paper(dict(CFG, lower=None))
        assert r["ok"] is False and "гран" in r["error"]

    def test_errors_never_raise(self, tmp_path):
        api = make_api(tmp_path)
        assert api.start_paper(None)["ok"] is False
        assert api.start_paper({})["ok"] is False
        assert api.stop_paper()["ok"] is False
        assert api.resume_paper()["ok"] is False


# ---------------------------------------------------------------------------
# backtest on a synthetic DataFrame (injected loader, no network)
# ---------------------------------------------------------------------------

class TestBacktest:
    def test_backtest_synthetic(self, tmp_path):
        calls = []

        def loader(symbol, interval, start, end):
            calls.append((symbol, interval, start, end))
            return synth_df()

        api = make_api(tmp_path, loader=loader)
        r = api.run_backtest(CFG)
        assert r["ok"] is True

        assert wait_until(
            lambda: api.get_backtest_status().get("running") is False)
        res = api.get_backtest_status()
        assert res["ok"] is True
        rep = res["report"]
        assert rep is not None
        for key in ("symbol", "n_candles", "total_return", "buyhold_return",
                    "realized", "unrealized", "fees", "max_dd", "n_trades",
                    "n_round_trips", "time_in_range", "equity_curve",
                    "buyhold_curve"):
            assert key in rep, key
        assert calls and calls[0][0] == "TESTUSDT" and calls[0][1] == "1h"
        assert rep["n_candles"] == 400
        assert rep["n_round_trips"] > 0
        assert rep["realized"] > 0  # oscillation must harvest grid profit
        assert 0 < len(rep["equity_curve"]) <= 1000
        assert len(rep["buyhold_curve"]) == len(rep["equity_curve"])
        json.dumps(res)  # must be JSON-serializable

    def test_backtest_curve_downsampled(self, tmp_path):
        api = make_api(tmp_path, loader=lambda *a: synth_df(2500))
        assert api.run_backtest(CFG)["ok"] is True
        assert wait_until(
            lambda: api.get_backtest_status().get("running") is False)
        rep = api.get_backtest_status()["report"]
        assert rep["n_candles"] == 2500
        assert len(rep["equity_curve"]) <= 1000

    def test_backtest_loader_failure_is_russian_error(self, tmp_path):
        def boom(*a):
            raise RuntimeError("no network")

        api = make_api(tmp_path, loader=boom)
        assert api.run_backtest(CFG)["ok"] is True
        assert wait_until(
            lambda: api.get_backtest_status().get("running") is False)
        res = api.get_backtest_status()
        assert res["ok"] is False
        assert "бэктест" in res["error"].lower()


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

class TestSelftest:
    def test_selftest_ok(self, tmp_path):
        res = run_selftest(data_dir=str(tmp_path))
        assert res["ok"] is True, res
        d = res["detail"]
        assert d["n_round_trips"] > 0
        assert d["max_identity_error"] < 1e-6
        json.dumps(res)
