"""App backend: compute_range, list_symbols, grid_mode config, 451 text.

NO network — injected offline clients only (see gridbot.app.api
injection points).
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from gridbot.app.api import (BUILTIN_USDT_SYMBOLS, DEFAULT_CONFIG,
                             SYMBOLS_CACHE_FILE, Api, _humanize_net_error)
from gridbot.exchange import BookTicker
from gridbot.paper import StaticTickerClient

windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="DPAPI is Windows-only")

CFG = {
    "symbol": "TESTUSDT",
    "lower": 100.0,
    "upper": 110.0,
    "levels": 11,
    "spacing": "arithmetic",
    "budget": 1100.0,
    "fee": 0.001,
    "poll": 1.0,
    "grid_mode": "step",
    "step_pct": 0.7,
}


def wait_until(cond, timeout=8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def price_client(mid: float) -> StaticTickerClient:
    return StaticTickerClient(
        [BookTicker("TESTUSDT", bid=mid - 0.01, ask=mid + 0.01)])


class BoomTickerClient(StaticTickerClient):
    def __init__(self, msg: str = "сеть недоступна"):
        super().__init__([BookTicker("TESTUSDT", bid=1.0, ask=1.0)])
        self.msg = msg

    def get_book_ticker(self, symbol: str):
        raise RuntimeError(self.msg)


# ---------------------------------------------------------------------------
# compute_range: exact math and validation
# ---------------------------------------------------------------------------

class TestComputeRange:
    def test_arithmetic_hand_computed(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        r = api.compute_range("btcusdt", 1.0, 11, "arithmetic")
        assert r["ok"] is True
        assert r["symbol"] == "BTCUSDT"
        # price 105, step 1% -> step_abs 1.05, span 10.5, symmetric
        assert r["price"] == 105.0
        assert r["step_abs"] == pytest.approx(1.05)
        assert r["lower"] == pytest.approx(99.75)
        assert r["upper"] == pytest.approx(110.25)
        json.dumps(r)

    def test_geometric_hand_computed(self, tmp_path):
        api = Api(client=price_client(100.0), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 1.0, 5, "geometric")
        assert r["ok"] is True
        # r = 1.01, half = 2: upper = 100*1.01^2 = 102.01,
        # lower = 100/1.0201 = 98.02960... -> 98.0296 (6 sig digits)
        assert r["upper"] == pytest.approx(102.01)
        assert r["lower"] == pytest.approx(98.0296, abs=5e-5)
        assert r["price"] == 100.0

    def test_step_pct_validation_russian(self, tmp_path):
        api = Api(client=price_client(100.0), data_dir=tmp_path)
        for bad in (0.01, 20.0, 0.0, -1.0, None, "abc"):
            r = api.compute_range("TESTUSDT", bad, 11, "arithmetic")
            assert r["ok"] is False, bad
            assert "Шаг" in r["error"], bad

    def test_levels_validation_russian(self, tmp_path):
        api = Api(client=price_client(100.0), data_dir=tmp_path)
        for bad in (1, 201, 2.5, None, "x"):
            r = api.compute_range("TESTUSDT", 1.0, bad, "arithmetic")
            assert r["ok"] is False, bad
            assert "уровней" in r["error"], bad

    def test_bad_spacing_and_empty_symbol(self, tmp_path):
        api = Api(client=price_client(100.0), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 1.0, 11, "linear")
        assert r["ok"] is False and "Шаг сетки" in r["error"]
        r = api.compute_range("", 1.0, 11, "arithmetic")
        assert r["ok"] is False and "символ" in r["error"]

    def test_lower_below_zero_rejected(self, tmp_path):
        # step 10% x 21 levels (arithmetic): span = 2*price -> lower = 0
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 10.0, 21, "arithmetic")
        assert r["ok"] is False
        assert "нижнюю" in r["error"]
        # geometric never crosses zero: the same inputs are fine
        r2 = api.compute_range("TESTUSDT", 10.0, 21, "geometric")
        assert r2["ok"] is True and r2["lower"] > 0

    def test_network_failure_never_raises(self, tmp_path):
        api = Api(client=BoomTickerClient(), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 1.0, 11, "arithmetic")
        assert r["ok"] is False
        assert "цену" in r["error"]

    def test_451_failure_mentions_mirror(self, tmp_path):
        api = Api(client=BoomTickerClient("HTTP 451"), data_dir=tmp_path)
        r = api.compute_range("TESTUSDT", 1.0, 11, "arithmetic")
        assert r["ok"] is False
        assert "зеркало" in r["error"]


# ---------------------------------------------------------------------------
# list_symbols: network -> cache (24h TTL) -> stale cache -> builtin
# ---------------------------------------------------------------------------

EXCHANGE_INFO = {"symbols": [
    {"symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT",
     "isSpotTradingAllowed": True},
    {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT",
     "isSpotTradingAllowed": True},
    {"symbol": "ETHBTC", "status": "TRADING", "quoteAsset": "BTC",
     "isSpotTradingAllowed": True},           # wrong quote -> dropped
    {"symbol": "OLDUSDT", "status": "BREAK", "quoteAsset": "USDT",
     "isSpotTradingAllowed": True},           # not trading -> dropped
    {"symbol": "LEVUSDT", "status": "TRADING", "quoteAsset": "USDT",
     "isSpotTradingAllowed": False},          # not spot -> dropped
]}


class InfoClient(StaticTickerClient):
    """Offline stub serving exchangeInfo (calls counted / can fail)."""

    def __init__(self, payload=EXCHANGE_INFO, fail=False):
        super().__init__([BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
        self.payload = payload
        self.fail = fail
        self.info_calls = 0

    def get_exchange_info(self) -> dict:
        self.info_calls += 1
        if self.fail:
            raise RuntimeError("сеть недоступна")
        return self.payload


class TestListSymbols:
    def test_network_path_filters_and_caches(self, tmp_path):
        client = InfoClient()
        api = Api(client=client, data_dir=tmp_path)
        r = api.list_symbols()
        assert r["ok"] is True
        assert r["source"] == "network"
        assert r["symbols"] == ["BTCUSDT", "ETHUSDT"]  # filtered + sorted
        json.dumps(r)
        # cache file shape: {"timestamp": <epoch>, "symbols": [...]}
        cache = tmp_path / "data_cache" / SYMBOLS_CACHE_FILE
        assert cache.exists()
        raw = json.loads(cache.read_text(encoding="utf-8"))
        assert set(raw) == {"timestamp", "symbols"}
        assert raw["symbols"] == ["BTCUSDT", "ETHUSDT"]
        assert abs(raw["timestamp"] - time.time()) < 60

    def test_cache_ttl_hit_no_network_call(self, tmp_path):
        client = InfoClient()
        api = Api(client=client, data_dir=tmp_path)
        assert api.list_symbols()["source"] == "network"
        assert client.info_calls == 1
        r = api.list_symbols()               # within 24h TTL
        assert r["source"] == "cache"
        assert r["symbols"] == ["BTCUSDT", "ETHUSDT"]
        assert client.info_calls == 1        # NOT called again

    def test_stale_cache_used_on_network_error(self, tmp_path):
        cache_dir = tmp_path / "data_cache"
        cache_dir.mkdir(parents=True)
        stale_ts = time.time() - 30 * 3600   # 30h old: past the 24h TTL
        (cache_dir / SYMBOLS_CACHE_FILE).write_text(
            json.dumps({"timestamp": stale_ts, "symbols": ["AAAUSDT"]}),
            encoding="utf-8")
        client = InfoClient(fail=True)
        api = Api(client=client, data_dir=tmp_path)
        r = api.list_symbols()
        assert client.info_calls == 1        # network WAS attempted
        assert r["source"] == "cache"        # stale cache beats builtin
        assert r["symbols"] == ["AAAUSDT"]

    def test_builtin_fallback_without_cache(self, tmp_path):
        api = Api(client=InfoClient(fail=True), data_dir=tmp_path)
        r = api.list_symbols()
        assert r["ok"] is True and r["source"] == "builtin"
        assert r["symbols"] == BUILTIN_USDT_SYMBOLS
        assert "BTCUSDT" in r["symbols"] and len(r["symbols"]) == 30

    def test_client_without_exchange_info_is_builtin(self, tmp_path):
        # plain StaticTickerClient has no get_exchange_info at all
        api = Api(client=price_client(100.0), data_dir=tmp_path)
        r = api.list_symbols()
        assert r["ok"] is True and r["source"] == "builtin"

    def test_corrupt_cache_is_ignored(self, tmp_path):
        cache_dir = tmp_path / "data_cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / SYMBOLS_CACHE_FILE).write_text("{broken json",
                                                    encoding="utf-8")
        api = Api(client=InfoClient(), data_dir=tmp_path)
        assert api.list_symbols()["source"] == "network"


# ---------------------------------------------------------------------------
# config roundtrip with grid_mode / step_pct (backward compatible)
# ---------------------------------------------------------------------------

class TestGridModeConfig:
    def test_defaults_include_grid_mode(self, tmp_path):
        assert DEFAULT_CONFIG["grid_mode"] == "manual"
        assert DEFAULT_CONFIG["step_pct"] == 0.5
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        c = api.get_defaults()["config"]
        assert c["grid_mode"] == "manual" and c["step_pct"] == 0.5

    def test_roundtrip_step_mode(self, tmp_path):
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        assert api.start_paper(CFG)["ok"] is True
        assert wait_until(lambda: api.get_status()["price"] is not None)
        assert api.stop_paper()["ok"] is True

        api2 = Api(client=price_client(105.0), data_dir=tmp_path)
        c = api2.get_defaults()["config"]
        assert c["grid_mode"] == "step"
        assert c["step_pct"] == 0.7
        assert c["symbol"] == "TESTUSDT"

    def test_old_config_without_new_keys_gets_defaults(self, tmp_path):
        # config.json written by an OLD app version (no grid_mode)
        old = {"symbol": "ETHUSDT", "lower": 1000.0, "upper": 2000.0,
               "levels": 15, "spacing": "geometric", "budget": 500.0,
               "fee": 0.001, "poll": 5.0}
        (tmp_path / "config.json").write_text(json.dumps(old),
                                              encoding="utf-8")
        api = Api(client=price_client(105.0), data_dir=tmp_path)
        c = api.get_defaults()["config"]
        assert c["symbol"] == "ETHUSDT"          # old values kept
        assert c["grid_mode"] == "manual"        # new keys defaulted
        assert c["step_pct"] == 0.5


# ---------------------------------------------------------------------------
# 451 -> human Russian explanation
# ---------------------------------------------------------------------------

class TestHumanize451:
    def test_451_maps_to_region_message(self):
        msg = _humanize_net_error(RuntimeError("Binance HTTP 451: closed"))
        assert "региона" in msg and "451" in msg
        assert "зеркало" in msg          # market data still works
        assert "live" in msg             # trading does not

    def test_other_errors_keep_generic_text(self):
        generic = "Проверка подключения не удалась."
        assert _humanize_net_error(RuntimeError("timeout"),
                                   generic) == generic

    @windows_only
    def test_test_connection_surfaces_region_message(self, tmp_path):
        class Blocked451Client:
            def get_server_time(self):
                raise RuntimeError("Binance HTTP 451: region blocked")

        api = Api(client=price_client(105.0), data_dir=tmp_path,
                  signed_client_factory=lambda n, e: Blocked451Client())
        assert api.save_api_keys("MYTESTKEY1234567890", "s3cret")["ok"]
        r = api.test_connection("mainnet")
        assert r["ok"] is False
        assert "региона" in r["error"] and "451" in r["error"]
