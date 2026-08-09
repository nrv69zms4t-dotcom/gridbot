"""LiveTrader: placement, fills, budget cap, recovery, CLI. NO network."""

from __future__ import annotations

import json
import logging

import pytest

from gridbot.engine import GridEngine
from gridbot.live import (LiveStopped, LiveTrader, main as live_main,
                          split_symbol)
from gridbot.tests.conftest import make_config
from gridbot.tests.fake_signed import FakeSignedClient

_logger_seq = 0


def make_test_logger() -> logging.Logger:
    """Fresh handler-less logger per test (propagates into caplog)."""
    global _logger_seq
    _logger_seq += 1
    logger = logging.getLogger(f"test.gridbot.live.{_logger_seq}")
    logger.setLevel(logging.INFO)
    return logger


def make_trader(tmp_path, client=None, config=None, **kw):
    config = config or make_config()
    client = client or FakeSignedClient()
    trader = LiveTrader(config, client, tmp_path / "live.json",
                        make_test_logger(), poll_seconds=0.0, **kw)
    return trader, client


def engine_oid(trader: LiveTrader, side: str, price: float) -> int:
    for oid, o in trader.engine.orders.items():
        if o.side == side and o.price == pytest.approx(price):
            return oid
    raise AssertionError(f"no open engine {side} @ {price}")


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

class TestStart:
    def test_places_market_buy_then_all_limits(self, tmp_path):
        trader, fake = make_trader(tmp_path)
        trader.start()

        # run id: 6 hex chars persisted in state
        assert len(trader.run_id) == 6
        assert all(c in "0123456789abcdef" for c in trader.run_id)

        # exactly one real market BUY funding the sell side
        assert len(fake.market_orders) == 1
        mo = fake.market_orders[0]
        assert mo["side"] == "BUY"
        spent = float(mo["cummulativeQuoteQty"])
        assert 0 < spent <= trader.config.quote_budget * 1.001
        assert trader.market_cost == pytest.approx(spent)

        # every engine order mirrored with the deterministic clientOrderId
        assert len(fake.open) == len(trader.engine.orders) == 10
        for oid, o in trader.engine.orders.items():
            coid = f"gb{trader.run_id}-{oid}"
            assert trader.placed[oid] == coid
            od = fake.open[coid]
            assert od["side"] == o.side
            assert float(od["price"]) == pytest.approx(o.price)
            assert float(od["origQty"]) == pytest.approx(o.qty)
            assert "e" not in od["price"].lower()
            assert "e" not in od["origQty"].lower()

        # state persisted with the order map
        payload = json.loads(trader.state_file.read_text(encoding="utf-8"))
        assert payload["live"]["run_id"] == trader.run_id
        assert set(payload["live"]["placed"].values()) == set(fake.open)
        assert payload["live"]["pending"] == []

    def test_insufficient_quote_balance_russian_error(self, tmp_path):
        fake = FakeSignedClient(balances={"USDT": 10.0})
        trader, _ = make_trader(tmp_path, client=fake)
        with pytest.raises(RuntimeError, match="Недостаточно средств"):
            trader.start()
        assert fake.open == {} and fake.market_orders == []
        assert not trader.state_file.exists()

    def test_market_buy_failure_aborts_before_any_limit(self, tmp_path):
        fake = FakeSignedClient()
        fake.fail_market = True
        trader, _ = make_trader(tmp_path, client=fake)
        with pytest.raises(RuntimeError, match="Стартовая покупка"):
            trader.start()
        assert fake.open == {}          # not a single limit was placed
        assert not trader.state_file.exists()  # nothing persisted


# ---------------------------------------------------------------------------
# poll cycle: fills and pairing
# ---------------------------------------------------------------------------

class TestFills:
    def test_buy_fill_places_real_paired_sell(self, tmp_path):
        trader, fake = make_trader(tmp_path)
        trader.start()
        buy_id = engine_oid(trader, "BUY", 104.0)
        fake.fill_order(trader.placed[buy_id])

        fills = trader.poll_once()
        assert [(f.side, f.price) for f in fills] == [("BUY", 104.0)]
        assert trader.engine.n_trades == 1
        assert buy_id not in trader.placed

        # the paired SELL one level up is now a REAL order
        sell_id = engine_oid(trader, "SELL", 105.0)
        coid = trader.placed[sell_id]
        assert fake.open[coid]["side"] == "SELL"
        assert fake.open[coid]["price"] == "105.00"

    def test_sell_fill_places_real_paired_buy(self, tmp_path):
        trader, fake = make_trader(tmp_path)
        trader.start()
        sell_id = engine_oid(trader, "SELL", 106.0)
        fake.fill_order(trader.placed[sell_id])

        fills = trader.poll_once()
        assert [(f.side, f.price) for f in fills] == [("SELL", 106.0)]
        buy_id = engine_oid(trader, "BUY", 105.0)
        assert fake.open[trader.placed[buy_id]]["side"] == "BUY"

    def test_placement_failure_is_queued_and_retried(self, tmp_path,
                                                     caplog):
        trader, fake = make_trader(tmp_path)
        trader.start()
        buy_id = engine_oid(trader, "BUY", 104.0)
        fake.fill_order(trader.placed[buy_id])
        fake.fail_next_place = 1

        with caplog.at_level(logging.INFO):
            trader.poll_once()
        sell_id = engine_oid(trader, "SELL", 105.0)
        assert sell_id in trader.pending          # queued, not on exchange
        assert sell_id not in trader.placed
        assert any("повторим" in r.message for r in caplog.records)
        # queue survives persistence
        payload = json.loads(trader.state_file.read_text(encoding="utf-8"))
        assert payload["live"]["pending"] == [sell_id]

        trader.poll_once()                        # retry succeeds now
        assert trader.pending == []
        assert fake.open[trader.placed[sell_id]]["side"] == "SELL"

    def test_partial_fill_waits_and_logs_once(self, tmp_path, caplog):
        trader, fake = make_trader(tmp_path)
        trader.start()
        buy_id = engine_oid(trader, "BUY", 104.0)
        coid = trader.placed[buy_id]
        fake.partial_fill(coid, 0.5)

        with caplog.at_level(logging.INFO):
            assert trader.poll_once() == []
            assert trader.poll_once() == []
        assert trader.engine.n_trades == 0        # engine untouched
        assert coid in fake.open                  # still resting
        partial_msgs = [r for r in caplog.records
                        if "ЧАСТИЧНО" in r.message]
        assert len(partial_msgs) == 1             # logged exactly once

        # once fully filled, it is applied normally
        fake.fill_order(coid)
        fills = trader.poll_once()
        assert [(f.side, f.price) for f in fills] == [("BUY", 104.0)]


# ---------------------------------------------------------------------------
# budget cap
# ---------------------------------------------------------------------------

class TestBudgetCap:
    def test_buy_exceeding_cap_is_refused(self, tmp_path, caplog):
        trader, fake = make_trader(tmp_path)
        trader.start()
        # simulate a desync where the whole budget is already at risk
        # (model cash gone): any further BUY must be refused
        trader.engine.cash = 0.0
        sell_id = engine_oid(trader, "SELL", 106.0)
        fake.fill_order(trader.placed[sell_id])

        with caplog.at_level(logging.INFO):
            trader.poll_once()
        buy_id = engine_oid(trader, "BUY", 105.0)  # engine paired it...
        assert buy_id in trader.pending            # ...but NOT sent
        assert buy_id not in trader.placed
        assert not any(od["side"] == "BUY"
                       and od["price"] == "105.00"
                       for od in fake.open.values())
        assert any("БЮДЖЕТ" in r.message for r in caplog.records)

    def test_normal_grid_never_trips_the_cap(self, tmp_path):
        trader, fake = make_trader(tmp_path)
        trader.start()
        # a full oscillation: down through every buy, back up, and down
        for price in (99.0, 111.0, 99.0):
            fake.fill_at_price(price)
            trader.poll_once()
            trader.poll_once()  # place-after-fill then verify stable
        assert trader.pending == []
        assert trader.engine.n_round_trips > 0


# ---------------------------------------------------------------------------
# external cancel and stop
# ---------------------------------------------------------------------------

class TestExternalCancelAndStop:
    def test_external_cancel_stops_bot_safely(self, tmp_path, caplog):
        trader, fake = make_trader(tmp_path)
        trader.start()
        coid = trader.placed[engine_oid(trader, "BUY", 104.0)]
        fake.cancel_external(coid)

        with caplog.at_level(logging.INFO):
            with pytest.raises(LiveStopped, match="отменён извне"):
                trader.poll_once()
        assert trader.stop_reason == "ордер отменён извне"
        assert fake.open == {}         # remaining orders were cancelled
        assert any("отменён извне" in r.message for r in caplog.records)

    def test_stop_cancels_all_tracked_orders(self, tmp_path):
        trader, fake = make_trader(tmp_path)
        trader.start()
        assert len(fake.open) == 10
        trader.stop()
        assert fake.open == {}
        assert trader.placed == {}
        # cancelled engine orders are queued for a possible resume
        assert sorted(trader.pending) == sorted(trader.engine.orders)
        payload = json.loads(trader.state_file.read_text(encoding="utf-8"))
        assert payload["live"]["placed"] == {}

    def test_keep_orders_on_stop(self, tmp_path):
        trader, fake = make_trader(tmp_path, keep_orders_on_stop=True)
        trader.start()
        trader.stop()
        assert len(fake.open) == 10    # untouched on the exchange

    def test_stop_cancel_failures_logged_not_fatal(self, tmp_path, caplog):
        trader, fake = make_trader(tmp_path)
        trader.start()
        # one order vanished exchange-side: cancel will fail for it
        victim = next(iter(trader.placed.values()))
        del fake.open[victim]
        with caplog.at_level(logging.INFO):
            trader.stop()              # must not raise
        assert fake.open == {}
        assert any("не удалось отменить" in r.message
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# restart recovery
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_offline_fills_applied_in_updatetime_order(self, tmp_path):
        trader1, fake = make_trader(tmp_path)
        trader1.start()
        # while "offline": a buy fills first, then a sell (distinct
        # updateTime stamps in this order)
        buy_id = engine_oid(trader1, "BUY", 104.0)
        sell_id = engine_oid(trader1, "SELL", 106.0)
        fake.fill_order(trader1.placed[buy_id])
        fake.fill_order(trader1.placed[sell_id])

        # reference: the same fills applied directly, in the same order
        ref = GridEngine(make_config())
        ref.start("t0", trader1.engine.start_price)
        ref.apply_external_fill(buy_id, "tr1")
        ref.apply_external_fill(sell_id, "tr2")

        trader2, _ = make_trader(tmp_path, client=fake)
        assert trader2.run_id == trader1.run_id   # persisted run id
        trader2.start()                           # recovery path

        assert trader2.engine.n_trades == 2
        s2, sr = trader2.engine.snapshot(105.0), ref.snapshot(105.0)
        # timestamps differ; the accounting must not
        for key in ("cash", "base", "equity", "realized_pnl",
                    "realized_pnl_gross", "unrealized_pnl", "fees_paid",
                    "n_trades", "n_round_trips", "n_open_orders"):
            assert s2[key] == sr[key], key
        # the paired orders created offline are now REAL orders
        for oid, o in trader2.engine.orders.items():
            assert oid in trader2.placed
            assert trader2.placed[oid] in fake.open
        assert trader2.pending == []

    def test_recovery_replaces_pending_pair(self, tmp_path):
        trader1, fake = make_trader(tmp_path)
        trader1.start()
        buy_id = engine_oid(trader1, "BUY", 104.0)
        fake.fill_order(trader1.placed[buy_id])
        fake.fail_next_place = 1
        trader1.poll_once()                       # pair SELL stays queued
        sell_id = engine_oid(trader1, "SELL", 105.0)
        assert sell_id in trader1.pending

        trader2, _ = make_trader(tmp_path, client=fake)
        trader2.start()
        assert trader2.pending == []
        assert fake.open[trader2.placed[sell_id]]["side"] == "SELL"

    def test_recovery_replaces_externally_cancelled(self, tmp_path):
        trader1, fake = make_trader(tmp_path)
        trader1.start()
        coid = trader1.placed[engine_oid(trader1, "BUY", 104.0)]
        fake.cancel_external(coid)                # cancelled while offline

        trader2, _ = make_trader(tmp_path, client=fake)
        trader2.start()
        # re-placed (clientOrderId reuse after terminal state is allowed)
        assert coid in fake.open
        assert fake.open[coid]["status"] == "NEW"
        assert len(fake.open) == 10

    def test_recovery_noop_when_everything_matches(self, tmp_path):
        trader1, fake = make_trader(tmp_path)
        trader1.start()
        before = dict(fake.open)
        trader2, _ = make_trader(tmp_path, client=fake)
        trader2.start()
        assert fake.open == before                # nothing re-sent
        assert trader2.placed == trader1.placed


# ---------------------------------------------------------------------------
# helpers / CLI
# ---------------------------------------------------------------------------

class TestSplitSymbol:
    def test_common_pairs(self):
        assert split_symbol("BTCUSDT") == ("BTC", "USDT")
        assert split_symbol("TESTUSDT") == ("TEST", "USDT")
        assert split_symbol("ETHBTC") == ("ETH", "BTC")

    def test_unknown_quote_raises(self):
        with pytest.raises(ValueError):
            split_symbol("XYZ")


class TestCli:
    def test_help_works(self, capsys):
        with pytest.raises(SystemExit) as ei:
            live_main(["--help"])
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "--mainnet" in out and "--testnet" in out

    def test_keys_only_from_env(self, monkeypatch):
        monkeypatch.delenv("GRIDBOT_API_KEY", raising=False)
        monkeypatch.delenv("GRIDBOT_API_SECRET", raising=False)
        with pytest.raises(SystemExit, match="GRIDBOT_API_KEY"):
            live_main(["--lower", "100", "--upper", "110"])

    def test_mainnet_requires_interactive_yes(self, monkeypatch):
        monkeypatch.setenv("GRIDBOT_API_KEY", "k")
        monkeypatch.setenv("GRIDBOT_API_SECRET", "s")
        monkeypatch.setattr("builtins.input", lambda *a: "no")
        with pytest.raises(SystemExit, match="Отменено"):
            live_main(["--lower", "100", "--upper", "110", "--mainnet"])
