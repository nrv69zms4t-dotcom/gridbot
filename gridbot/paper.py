"""Paper trading: live public prices, simulated fills, persistent state.

Polls the public Binance bookTicker every N seconds (default 10) and feeds
the same ``GridEngine`` used by the backtester.  Fill trigger uses the best
bid/ask crossing a level:

* a resting BUY at p fills when ``ask <= p``  -> engine ``low``  := ask
* a resting SELL at p fills when ``bid >= p`` -> engine ``high`` := bid
* engine ``close`` := mid

(so ``high < low`` is normal here — the engine only compares bounds).

State is persisted to JSON after EVERY event, so a restart resumes
cleanly.  Every simulated fill and a periodic equity snapshot are logged
to ``gridbot/logs/paper_{symbol}.log``.

Offline catch-up: the state also stores ``last_seen_ms`` (time of the
last successful poll).  On resume, ``catch_up()`` downloads the 1m
candles for the downtime gap and replays them through the SAME honest
engine rules (orders created inside a candle cannot fill in it), so the
grid "lives through" the time the app was closed.

CLI:
    python -m gridbot.paper --symbol BTCUSDT --lower 70000 --upper 110000 \
        --levels 30 --budget 1000
    python -m gridbot.paper --dry-run     # one offline poll cycle, no network
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .data import fetch_klines_raw
from .engine import GridConfig, GridEngine
from .exchange import (BinancePublicClient, BookTicker, ExchangeClient,
                       default_filters, fetch_filters_or_default)

LOGS_DIR = Path(__file__).resolve().parent / "logs"
STATE_DIR = Path(__file__).resolve().parent / "state"

SNAPSHOT_EVERY = 30  # log an equity snapshot every N polls

#: catch-up replay only kicks in for gaps of at least this length
CATCH_UP_MIN_GAP_MS = 120_000
_MINUTE_MS = 60_000

#: stateless reusable no-op context manager (``catch_up`` without a lock)
_NULL_LOCK = contextlib.nullcontext()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_logger(symbol: str, log_dir: Path | None = None) -> logging.Logger:
    d = Path(log_dir) if log_dir else LOGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"gridbot.paper.{symbol}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s  %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(d / f"paper_{symbol}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


class PaperTrader:
    """Grid engine + live prices + JSON state persistence."""

    def __init__(self, config: GridConfig, client: ExchangeClient,
                 state_file: Path, logger: logging.Logger,
                 poll_seconds: float = 10.0):
        self.client = client
        self.state_file = Path(state_file)
        self.logger = logger
        self.poll_seconds = poll_seconds
        self._polls = 0
        #: unix ms of the last successfully processed price event;
        #: None on fresh grids and on old state files (no replay then)
        self.last_seen_ms: int | None = None

        if self.state_file.exists():
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.engine = GridEngine.from_dict(saved["engine"])
            raw_seen = saved.get("last_seen_ms")  # absent in old states
            self.last_seen_ms = int(raw_seen) if raw_seen is not None else None
            self.logger.info(
                "RESUME  state from %s (equity=%.2f, %d open orders)",
                self.state_file, self.engine.equity(),
                len(self.engine.orders))
            if saved["engine"]["config"] != config.to_dict():
                self.logger.info(
                    "NOTE    saved config differs from CLI args; the SAVED "
                    "config wins (delete the state file to start fresh)")
        else:
            self.engine = GridEngine(config)

    # ------------------------------------------------------------------ steps

    def ensure_started(self, ticker: BookTicker) -> None:
        if not self.engine.started:
            info = self.engine.start(_now_iso(), ticker.mid)
            self.logger.info(
                "START   %s @ %.4f | initial base buy %.8f (cost %.2f, "
                "fee %.4f) | %d open orders",
                self.engine.config.symbol, ticker.mid, info["initial_base"],
                info["cost"], info["fee"], len(self.engine.orders))
            self.save_state()

    def step(self, ticker: BookTicker) -> list:
        """One poll cycle: feed bid/ask, log fills, persist state."""
        self._polls += 1
        self.ensure_started(ticker)
        ts = _now_iso()
        fills = self.engine.on_price(ts, high=ticker.bid, low=ticker.ask,
                                     close=ticker.mid)
        for f in fills:
            if f.side == "BUY":
                self.logger.info(
                    "FILL    BUY  lvl %2d @ %.4f qty %.8f fee %.6f",
                    f.level, f.price, f.qty, f.fee)
            else:
                self.logger.info(
                    "FILL    SELL lvl %2d @ %.4f qty %.8f fee %.6f "
                    "round-trip pnl %.6f",
                    f.level, f.price, f.qty, f.fee, f.realized_net)
        if fills or self._polls % SNAPSHOT_EVERY == 1:
            s = self.engine.snapshot(ticker.mid)
            self.logger.info(
                "EQUITY  %.2f | cash %.2f | base %.8f | realized %.4f | "
                "fees %.4f | trips %d | %s",
                s["equity"], s["cash"], s["base"], s["realized_pnl"],
                s["fees_paid"], s["n_round_trips"], s["status"])
        self.last_seen_ms = int(time.time() * 1000)
        self.save_state()
        return fills

    # -------------------------------------------------------- offline catch-up

    def catch_up(self, kline_fetcher, lock=None) -> dict:
        """Replay the 1m candles for the downtime gap through the engine.

        ``kline_fetcher(symbol, "1m", start_ms, end_ms)`` must return raw
        Binance kline rows (index 0 open_time ms, 1-4 OHLC strings) —
        e.g. ``gridbot.data.fetch_klines_raw`` (paginated).  Only candles
        that are COMPLETED (open + 60s <= now) and start at or after
        ``last_seen_ms`` are replayed — the candle containing the last
        live poll was (partially) observed already and is skipped, so
        nothing is ever double-counted.  The standard honest engine rules
        apply automatically.

        ``lock`` (optional) guards the engine-mutation phase only — the
        slow network fetch happens WITHOUT it, so a UI status poller is
        never blocked by the download.

        Never raises: any failure (no network, bad rows) logs a warning
        по-русски and returns ``{"candles": 0, "fills": []}`` — replay
        failure must never prevent a normal resume.
        """
        empty = {"candles": 0, "fills": [], "from": None, "to": None}
        try:
            if not self.engine.started or self.last_seen_ms is None:
                return empty
            now_ms = int(time.time() * 1000)
            if now_ms - self.last_seen_ms < CATCH_UP_MIN_GAP_MS:
                return empty
            start_ms = self.last_seen_ms - self.last_seen_ms % _MINUTE_MS
            self.logger.info("ДОГОН   скачиваю свечи за время простоя…")
            rows = kline_fetcher(self.engine.config.symbol, "1m",
                                 start_ms, now_ms)
        except Exception as exc:
            self.logger.info("ДОГОН   не удалось скачать свечи за время "
                             "простоя: %s — продолжаю без догона", exc)
            return empty

        fills_out: list[dict] = []
        n_candles = 0
        first_open: int | None = None
        last_open: int | None = None
        cm = lock if lock is not None else _NULL_LOCK
        with cm:
            for row in rows:
                try:
                    open_ms = int(row[0])
                    high, low = float(row[2]), float(row[3])
                    close = float(row[4])
                except (TypeError, ValueError, IndexError):
                    continue  # malformed row: skip, keep replaying
                if open_ms < self.last_seen_ms:
                    continue  # candle (partially) observed live already
                if open_ms + _MINUTE_MS > now_ms:
                    continue  # incomplete candle: wait for it to close
                ts = datetime.fromtimestamp(
                    open_ms / 1000.0, tz=timezone.utc
                ).isoformat(timespec="seconds")
                try:
                    fills = self.engine.on_price(ts, high=high, low=low,
                                                 close=close)
                except Exception as exc:
                    self.logger.info("ДОГОН   ошибка движка на свече %s: %s "
                                     "— догон прерван", ts, exc)
                    break
                n_candles += 1
                if first_open is None:
                    first_open = open_ms
                last_open = open_ms
                for f in fills:
                    self.logger.info(
                        "ДОГОН   FILL %s lvl %2d @ %.4f qty %.8f",
                        f.side, f.level, f.price, f.qty)
                    fills_out.append({"time": str(f.timestamp),
                                      "side": f.side,
                                      "price": float(f.price),
                                      "qty": float(f.qty)})
            if last_open is not None:
                self.last_seen_ms = last_open + _MINUTE_MS
                self.save_state()

        def _iso(ms: int | None) -> str | None:
            if ms is None:
                return None
            return datetime.fromtimestamp(
                ms / 1000.0, tz=timezone.utc).isoformat(timespec="seconds")

        out = {"candles": n_candles, "fills": fills_out,
               "from": _iso(first_open),
               "to": _iso(None if last_open is None
                          else last_open + _MINUTE_MS)}
        self.logger.info(
            "ДОГОН   %d свечей, %d исполнений за время простоя (%s → %s)",
            n_candles, len(fills_out), out["from"] or "—", out["to"] or "—")
        return out

    def run(self) -> None:
        self.logger.info("paper trader polling every %.0fs (Ctrl+C to stop)",
                         self.poll_seconds)
        try:
            while True:
                try:
                    ticker = self.client.get_book_ticker(
                        self.engine.config.symbol)
                    self.step(ticker)
                except (NotImplementedError, KeyboardInterrupt):
                    raise
                except Exception as exc:  # network hiccup: log and retry
                    self.logger.info("WARN    poll failed: %s", exc)
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            self.logger.info("stopped by user; state saved at %s",
                             self.state_file)

    # ------------------------------------------------------------ persistence

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": _now_iso(),
                   "last_seen_ms": self.last_seen_ms,
                   "engine": self.engine.to_dict()}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)


# ----------------------------------------------------------------------------
# offline stub for --dry-run and tests
# ----------------------------------------------------------------------------

class StaticTickerClient(ExchangeClient):
    """Injectable client returning a scripted sequence of tickers."""

    def __init__(self, tickers: list[BookTicker]):
        self._tickers = list(tickers)
        self._i = 0

    def get_book_ticker(self, symbol: str) -> BookTicker:
        t = self._tickers[min(self._i, len(self._tickers) - 1)]
        self._i += 1
        return t

    def get_klines(self, *a, **k):  # pragma: no cover - not used offline
        raise RuntimeError("offline stub has no klines")

    def get_symbol_filters(self, symbol: str):
        return default_filters(symbol)

    def place_order(self, *a, **k):
        raise NotImplementedError(
            "Live trading intentionally not implemented — verify on paper "
            "first")

    def cancel_order(self, *a, **k):
        raise NotImplementedError(
            "Live trading intentionally not implemented — verify on paper "
            "first")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m gridbot.paper",
                                description="Grid paper trader "
                                            "(simulated fills)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--lower", type=float, default=None)
    p.add_argument("--upper", type=float, default=None)
    p.add_argument("--levels", type=int, default=30)
    p.add_argument("--budget", type=float, default=1000.0)
    p.add_argument("--spacing", choices=["arithmetic", "geometric"],
                   default="arithmetic")
    p.add_argument("--fee", type=float, default=0.001)
    p.add_argument("--poll", type=float, default=10.0,
                   help="seconds between bookTicker polls")
    p.add_argument("--state-file", default=None,
                   help="JSON state path (default "
                        "gridbot/state/paper_{symbol}.json)")
    p.add_argument("--dry-run", action="store_true",
                   help="one offline poll cycle with synthetic prices, "
                        "no network; then exit")
    return p


def main(argv: list[str] | None = None) -> PaperTrader:
    args = build_parser().parse_args(argv)
    state_file = Path(args.state_file) if args.state_file else (
        STATE_DIR / f"paper_{args.symbol}.json")
    logger = make_logger(args.symbol)

    if args.dry_run:
        lower = args.lower if args.lower is not None else 90.0
        upper = args.upper if args.upper is not None else 110.0
        mid = 0.5 * (lower + upper)
        client: ExchangeClient = StaticTickerClient(
            [BookTicker(args.symbol, bid=mid - 0.01, ask=mid + 0.01)])
        filters = default_filters(args.symbol)
    else:
        if args.lower is None or args.upper is None:
            raise SystemExit("provide --lower and --upper")
        lower, upper = args.lower, args.upper
        client = BinancePublicClient()
        filters, from_net = fetch_filters_or_default(args.symbol, client)
        if not from_net:
            logger.info("filters: network unavailable — offline defaults")

    config = GridConfig(
        symbol=args.symbol, lower_price=lower, upper_price=upper,
        n_levels=args.levels, spacing=args.spacing,
        quote_budget=args.budget, fee_rate=args.fee,
        min_notional=filters.min_notional, qty_step=filters.qty_step,
        price_step=filters.price_step,
    )

    trader = PaperTrader(config, client, state_file, logger,
                         poll_seconds=args.poll)
    if args.dry_run:
        ticker = client.get_book_ticker(args.symbol)
        trader.step(ticker)
        logger.info("dry-run OK: one poll cycle done, state at %s",
                    state_file)
        return trader
    if trader.engine.started:
        # resume from saved state: replay the candles missed while offline
        # (never fatal — catch_up logs and degrades to a plain resume)
        trader.catch_up(lambda s, i, a, b:
                        fetch_klines_raw(s, i, a, b, client=client))
    trader.run()
    return trader


if __name__ == "__main__":
    main()
