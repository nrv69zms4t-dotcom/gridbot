"""Paper offline catch-up («догон по истории») — NO network.

Time is frozen via a fake ``time`` module injected into ``gridbot.paper``,
so the completed-candle boundary and the gap check are deterministic.
Synthetic kline rows mimic the raw Binance format (index 0 open_time ms,
1-4 OHLC strings).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import gridbot.paper as paper_mod
from gridbot.data import fetch_klines_raw
from gridbot.engine import GridEngine
from gridbot.exchange import BookTicker
from gridbot.paper import PaperTrader, StaticTickerClient, make_logger
from gridbot.tests.conftest import make_config

MIN = 60_000
HOUR = 3_600_000
#: aligned epoch minute (28_000_000 * 60_000 ms ~ 2023-03-28 UTC)
B = 28_000_000 * MIN


class FrozenTime:
    """Stand-in for the ``time`` module inside gridbot.paper."""

    def __init__(self, now_ms: int):
        self.now_ms = now_ms

    def time(self) -> float:
        return self.now_ms / 1000.0

    def sleep(self, seconds: float) -> None:  # pragma: no cover
        pass


@pytest.fixture
def frozen(monkeypatch) -> FrozenTime:
    ft = FrozenTime(B)
    monkeypatch.setattr(paper_mod, "time", ft)
    return ft


def row(open_ms: int, o: float, h: float, lo: float, c: float) -> list:
    """One raw kline row in the Binance shape (OHLC as strings)."""
    return [open_ms, str(o), str(h), str(lo), str(c), "1.0",
            open_ms + MIN - 1, "0", 0, "0", "0", "0"]


def zigzag_rows(start_ms: int, n: int, start_price: float = 105.0) -> list:
    """n 1m candles walking 105 -> 101 -> 109 -> ... (plenty of fills)."""
    rows, p, prev, direction = [], start_price, start_price, -1
    for k in range(n):
        p = round(p + direction * 0.5, 6)
        if p <= 101.0:
            direction = 1
        elif p >= 109.0:
            direction = -1
        rows.append(row(start_ms + k * MIN, prev, max(prev, p) + 0.05,
                        min(prev, p) - 0.05, p))
        prev = p
    return rows


def iso(ms: int) -> str:
    return datetime.fromtimestamp(
        ms / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")


def make_trader(tmp_path, name="catchup") -> PaperTrader:
    config = make_config()
    client = StaticTickerClient(
        [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
    logger = make_logger(config.symbol, log_dir=tmp_path / "logs")
    return PaperTrader(config, client, tmp_path / f"{name}.json", logger,
                       poll_seconds=0.0)


def started_trader(tmp_path, name="catchup") -> PaperTrader:
    """Trader with a started grid; last_seen_ms == frozen now."""
    trader = make_trader(tmp_path, name)
    trader.step(BookTicker("TESTUSDT", bid=104.99, ask=105.01))
    return trader


class RecordingFetcher:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple] = []

    def __call__(self, symbol, interval, start_ms, end_ms):
        self.calls.append((symbol, interval, start_ms, end_ms))
        return self.rows


# ---------------------------------------------------------------------------
# exact replay: fills and accounting identical to a live engine copy
# ---------------------------------------------------------------------------

class TestReplayIdentity:
    def test_3h_zigzag_equals_direct_engine_feed(self, tmp_path, frozen):
        trader = started_trader(tmp_path)
        assert trader.last_seen_ms == B

        # reference: an exact engine copy fed the same candles directly
        ref = GridEngine.from_dict(
            json.loads(json.dumps(trader.engine.to_dict())))

        frozen.now_ms = B + 3 * HOUR
        rows = zigzag_rows(B, 180)          # 3h of completed 1m candles
        fetcher = RecordingFetcher(rows)
        rep = trader.catch_up(fetcher)

        assert fetcher.calls == [("TESTUSDT", "1m", B, B + 3 * HOUR)]
        assert rep["candles"] == 180
        assert rep["from"] == iso(B)
        assert rep["to"] == iso(B + 180 * MIN)
        assert trader.last_seen_ms == B + 180 * MIN

        ref_fills = []
        for r in rows:
            fills = ref.on_price(iso(int(r[0])), high=float(r[2]),
                                 low=float(r[3]), close=float(r[4]))
            ref_fills.extend(
                {"time": str(f.timestamp), "side": f.side,
                 "price": float(f.price), "qty": float(f.qty)}
                for f in fills)
        assert ref_fills, "zigzag must produce fills"
        assert rep["fills"] == ref_fills

        # full state identity + the exact accounting identity
        close = float(rows[-1][4])
        assert trader.engine.snapshot(close) == ref.snapshot(close)
        snap = trader.engine.snapshot(close)
        lhs = snap["cash"] + snap["base"] * close
        rhs = (trader.engine.config.quote_budget
               + trader.engine.realized_pnl_gross
               + snap["unrealized_pnl"] - snap["fees_paid"])
        assert abs(lhs - rhs) < 1e-6

        # fills carry HISTORICAL candle timestamps
        assert rep["fills"][0]["time"].startswith(iso(B)[:10])

    def test_state_saved_after_replay(self, tmp_path, frozen):
        trader = started_trader(tmp_path)
        frozen.now_ms = B + 3 * HOUR
        trader.catch_up(RecordingFetcher(zigzag_rows(B, 180)))

        saved = json.loads(trader.state_file.read_text(encoding="utf-8"))
        assert saved["last_seen_ms"] == B + 180 * MIN
        restored = make_trader(tmp_path)     # same state file
        assert restored.last_seen_ms == B + 180 * MIN
        assert restored.engine.snapshot(105.0) == \
            trader.engine.snapshot(105.0)


# ---------------------------------------------------------------------------
# boundaries: nothing is ever replayed twice, incomplete candles wait
# ---------------------------------------------------------------------------

class TestBoundaries:
    def test_partially_seen_candle_not_reprocessed(self, tmp_path, frozen):
        # last poll happened MID-candle: the candle containing it was
        # (partially) observed live and must NOT be replayed
        frozen.now_ms = B + 30_000
        trader = started_trader(tmp_path)
        assert trader.last_seen_ms == B + 30_000

        frozen.now_ms = B + 3 * HOUR
        rows = [
            row(B, 105.0, 105.1, 100.9, 101.0),        # would fill 4 buys
            row(B + MIN, 105.0, 105.05, 103.95, 104.5),  # fills BUY @ 104
        ]
        rep = trader.catch_up(RecordingFetcher(rows))
        assert rep["candles"] == 1                     # candle at B skipped
        assert [(f["side"], f["price"]) for f in rep["fills"]] == \
            [("BUY", 104.0)]
        assert trader.last_seen_ms == B + 2 * MIN

    def test_candle_closing_at_last_seen_not_reprocessed(self, tmp_path,
                                                         frozen):
        # last_seen sits exactly on a minute boundary (a previous replay
        # ended there): the candle CLOSING at last_seen is old news, the
        # candle OPENING at last_seen is new and must be replayed
        trader = started_trader(tmp_path)          # last_seen == B exactly
        frozen.now_ms = B + 3 * HOUR
        rows = [
            row(B - MIN, 105.0, 105.1, 100.9, 101.0),  # closed AT last_seen
            row(B, 105.0, 105.05, 103.95, 104.5),      # opens at last_seen
        ]
        rep = trader.catch_up(RecordingFetcher(rows))
        assert rep["candles"] == 1
        assert [(f["side"], f["price"]) for f in rep["fills"]] == \
            [("BUY", 104.0)]

    def test_incomplete_last_candle_excluded(self, tmp_path, frozen):
        trader = started_trader(tmp_path)
        now = B + 3 * HOUR + 30_000                # mid-minute "now"
        frozen.now_ms = now
        floor_now = now - now % MIN
        rows = [
            row(floor_now - MIN, 105.0, 105.05, 103.95, 104.5),  # complete
            row(floor_now, 104.5, 104.6, 100.9, 101.0),  # STILL OPEN
        ]
        rep = trader.catch_up(RecordingFetcher(rows))
        assert rep["candles"] == 1
        assert [(f["side"], f["price"]) for f in rep["fills"]] == \
            [("BUY", 104.0)]                       # no fills from the open one
        assert trader.last_seen_ms == floor_now    # close of the complete one


# ---------------------------------------------------------------------------
# small gaps and failures never disturb a normal resume
# ---------------------------------------------------------------------------

class TestNoReplayPaths:
    def test_gap_below_two_minutes_never_fetches(self, tmp_path, frozen):
        trader = started_trader(tmp_path)
        frozen.now_ms = B + 119_000                # 119 s < 120 s
        fetcher = RecordingFetcher([])
        rep = trader.catch_up(fetcher)
        assert rep == {"candles": 0, "fills": [], "from": None, "to": None}
        assert fetcher.calls == []                 # fetcher MUST NOT be hit

    def test_fetcher_error_means_clean_resume(self, tmp_path, frozen):
        trader = started_trader(tmp_path)
        frozen.now_ms = B + 3 * HOUR

        def boom(*a):
            raise RuntimeError("нет сети")

        rep = trader.catch_up(boom)
        assert rep["candles"] == 0 and rep["fills"] == []
        # normal polling continues to work after the failed replay
        fills = trader.step(BookTicker("TESTUSDT", bid=103.9, ask=104.0))
        assert [(f.side, f.price) for f in fills] == [("BUY", 104.0)]

    def test_empty_rows_is_noop(self, tmp_path, frozen):
        trader = started_trader(tmp_path)
        before = trader.engine.snapshot(105.0)
        frozen.now_ms = B + 3 * HOUR
        rep = trader.catch_up(RecordingFetcher([]))
        assert rep["candles"] == 0 and rep["fills"] == []
        assert trader.engine.snapshot(105.0) == before

    def test_old_state_without_last_seen_skips_replay(self, tmp_path,
                                                      frozen):
        trader = started_trader(tmp_path)
        payload = json.loads(trader.state_file.read_text(encoding="utf-8"))
        del payload["last_seen_ms"]                # simulate an OLD state
        trader.state_file.write_text(json.dumps(payload), encoding="utf-8")

        restored = make_trader(tmp_path)
        assert restored.last_seen_ms is None
        frozen.now_ms = B + 3 * HOUR
        fetcher = RecordingFetcher(zigzag_rows(B, 180))
        rep = restored.catch_up(fetcher)
        assert rep["candles"] == 0 and fetcher.calls == []

    def test_not_started_engine_skips_replay(self, tmp_path, frozen):
        trader = make_trader(tmp_path)             # grid never started
        fetcher = RecordingFetcher([])
        assert trader.catch_up(fetcher)["candles"] == 0
        assert fetcher.calls == []


# ---------------------------------------------------------------------------
# pagination: long gaps are downloaded page by page
# ---------------------------------------------------------------------------

class PagingClient(StaticTickerClient):
    """Offline client whose get_klines serves rows in 1000-row pages."""

    def __init__(self, rows):
        super().__init__(
            [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
        self.rows = rows
        self.kline_calls: list[tuple] = []

    def get_klines(self, symbol, interval, start_ms, end_ms, limit=1000):
        self.kline_calls.append((start_ms, end_ms, limit))
        return [r for r in self.rows
                if start_ms <= r[0] <= end_ms][:limit]


class TestPagination:
    def test_fetch_klines_raw_two_pages(self):
        rows = zigzag_rows(B, 1500)
        client = PagingClient(rows)
        out = fetch_klines_raw("TESTUSDT", "1m", B, B + 1500 * MIN,
                               client=client, page_pause=0.0)
        assert len(out) == 1500
        assert out == rows
        assert len(client.kline_calls) == 2
        # second page starts right after the last row of the first page
        assert client.kline_calls[1][0] == B + 1000 * MIN

    def test_catch_up_over_paginated_fetcher(self, tmp_path, frozen):
        trader = started_trader(tmp_path)
        ref = GridEngine.from_dict(
            json.loads(json.dumps(trader.engine.to_dict())))

        frozen.now_ms = B + 1500 * MIN             # 25-hour gap
        rows = zigzag_rows(B, 1500)
        client = PagingClient(rows)
        rep = trader.catch_up(
            lambda s, i, a, b: fetch_klines_raw(s, i, a, b, client=client,
                                                page_pause=0.0))
        assert rep["candles"] == 1500
        assert len(client.kline_calls) == 2        # really paginated
        for r in rows:
            ref.on_price(iso(int(r[0])), high=float(r[2]),
                         low=float(r[3]), close=float(r[4]))
        close = float(rows[-1][4])
        assert trader.engine.snapshot(close) == ref.snapshot(close)
