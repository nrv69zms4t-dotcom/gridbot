"""Historical kline download + local cache.

Downloads candles from the public Binance REST API (paginated, polite
rate-limit handling in the client) and caches them as CSV under
``gridbot/data_cache/``.  Loading returns a pandas DataFrame with
columns: open_time (UTC), open, high, low, close, volume.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd

from .exchange import BinancePublicClient, ExchangeClient

CACHE_DIR = Path(__file__).resolve().parent / "data_cache"

_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "6h": 21_600_000, "8h": 28_800_000, "12h": 43_200_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
}

COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]


def interval_ms(interval: str) -> int:
    try:
        return _INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(f"unsupported interval {interval!r}; "
                         f"one of {sorted(_INTERVAL_MS)}") from None


def _to_ms(ts: str | pd.Timestamp) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.timestamp() * 1000)


def cache_path(symbol: str, interval: str, start: str, end: str,
               cache_dir: Path | None = None) -> Path:
    d = Path(cache_dir) if cache_dir else CACHE_DIR
    s = pd.Timestamp(start).strftime("%Y%m%d")
    e = pd.Timestamp(end).strftime("%Y%m%d")
    return d / f"{symbol}_{interval}_{s}_{e}.csv"


def fetch_klines_raw(symbol: str, interval: str, start_ms: int, end_ms: int,
                     client: ExchangeClient | None = None,
                     page_pause: float = 0.25) -> list[list]:
    """Download [start_ms, end_ms) klines as RAW rows, paginated (1000/page).

    Millisecond bounds, no pandas, no cache — the hot path for the paper
    catch-up replay and any caller that parses rows directly.
    ``page_pause`` is a weight-aware pause between pages; 429s are handled
    inside the client (sleep on Retry-After).
    """
    cl = client or BinancePublicClient()
    step = interval_ms(interval)
    start_ms, end_ms = int(start_ms), int(end_ms)
    rows: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        page = cl.get_klines(symbol, interval, cursor, end_ms - 1, limit=1000)
        if not page:
            break
        rows.extend(page)
        last_open = int(page[-1][0])
        cursor = last_open + step
        if len(page) < 1000:
            break
        time.sleep(page_pause)
    return rows


def fetch_klines(symbol: str, interval: str, start: str, end: str,
                 client: ExchangeClient | None = None,
                 page_pause: float = 0.25) -> pd.DataFrame:
    """Download [start, end) klines, paginated (1000/page).

    Date-string bounds; see ``fetch_klines_raw`` for the underlying loop.
    """
    rows = fetch_klines_raw(symbol, interval, _to_ms(start), _to_ms(end),
                            client=client, page_pause=page_pause)

    if not rows:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = COLUMNS
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"),
                                     unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df = df.drop_duplicates(subset="open_time").sort_values("open_time")
    return df.reset_index(drop=True)


def load_klines(symbol: str, interval: str, start: str, end: str,
                client: ExchangeClient | None = None,
                cache_dir: Path | None = None,
                force_refresh: bool = False) -> pd.DataFrame:
    """Load candles from cache, downloading (and caching) if missing."""
    path = cache_path(symbol, interval, start, end, cache_dir)
    if path.exists() and not force_refresh:
        return read_cache(path)
    df = fetch_klines(symbol, interval, start, end, client=client)
    if not df.empty:
        path.parent.mkdir(parents=True, exist_ok=True)
        # atomic write: a crash mid-write must not leave a truncated cache
        tmp = path.with_suffix(".csv.tmp")
        df.to_csv(tmp, index=False)
        os.replace(tmp, path)
    return df


def read_cache(path: Path | str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df
