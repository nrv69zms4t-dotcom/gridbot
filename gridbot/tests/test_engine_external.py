"""GridEngine.apply_external_fill — identical accounting to on_price."""

from __future__ import annotations

import json

import pytest

from gridbot.engine import GridEngine
from gridbot.tests.conftest import make_config


class TestExternalFillIdentity:
    def test_snapshots_identical_to_candle_path(self):
        """Same fill sequence via on_price vs apply_external_fill must
        produce byte-identical state (snapshot AND full to_dict)."""
        cfg = make_config()
        a, b = GridEngine(cfg), GridEngine(cfg)
        a.start("t0", 105.0)
        b.start("t0", 105.0)

        # one fill per candle: buy@104, sell@105, buy@104, sell@105
        for i, p in enumerate([104.0, 105.0, 104.0, 105.0]):
            ts = f"t{i + 1}"
            fills = a.on_price(ts, high=p, low=p, close=p)
            assert len(fills) == 1, f"expected single fill at {p}"
            fa = fills[0]
            fb, _paired = b.apply_external_fill(fa.order_id, ts)
            assert (fb.side, fb.price, fb.qty, fb.fee,
                    fb.realized_net) == \
                   (fa.side, fa.price, fa.qty, fa.fee, fa.realized_net)
            assert b.snapshot(p) == a.snapshot(p)

        assert a.to_dict() == b.to_dict()
        # states survive a real JSON round trip identically too
        assert json.loads(json.dumps(a.to_dict())) == \
               json.loads(json.dumps(b.to_dict()))
        assert b.n_round_trips == 2

    def test_returns_paired_sell_after_buy(self, config):
        eng = GridEngine(config)
        eng.start("t0", 105.0)
        buy_id = next(i for i, o in eng.orders.items()
                      if o.side == "BUY" and o.price == pytest.approx(104.0))
        qty = eng.orders[buy_id].qty
        fill, paired = eng.apply_external_fill(buy_id, "t1")
        assert fill.side == "BUY" and fill.price == pytest.approx(104.0)
        assert paired is not None
        assert paired.side == "SELL"
        assert paired.price == pytest.approx(105.0)   # one level up
        assert paired.qty == pytest.approx(qty)
        assert paired.basis == pytest.approx(104.0)
        assert paired.order_id in eng.orders
        assert buy_id not in eng.orders

    def test_returns_paired_buy_after_sell(self, config):
        eng = GridEngine(config)
        eng.start("t0", 105.0)
        sell_id = next(i for i, o in eng.orders.items()
                       if o.side == "SELL"
                       and o.price == pytest.approx(106.0))
        fill, paired = eng.apply_external_fill(sell_id, "t1")
        assert fill.side == "SELL"
        assert fill.realized_net != 0.0
        assert paired is not None
        assert paired.side == "BUY"
        assert paired.price == pytest.approx(105.0)   # one level down

    def test_unknown_or_closed_id_raises(self, config):
        eng = GridEngine(config)
        eng.start("t0", 105.0)
        with pytest.raises(KeyError, match="not open"):
            eng.apply_external_fill(9999, "t1")
        buy_id = next(i for i, o in eng.orders.items() if o.side == "BUY")
        eng.apply_external_fill(buy_id, "t1")
        with pytest.raises(KeyError, match="not open"):
            eng.apply_external_fill(buy_id, "t2")  # already filled

    def test_before_start_raises(self, config):
        eng = GridEngine(config)
        with pytest.raises(RuntimeError, match="start"):
            eng.apply_external_fill(1, "t0")
