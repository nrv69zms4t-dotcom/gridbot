"""Backtester end-to-end on a synthetic zigzag with hand-computable results.

Grid: 11 levels at 100..110, 100 quote per level, fee 0.1%.
Start price P0 = 110 (top level) -> 10 BUY limits at 100..109, no initial
inventory.  Price path: flat candles stepping one level per candle,
zigzagging 110 -> 100 -> 110, k = 3 full cycles.

Hand computation per cycle: 10 buy fills on the way down, 10 sell fills on
the way up => 10 round trips per cycle, each with
    profit_i = qty_i * 1.0 - qty_i * (p_i + p_{i+1}) * fee
where p_i = 100 + i and qty_i is the level quantity at the buy level.
"""

import pytest
import pandas as pd

from gridbot.backtest import derive_range, run_backtest
from gridbot.engine import GridEngine
from gridbot.tests.conftest import make_config


def zigzag_df(n_cycles: int = 3) -> pd.DataFrame:
    prices = [110.0]
    for _ in range(n_cycles):
        prices += [float(p) for p in range(109, 99, -1)]   # 109..100
        prices += [float(p) for p in range(101, 111)]      # 101..110
    times = pd.date_range("2025-01-01", periods=len(prices), freq="1min",
                          tz="UTC")
    return pd.DataFrame({
        "open_time": times, "open": prices, "high": prices,
        "low": prices, "close": prices,
        "volume": [0.0] * len(prices),
    })


class TestZigzagBacktest:
    def test_exact_round_trips_and_profit(self, config):
        k = 3
        df = zigzag_df(k)
        report, curve = run_backtest(df, config)

        # exact trade counts: 10 buys + 10 sells per cycle
        assert report.n_round_trips == 10 * k
        assert report.n_trades == 20 * k

        # exact realized profit, computed by hand from level quantities
        qtys = GridEngine(config).level_qtys
        fee = config.fee_rate
        per_cycle = sum(
            qtys[i] * 1.0
            - qtys[i] * (100.0 + i) * fee          # buy fee
            - qtys[i] * (101.0 + i) * fee          # sell fee
            for i in range(10)
        )
        assert report.realized_grid_profit == pytest.approx(k * per_cycle,
                                                            abs=1e-9)

        # path ends at 110 with zero inventory: equity = budget + realized
        assert report.final_base_inventory == pytest.approx(0.0, abs=1e-12)
        assert report.final_equity == pytest.approx(
            config.quote_budget + k * per_cycle, abs=1e-9)
        assert report.total_return_pct == pytest.approx(
            100.0 * k * per_cycle / config.quote_budget, abs=1e-9)

        # price never left [100, 110]; buy & hold went 110 -> 110
        assert report.time_in_range_pct == pytest.approx(100.0)
        assert report.buy_hold_return_pct == pytest.approx(0.0)

        # equity curve sanity
        assert len(curve) == len(df)
        assert curve["equity"].iloc[-1] == pytest.approx(report.final_equity)
        assert curve["n_round_trips"].iloc[-1] == 10 * k

    def test_fees_positive_and_drawdown_nonpositive(self, config):
        report, _ = run_backtest(zigzag_df(2), config)
        assert report.fees_paid > 0
        assert report.max_drawdown_pct <= 0.0


class TestAutoRange:
    def test_range_uses_only_leading_fraction(self):
        # first 20%: prices around 100..110; remainder: around 200..210 —
        # a lookahead-free range must come from the FIRST part only
        head = zigzag_df(1)
        tail = zigzag_df(1).copy()
        tail["open_time"] = pd.date_range(
            "2025-02-01", periods=len(tail), freq="1min", tz="UTC")
        for c in ("open", "high", "low", "close"):
            tail[c] = tail[c] + 100.0
        df = pd.concat([head] * 1 + [tail] * 4, ignore_index=True)

        lower, upper, n_calib = derive_range(df, frac=0.2)
        assert n_calib == len(head)
        assert 100.0 <= lower <= upper <= 110.0  # untouched by the tail
