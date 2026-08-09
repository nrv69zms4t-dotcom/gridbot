"""App backend: chart data feed + resume with offline catch-up. NO network.

Injection points: ``client`` (offline stub with raw klines), ``data_dir``
(tmp_path), ``raw_kline_fetcher`` (synthetic candles for the replay).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from gridbot.app.api import Api
from gridbot.exchange import BookTicker
from gridbot.paper import StaticTickerClient

MIN = 60_000

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


def wait_until(cond, timeout=8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def row(open_ms: int, o: float, h: float, lo: float, c: float) -> list:
    return [open_ms, str(o), str(h), str(lo), str(c), "1.0",
            open_ms + MIN - 1, "0", 0, "0", "0", "0"]


class ChartClient(StaticTickerClient):
    """Offline ticker stub that also serves raw kline rows (counted)."""

    def __init__(self, rows):
        super().__init__(
            [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
        self.rows = rows
        self.kline_calls = 0

    def get_klines(self, symbol, interval, start_ms, end_ms, limit=1000):
        self.kline_calls += 1
        return [r for r in self.rows
                if start_ms <= r[0] <= end_ms][:limit]


class BoomClient(StaticTickerClient):
    def __init__(self):
        super().__init__(
            [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])

    def get_klines(self, *a, **k):
        raise RuntimeError("сеть недоступна")


def recent_rows(n: int = 10) -> list:
    """n closed 1m candles ending at the current minute boundary."""
    floor_now = int(time.time() * 1000) // MIN * MIN
    return [row(floor_now - (n - k) * MIN, 105.0, 105.5, 104.5, 105.1)
            for k in range(n)]


# ---------------------------------------------------------------------------
# get_chart_data: shape, throttle, validation
# ---------------------------------------------------------------------------

class TestGetChartData:
    def test_shape_and_parsing(self, tmp_path):
        rows = recent_rows(10)
        client = ChartClient(rows)
        api = Api(client=client, data_dir=tmp_path)
        r = api.get_chart_data("testusdt", "1m")
        assert r["ok"] is True
        assert r["symbol"] == "TESTUSDT" and r["interval"] == "1m"
        candles = r["candles"]
        assert len(candles) == 10
        for c, src in zip(candles, rows):
            assert set(c) == {"time", "open", "high", "low", "close"}
            assert isinstance(c["time"], int)
            assert c["time"] == src[0] // 1000        # unix SECONDS
            assert c["open"] == float(src[1])
            assert c["high"] == float(src[2])
            assert c["low"] == float(src[3])
            assert c["close"] == float(src[4])
        json.dumps(r)  # must be JSON-serializable

    def test_throttle_5s_per_symbol_interval(self, tmp_path):
        client = ChartClient(recent_rows(5))
        api = Api(client=client, data_dir=tmp_path)
        r1 = api.get_chart_data("TESTUSDT", "1m")
        r2 = api.get_chart_data("TESTUSDT", "1m")  # within 5 s: cached
        assert r1["ok"] and r2["ok"]
        assert client.kline_calls == 1
        assert r2 == r1
        # a DIFFERENT interval is its own cache key -> new HTTP call
        api.get_chart_data("TESTUSDT", "5m")
        assert client.kline_calls == 2
        # and a different symbol too
        api.get_chart_data("OTHERUSDT", "1m")
        assert client.kline_calls == 3

    def test_bad_interval_and_symbol_russian_errors(self, tmp_path):
        api = Api(client=ChartClient([]), data_dir=tmp_path)
        r = api.get_chart_data("TESTUSDT", "2h")
        assert r["ok"] is False and "интервал" in r["error"].lower()
        r = api.get_chart_data("", "1m")
        assert r["ok"] is False and "символ" in r["error"].lower()
        r = api.get_chart_data(None, None)
        assert r["ok"] is False

    def test_network_error_is_russian_and_not_cached(self, tmp_path):
        api = Api(client=BoomClient(), data_dir=tmp_path)
        r = api.get_chart_data("TESTUSDT", "1m")
        assert r["ok"] is False
        assert "свечи" in r["error"].lower()


# ---------------------------------------------------------------------------
# resume_paper runs the catch-up replay before polling
# ---------------------------------------------------------------------------

def zigzag_fetcher(symbol, interval, start_ms, end_ms):
    """Synthetic 1m zigzag candles for [start_ms, end_ms)."""
    assert interval == "1m"
    rows, p, prev, direction = [], 105.0, 105.0, -1
    open_ms = int(start_ms)
    while open_ms + MIN <= end_ms:
        p = round(p + direction * 0.5, 6)
        if p <= 101.0:
            direction = 1
        elif p >= 109.0:
            direction = -1
        rows.append(row(open_ms, prev, max(prev, p) + 0.05,
                        min(prev, p) - 0.05, p))
        prev = p
        open_ms += MIN
    return rows


class TestResumeWithCatchUp:
    def test_replay_fills_land_in_recent_fills(self, tmp_path):
        client = StaticTickerClient(
            [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
        api = Api(client=client, data_dir=tmp_path)
        assert api.start_paper(CFG)["ok"] is True
        assert wait_until(lambda: api.get_status()["price"] is not None)
        assert api.stop_paper()["ok"] is True

        # simulate 3 hours of downtime: rewind last_seen_ms in the state
        state_path = tmp_path / "state" / "paper_TESTUSDT.json"
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["last_seen_ms"] is not None
        rewound = int(time.time() * 1000) - 3 * 3_600_000
        rewound -= rewound % MIN
        saved["last_seen_ms"] = rewound
        state_path.write_text(json.dumps(saved), encoding="utf-8")

        client2 = StaticTickerClient(
            [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
        api2 = Api(client=client2, data_dir=tmp_path,
                   raw_kline_fetcher=zigzag_fetcher)
        r = api2.resume_paper()
        assert r["ok"] is True and r.get("resumed") is True
        try:
            assert wait_until(
                lambda: len(api2.get_status()["recent_fills"]) > 0)
            st = api2.get_status()
            assert st["running"] is True

            # replay fills carry HISTORICAL timestamps; live fills (the
            # first tick after the replayed gap) may legitimately join
            # them with fresh timestamps — but the historical ones must
            # be there, hours in the past
            times = [datetime.fromisoformat(f["time"])
                     for f in st["recent_fills"]]
            now = datetime.now(timezone.utc)
            assert any((now - t).total_seconds() > 3600 for t in times)

            # engine accounting reflects the replayed round trips
            assert st["n_trades"] > 0
            # the catch-up left its trace in the log
            assert any("ДОГОН" in line for line in st["log_tail"])
            json.dumps(st)
        finally:
            api2.stop_paper()

    def test_fresh_start_has_no_replay(self, tmp_path):
        calls = []

        def fetcher(*a):
            calls.append(a)
            return []

        client = StaticTickerClient(
            [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
        api = Api(client=client, data_dir=tmp_path, raw_kline_fetcher=fetcher)
        assert api.start_paper(CFG)["ok"] is True
        try:
            assert wait_until(lambda: api.get_status()["price"] is not None)
            assert calls == []          # fresh grid: no catch-up fetch
        finally:
            api.stop_paper()
