"""Shared fixtures — synthetic only, NO network in tests."""

import pytest

from gridbot.engine import GridConfig


def make_config(**overrides) -> GridConfig:
    """Canonical test grid: 11 levels 100..110, 100 quote per level."""
    kw = dict(
        symbol="TESTUSDT",
        lower_price=100.0,
        upper_price=110.0,
        n_levels=11,
        spacing="arithmetic",
        quote_budget=1100.0,
        fee_rate=0.001,
        min_notional=1.0,
        qty_step=0.001,
        price_step=0.01,
    )
    kw.update(overrides)
    return GridConfig(**kw)


@pytest.fixture
def config() -> GridConfig:
    return make_config()
