"""Level generation, rounding, and construction-time validation."""

import math

import pytest

from gridbot.engine import GridConfig, GridEngine, generate_levels, round_step
from gridbot.tests.conftest import make_config


class TestRoundStep:
    def test_floor(self):
        assert round_step(0.9619, 0.001, "floor") == pytest.approx(0.961)

    def test_nearest(self):
        assert round_step(103.336, 0.01, "nearest") == pytest.approx(103.34)

    def test_zero_step_passthrough(self):
        assert round_step(1.2345, 0.0) == 1.2345

    def test_no_float_dust(self):
        # classic float trap: 0.1+0.2; Decimal-based rounding must be exact
        assert round_step(0.30000000000000004, 0.1, "floor") == 0.3


class TestLevelGeneration:
    def test_arithmetic_equal_steps(self, config):
        levels = generate_levels(config)
        assert levels == [100.0 + i for i in range(11)]

    def test_geometric_equal_ratio(self):
        cfg = make_config(spacing="geometric", lower_price=100.0,
                          upper_price=200.0, n_levels=5, price_step=1e-6)
        levels = generate_levels(cfg)
        assert levels[0] == pytest.approx(100.0)
        assert levels[-1] == pytest.approx(200.0)
        ratios = [b / a for a, b in zip(levels, levels[1:])]
        for r in ratios:
            assert r == pytest.approx(2 ** 0.25, rel=1e-6)

    def test_prices_rounded_to_price_step(self):
        cfg = make_config(lower_price=100.0, upper_price=101.0, n_levels=7,
                          price_step=0.01)
        for p in generate_levels(cfg):
            # p must sit exactly on the 0.01 grid
            assert abs(p * 100 - round(p * 100)) < 1e-9

    def test_price_step_too_coarse_raises(self):
        cfg = make_config(lower_price=100.0, upper_price=100.05, n_levels=11,
                          price_step=0.01, min_notional=0.0)
        with pytest.raises(ValueError, match="price_step too coarse"):
            generate_levels(cfg)


class TestConfigValidation:
    def test_upper_below_lower_raises(self):
        with pytest.raises(ValueError):
            make_config(lower_price=110.0, upper_price=100.0)

    def test_too_few_levels_raises(self):
        with pytest.raises(ValueError):
            make_config(n_levels=1)

    def test_bad_spacing_raises(self):
        with pytest.raises(ValueError):
            make_config(spacing="fibonacci")


class TestMinNotional:
    def test_min_notional_violation_raises_at_construction(self):
        # 100 quote per level but min_notional 200 -> every level violates
        with pytest.raises(ValueError, match="min_notional"):
            GridEngine(make_config(min_notional=200.0))

    def test_zero_qty_level_raises(self):
        # budget so small that rounding down gives qty == 0
        with pytest.raises(ValueError, match="min_notional"):
            GridEngine(make_config(quote_budget=0.5, min_notional=0.0))

    def test_valid_config_constructs(self, config):
        eng = GridEngine(config)
        for p, q in zip(eng.levels, eng.level_qtys):
            assert p * q >= config.min_notional
            assert q > 0


class TestQtySizing:
    def test_qty_floor_to_step_with_fee_headroom(self, config):
        eng = GridEngine(config)
        slice_quote = config.quote_budget / config.n_levels
        for p, q in zip(eng.levels, eng.level_qtys):
            expected = math.floor(slice_quote / (1 + config.fee_rate) / p
                                  / config.qty_step + 1e-9) * config.qty_step
            assert q == pytest.approx(expected, abs=1e-12)
            # cost incl. fee never exceeds the per-level slice
            assert p * q * (1 + config.fee_rate) <= slice_quote + 1e-9
