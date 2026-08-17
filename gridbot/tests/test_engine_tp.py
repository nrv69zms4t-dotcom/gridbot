"""tp-mode grid logic (buy-ladder + individual TP/RR) — engine level.

Covers: config validation and serialization, start placement (no initial
market buy), exact TP price incl. floor rounding and the one-tick guard,
exact round-trip PnL with the re-arm rule, the same-candle rule, a
hand-computed zigzag backtest, the accounting identity on a random walk,
and apply_external_fill parity with the candle path.
"""

from __future__ import annotations

import json
import random

import pytest

from gridbot.backtest import run_backtest
from gridbot.engine import GridConfig, GridEngine
from gridbot.tests.conftest import make_config
from gridbot.tests.test_backtest import zigzag_df


def tp_config(**overrides) -> GridConfig:
    """Canonical tp-mode grid: classic test grid + logic=tp, RR=3."""
    kw = dict(logic="tp", tp_mult=3.0)
    kw.update(overrides)
    return make_config(**kw)


def started(config: GridConfig, p0: float = 105.0) -> GridEngine:
    eng = GridEngine(config)
    eng.start("t0", p0)
    return eng


# ---------------------------------------------------------------------------
# config: validation + serialization
# ---------------------------------------------------------------------------

class TestTpConfig:
    def test_tp_mult_bounds_enforced_in_tp_mode(self):
        with pytest.raises(ValueError, match="tp_mult"):
            tp_config(tp_mult=0.4)
        with pytest.raises(ValueError, match="tp_mult"):
            tp_config(tp_mult=50.5)
        assert tp_config(tp_mult=0.5).tp_mult == 0.5
        assert tp_config(tp_mult=50.0).tp_mult == 50.0

    def test_unknown_logic_raises(self):
        with pytest.raises(ValueError, match="logic"):
            make_config(logic="martingale")

    def test_classic_ignores_tp_mult(self):
        # out-of-range tp_mult is fine while logic == classic
        cfg = make_config(logic="classic", tp_mult=999.0)
        assert cfg.logic == "classic"

    def test_dict_roundtrip_keeps_logic_and_tp_mult(self):
        cfg = tp_config(tp_mult=2.5)
        d = json.loads(json.dumps(cfg.to_dict()))
        cfg2 = GridConfig.from_dict(d)
        assert cfg2 == cfg
        assert cfg2.logic == "tp" and cfg2.tp_mult == 2.5

    def test_old_dict_without_new_keys_is_classic(self):
        d = make_config().to_dict()
        del d["logic"]
        del d["tp_mult"]
        cfg = GridConfig.from_dict(d)
        assert cfg.logic == "classic" and cfg.tp_mult == 1.0


# ---------------------------------------------------------------------------
# start: buys only, no initial market purchase
# ---------------------------------------------------------------------------

class TestTpStart:
    def test_only_buys_below_p0_and_no_initial_purchase(self):
        cfg = tp_config()
        eng = GridEngine(cfg)
        info = eng.start("t0", 105.0)
        assert info["initial_base"] == 0.0
        assert info["cost"] == 0.0 and info["fee"] == 0.0
        # untouched funds: the whole budget stays in cash, zero base
        assert eng.cash == cfg.quote_budget
        assert eng.base == 0.0 and eng.fees_paid == 0.0
        assert all(o.side == "BUY" for o in eng.orders.values())
        assert sorted(o.price for o in eng.orders.values()) == \
            [100.0, 101.0, 102.0, 103.0, 104.0]  # levels >= 105 stay idle

    def test_p0_above_range_places_all_buys(self):
        eng = started(tp_config(), 120.0)
        assert len(eng.orders) == 11
        assert all(o.side == "BUY" for o in eng.orders.values())

    def test_p0_below_range_places_nothing(self):
        eng = started(tp_config(), 95.0)
        assert eng.orders == {}
        assert eng.on_price("t1", high=96.0, low=94.0, close=95.0) == []


# ---------------------------------------------------------------------------
# TP price: exact placement, floor rounding, one-tick guard
# ---------------------------------------------------------------------------

class TestTpPrice:
    def test_sell_exactly_rr_steps_above_entry(self):
        eng = started(tp_config(), 105.0)   # arithmetic step = 1.0, RR = 3
        [buy] = eng.on_price("t1", high=104.5, low=104.0, close=104.2)
        sells = [o for o in eng.orders.values() if o.side == "SELL"]
        assert len(sells) == 1
        s = sells[0]
        assert s.price == pytest.approx(104.0 + 3.0, abs=1e-12)
        assert s.qty == pytest.approx(buy.qty)
        assert s.basis == pytest.approx(104.0)
        assert s.level == 4                 # ORIGIN level index remembered

    def test_tp_rounded_down_to_price_step(self):
        # raw TP = 104 + 1.7 = 105.7 -> floor to 0.5 grid -> 105.5
        eng = started(tp_config(tp_mult=1.7, price_step=0.5), 105.0)
        eng.on_price("t1", high=104.5, low=104.0, close=104.2)
        [s] = [o for o in eng.orders.values() if o.side == "SELL"]
        assert s.price == pytest.approx(105.5, abs=1e-12)

    def test_tp_never_below_one_tick_above_entry(self):
        # raw TP = 104 + 0.5 = 104.5 -> floor to 1.0 grid would be 104
        # (== entry); the guard bumps it to entry + one price_step = 105
        eng = started(tp_config(tp_mult=0.5, price_step=1.0), 105.0)
        eng.on_price("t1", high=104.5, low=104.0, close=104.2)
        [s] = [o for o in eng.orders.values() if o.side == "SELL"]
        assert s.price == pytest.approx(105.0, abs=1e-12)

    def test_geometric_tp_multiplies_by_ratio_power(self):
        # ratio = 2**(1/4); buy at 100 -> TP = 100 * ratio**2 = 100*sqrt(2)
        cfg = tp_config(spacing="geometric", lower_price=100.0,
                        upper_price=200.0, n_levels=5, tp_mult=2.0)
        eng = started(cfg, 110.0)           # only level 0 (100) below P0
        eng.on_price("t1", high=101.0, low=100.0, close=100.5)
        [s] = [o for o in eng.orders.values() if o.side == "SELL"]
        assert s.price == pytest.approx(141.42, abs=1e-9)  # floored to 0.01

    def test_multiple_concurrent_sells_at_non_level_prices(self):
        eng = started(tp_config(), 105.0)
        fills = eng.on_price("t1", high=104.9, low=100.0, close=100.0)
        assert sorted(f.price for f in fills) == \
            [100.0, 101.0, 102.0, 103.0, 104.0]
        sells = sorted((o.price, o.level) for o in eng.orders.values()
                       if o.side == "SELL")
        assert sells == [(103.0, 0), (104.0, 1), (105.0, 2),
                         (106.0, 3), (107.0, 4)]


# ---------------------------------------------------------------------------
# round trip: exact PnL + re-arm; same-candle rule
# ---------------------------------------------------------------------------

class TestTpRoundTrip:
    def test_realized_exact_and_buy_rearmed_at_origin(self, ):
        cfg = tp_config()
        eng = started(cfg, 105.0)
        [buy] = eng.on_price("t1", high=104.5, low=104.0, close=104.2)
        q = buy.qty
        [sell] = eng.on_price("t2", high=107.0, low=106.0, close=106.5)
        assert (sell.side, sell.price) == ("SELL", 107.0)
        # realized = RR * step * qty - buy fee - sell fee, EXACTLY
        expected = (3.0 * q
                    - 104.0 * q * cfg.fee_rate
                    - 107.0 * q * cfg.fee_rate)
        assert sell.realized_net == pytest.approx(expected, abs=1e-12)
        assert eng.realized_pnl == pytest.approx(expected, abs=1e-12)
        assert eng.n_round_trips == 1
        # the origin level is re-armed with the standard allocation
        rearmed = [o for o in eng.orders.values()
                   if o.side == "BUY" and o.level == 4]
        assert len(rearmed) == 1
        assert rearmed[0].price == pytest.approx(104.0)
        assert rearmed[0].qty == pytest.approx(eng.level_qtys[4])

    def test_rearmed_buy_cannot_fill_in_the_tp_candle(self):
        eng = started(tp_config(), 105.0)
        eng.on_price("t1", high=104.5, low=104.0, close=104.2)
        # one candle spans 104..107: the TP sell fills, and the re-armed
        # buy@104 (created inside this candle) must survive it
        fills = eng.on_price("t2", high=107.0, low=104.0, close=105.0)
        assert [(f.side, f.price) for f in fills] == [("SELL", 107.0)]
        assert any(o.side == "BUY" and o.price == 104.0
                   for o in eng.orders.values())
        # ...and it DOES fill on the next candle
        fills3 = eng.on_price("t3", high=104.5, low=104.0, close=104.2)
        assert [(f.side, f.price) for f in fills3] == [("BUY", 104.0)]


# ---------------------------------------------------------------------------
# zigzag backtest with hand-computed trips and profit
# ---------------------------------------------------------------------------

class TestTpZigzagBacktest:
    def test_exact_round_trips_and_profit(self):
        """P0 = 110, buys on 100..109, RR = 3 (TP = entry + 3).

        Down-leg fills the ladder; up-leg (max 110) harvests only the
        TPs at 103..110, i.e. the buys at 100..107 — the buys at
        108/109 (TP 111/112) stay open forever.  Cycle 1: 10 buys,
        8 trips; every later cycle: 8 buys, 8 trips (only re-armed
        levels 0..7 trade).
        """
        k = 3
        cfg = tp_config()
        df = zigzag_df(k)
        report, curve = run_backtest(df, cfg)

        assert report.logic == "tp" and report.tp_mult == 3.0
        assert report.n_round_trips == 8 * k
        assert report.n_trades == (10 + 8 * (k - 1)) + 8 * k

        qtys = GridEngine(cfg).level_qtys
        fee = cfg.fee_rate
        per_cycle = sum(
            qtys[i] * 3.0
            - qtys[i] * (100.0 + i) * fee      # buy fee
            - qtys[i] * (103.0 + i) * fee      # TP sell fee
            for i in range(8)
        )
        assert report.realized_grid_profit == pytest.approx(k * per_cycle,
                                                            abs=1e-9)

        # buys at 108/109 never harvested: their base is still held
        q8, q9 = qtys[8], qtys[9]
        assert report.final_base_inventory == pytest.approx(q8 + q9,
                                                            abs=1e-12)
        # equity at close 110 = budget + realized(net) + unrealized of the
        # two open lots - their (not yet attributed) buy fees
        expected_equity = (cfg.quote_budget + k * per_cycle
                           + (110.0 - 108.0) * q8 + (110.0 - 109.0) * q9
                           - 108.0 * q8 * fee - 109.0 * q9 * fee)
        assert report.final_equity == pytest.approx(expected_equity,
                                                    abs=1e-9)
        assert report.time_in_range_pct == pytest.approx(100.0)
        assert report.buy_hold_return_pct == pytest.approx(0.0)
        assert curve["n_round_trips"].iloc[-1] == 8 * k

    def test_summary_mentions_logic_and_rr(self):
        report, _ = run_backtest(zigzag_df(1), tp_config())
        assert "RR=3" in report.summary()
        # classic summary stays untouched
        classic, _ = run_backtest(zigzag_df(1), make_config())
        assert "RR" not in classic.summary()
        assert "logic" not in classic.summary()


# ---------------------------------------------------------------------------
# accounting identity on a seeded random walk
# ---------------------------------------------------------------------------

class TestTpAccountingIdentity:
    def test_identity_holds_on_every_step(self):
        """cash + base*close == budget + realized_gross + unrealized - fees,
        at EVERY step of a seeded synthetic path (in and out of range)."""
        cfg = tp_config()
        eng = started(cfg, 105.0)
        rng = random.Random(42)
        close = 105.0
        for t in range(600):
            close = min(max(close + rng.uniform(-1.5, 1.5), 95.0), 115.0)
            hi = close + rng.uniform(0.0, 1.0)
            lo = close - rng.uniform(0.0, 1.0)
            eng.on_price(t, high=hi, low=lo, close=close)
            s = eng.snapshot(close)
            lhs = s["cash"] + s["base"] * close
            rhs = (cfg.quote_budget + s["realized_pnl_gross"]
                   + s["unrealized_pnl"] - s["fees_paid"])
            assert lhs == pytest.approx(rhs, abs=1e-6), f"step {t}"
            assert s["cash"] >= -1e-9  # sizing guarantees no negative cash
            assert s["base"] >= -1e-9
        assert eng.n_round_trips > 0   # the walk actually traded

    def test_snapshot_reports_logic(self):
        eng = started(tp_config(), 105.0)
        assert eng.snapshot(105.0)["logic"] == "tp"
        classic = GridEngine(make_config())
        classic.start("t0", 105.0)
        assert classic.snapshot(105.0)["logic"] == "classic"


# ---------------------------------------------------------------------------
# engine state serialization
# ---------------------------------------------------------------------------

class TestTpSerialization:
    def test_mid_run_roundtrip_and_continue(self):
        eng = started(tp_config(), 105.0)
        eng.on_price("t1", high=104.5, low=102.0, close=103.0)  # 3 buys
        eng.on_price("t2", high=106.0, low=103.5, close=105.5)  # TP fills
        d = json.loads(json.dumps(eng.to_dict()))
        eng2 = GridEngine.from_dict(d)
        assert eng2.to_dict() == eng.to_dict()
        assert eng2.config.logic == "tp" and eng2.config.tp_mult == 3.0
        assert eng2.snapshot(105.5) == eng.snapshot(105.5)
        # both continue identically
        fa = eng.on_price("t3", high=107.5, low=104.0, close=105.0)
        fb = eng2.on_price("t3", high=107.5, low=104.0, close=105.0)
        assert [(f.side, f.price, f.qty) for f in fa] == \
               [(f.side, f.price, f.qty) for f in fb]
        assert eng2.to_dict() == eng.to_dict()

    def test_old_engine_state_without_logic_is_classic(self):
        classic = GridEngine(make_config())
        classic.start("t0", 105.0)
        d = classic.to_dict()
        del d["config"]["logic"]
        del d["config"]["tp_mult"]
        eng = GridEngine.from_dict(json.loads(json.dumps(d)))
        assert eng.config.logic == "classic"
        assert eng.snapshot(105.0)["logic"] == "classic"


# ---------------------------------------------------------------------------
# apply_external_fill parity (live path == candle path)
# ---------------------------------------------------------------------------

class TestTpExternalFillParity:
    def test_snapshots_identical_to_candle_path(self):
        """Same fill sequence via on_price vs apply_external_fill must
        produce byte-identical state in tp mode too."""
        cfg = tp_config()
        a, b = GridEngine(cfg), GridEngine(cfg)
        a.start("t0", 105.0)
        b.start("t0", 105.0)

        # one fill per candle: buy@104, TP@107, re-armed buy@104, TP@107
        for i, p in enumerate([104.0, 107.0, 104.0, 107.0]):
            ts = f"t{i + 1}"
            fills = a.on_price(ts, high=p, low=p, close=p)
            assert len(fills) == 1, f"expected single fill at {p}"
            fa = fills[0]
            fb, _paired = b.apply_external_fill(fa.order_id, ts)
            assert (fb.side, fb.price, fb.qty, fb.fee, fb.realized_net) == \
                   (fa.side, fa.price, fa.qty, fa.fee, fa.realized_net)
            assert b.snapshot(p) == a.snapshot(p)

        assert a.to_dict() == b.to_dict()
        assert json.loads(json.dumps(a.to_dict())) == \
               json.loads(json.dumps(b.to_dict()))
        assert b.n_round_trips == 2

    def test_returns_tp_sell_after_buy_and_rearmed_buy_after_sell(self):
        eng = started(tp_config(), 105.0)
        buy_id = next(i for i, o in eng.orders.items()
                      if o.side == "BUY" and o.price == pytest.approx(104.0))
        qty = eng.orders[buy_id].qty
        fill, paired = eng.apply_external_fill(buy_id, "t1")
        assert fill.side == "BUY"
        assert paired is not None
        assert paired.side == "SELL"
        assert paired.price == pytest.approx(107.0)   # RR steps up
        assert paired.qty == pytest.approx(qty)
        assert paired.basis == pytest.approx(104.0)
        assert paired.level == 4                      # origin level

        fill2, paired2 = eng.apply_external_fill(paired.order_id, "t2")
        assert fill2.side == "SELL" and fill2.realized_net > 0
        assert paired2 is not None
        assert paired2.side == "BUY"
        assert paired2.price == pytest.approx(104.0)  # re-armed at origin
        assert paired2.level == 4
