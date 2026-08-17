"""Backtester: feed cached candles chronologically into the grid engine.

No lookahead anywhere:
* the engine only ever sees candles up to "now";
* ``--auto-range`` derives [lower, upper] from the FIRST portion of the
  window only (default first 20% of candles) and then backtests on the
  REMAINDER — the traded data never influences the range choice.

CLI:
    python -m gridbot.backtest --symbol BTCUSDT --start 2025-01-01 \
        --end 2025-07-01 --lower 70000 --upper 110000 --levels 30 \
        --budget 1000 --interval 1m
    python -m gridbot.backtest --symbol BTCUSDT --auto-range ...
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .data import load_klines, read_cache
from .engine import GridConfig, GridEngine
from .exchange import fetch_filters_or_default

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


# ----------------------------------------------------------------------------
# range derivation (lookahead-safe)
# ----------------------------------------------------------------------------

def derive_range(df: pd.DataFrame, frac: float = 0.2, q_low: float = 0.05,
                 q_high: float = 0.95) -> tuple[float, float, int]:
    """[lower, upper] from the first ``frac`` of candles ONLY.

    Returns (lower, upper, n_calib) where ``n_calib`` is how many leading
    candles were consumed for calibration; trade only ``df.iloc[n_calib:]``.
    """
    n_calib = max(int(len(df) * frac), 2)
    head = df.iloc[:n_calib]
    lower = float(head["low"].quantile(q_low))
    upper = float(head["high"].quantile(q_high))
    if upper <= lower:
        raise ValueError("degenerate auto-range: upper <= lower")
    return lower, upper, n_calib


# ----------------------------------------------------------------------------
# core run
# ----------------------------------------------------------------------------

@dataclass
class BacktestReport:
    symbol: str
    n_candles: int
    start_time: str
    end_time: str
    start_price: float
    final_close: float
    initial_budget: float
    final_equity: float
    total_return_pct: float
    buy_hold_return_pct: float
    realized_grid_profit: float
    unrealized_pnl: float
    fees_paid: float
    max_drawdown_pct: float
    n_trades: int
    n_round_trips: int
    time_in_range_pct: float
    final_base_inventory: float
    final_cash: float
    logic: str = "classic"
    tp_mult: float = 1.0

    def summary(self) -> str:
        r = self
        lines = [
            "=" * 62,
            f"  GRID BACKTEST  {r.symbol}   "
            f"{r.start_time} -> {r.end_time}  ({r.n_candles} candles)",
            "=" * 62,
        ]
        # tp mode gets an extra line; classic output stays byte-identical
        if r.logic == "tp":
            lines.append(f"  logic              : buy-ladder + TP "
                         f"(RR={r.tp_mult:g} steps)")
        lines += [
            f"  start price        : {r.start_price:>14,.4f}",
            f"  final close        : {r.final_close:>14,.4f}",
            f"  initial budget     : {r.initial_budget:>14,.2f}",
            f"  final equity       : {r.final_equity:>14,.2f}",
            f"  total return       : {r.total_return_pct:>13,.2f}%",
            f"  buy & hold return  : {r.buy_hold_return_pct:>13,.2f}%",
            f"  realized grid PnL  : {r.realized_grid_profit:>14,.2f}",
            f"  unrealized PnL     : {r.unrealized_pnl:>14,.2f}",
            f"  fees paid          : {r.fees_paid:>14,.2f}",
            f"  max drawdown       : {r.max_drawdown_pct:>13,.2f}%",
            f"  trades / roundtrips: {r.n_trades:>8d} / {r.n_round_trips}",
            f"  time in range      : {r.time_in_range_pct:>13,.2f}%",
            f"  final inventory    : {r.final_base_inventory:>14,.6f} base"
            f"  + {r.final_cash:,.2f} quote",
            "=" * 62,
        ]
        return "\n".join(lines)


def run_backtest(df: pd.DataFrame, config: GridConfig
                 ) -> tuple[BacktestReport, pd.DataFrame]:
    """Chronological run; returns (report, equity_curve DataFrame)."""
    if df.empty:
        raise ValueError("no candles to backtest")
    engine = GridEngine(config)
    p0 = float(df.iloc[0]["open"])
    engine.start(df.iloc[0]["open_time"], p0)

    rows = []
    in_range = 0
    for t, hi, lo, cl in zip(df["open_time"], df["high"], df["low"],
                             df["close"]):
        engine.on_price(t, float(hi), float(lo), float(cl))
        eq = engine.equity(float(cl))
        if engine.status(float(cl)) == "in_range":
            in_range += 1
        rows.append((t, float(cl), eq, engine.realized_pnl,
                     engine.fees_paid, engine.n_round_trips))

    curve = pd.DataFrame(rows, columns=["open_time", "close", "equity",
                                        "realized_pnl", "fees_paid",
                                        "n_round_trips"])
    peak = curve["equity"].cummax()
    max_dd = float(((curve["equity"] - peak) / peak).min()) * 100.0

    final_close = float(df.iloc[-1]["close"])
    snap = engine.snapshot(final_close)
    report = BacktestReport(
        symbol=config.symbol,
        n_candles=len(df),
        start_time=str(df.iloc[0]["open_time"]),
        end_time=str(df.iloc[-1]["open_time"]),
        start_price=p0,
        final_close=final_close,
        initial_budget=config.quote_budget,
        final_equity=snap["equity"],
        total_return_pct=(snap["equity"] / config.quote_budget - 1) * 100.0,
        buy_hold_return_pct=(final_close / p0 - 1) * 100.0,
        realized_grid_profit=snap["realized_pnl"],
        unrealized_pnl=snap["unrealized_pnl"],
        fees_paid=snap["fees_paid"],
        max_drawdown_pct=max_dd,
        n_trades=snap["n_trades"],
        n_round_trips=snap["n_round_trips"],
        time_in_range_pct=100.0 * in_range / len(df),
        final_base_inventory=snap["base"],
        final_cash=snap["cash"],
        logic=config.logic,
        tp_mult=config.tp_mult,
    )
    return report, curve


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m gridbot.backtest",
                                description="Spot grid backtester")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--start", default=None,
                   help="UTC date, e.g. 2025-01-01 (default: 60 days ago)")
    p.add_argument("--end", default=None, help="UTC date (default: today)")
    p.add_argument("--interval", default="1m")
    p.add_argument("--lower", type=float, default=None)
    p.add_argument("--upper", type=float, default=None)
    p.add_argument("--levels", type=int, default=30)
    p.add_argument("--budget", type=float, default=1000.0)
    p.add_argument("--spacing", choices=["arithmetic", "geometric"],
                   default="arithmetic")
    p.add_argument("--logic", choices=["classic", "tp"], default="classic",
                   help="classic: paired buy<->sell levels; tp: buy-ladder "
                        "where each buy closes at its own TP (see --rr)")
    p.add_argument("--rr", type=float, default=3.0,
                   help="tp logic only: TP distance in grid steps "
                        "(RR, default 3.0)")
    p.add_argument("--fee", type=float, default=0.001)
    p.add_argument("--auto-range", action="store_true",
                   help="derive [lower, upper] from the FIRST part of the "
                        "window only, then trade the remainder (no lookahead)")
    p.add_argument("--range-frac", type=float, default=0.2,
                   help="fraction of leading candles used for --auto-range")
    p.add_argument("--data-file", default=None,
                   help="path to a cached/synthetic candles CSV "
                        "(skips download)")
    p.add_argument("--out-dir", default=str(REPORTS_DIR),
                   help="where to write the equity-curve CSV")
    p.add_argument("--offline", action="store_true",
                   help="never touch the network (requires --data-file or "
                        "an existing cache)")
    return p


def main(argv: list[str] | None = None) -> BacktestReport:
    args = build_parser().parse_args(argv)

    now = pd.Timestamp.utcnow().normalize()
    start = args.start or str((now - pd.Timedelta(days=60)).date())
    end = args.end or str(now.date())

    if args.data_file:
        df = read_cache(args.data_file)
    else:
        if args.offline:
            raise SystemExit("--offline requires --data-file")
        df = load_klines(args.symbol, args.interval, start, end)
    if df.empty:
        raise SystemExit("no data loaded — check symbol/dates/network")

    lower, upper = args.lower, args.upper
    if args.auto_range:
        lower, upper, n_calib = derive_range(df, frac=args.range_frac)
        print(f"[auto-range] calibrated on first {n_calib} candles: "
              f"lower={lower:,.2f} upper={upper:,.2f}; trading the "
              f"remaining {len(df) - n_calib} candles")
        df = df.iloc[n_calib:].reset_index(drop=True)
    if lower is None or upper is None:
        raise SystemExit("provide --lower/--upper or use --auto-range")

    if args.offline or args.data_file:
        from .exchange import default_filters
        filters, from_net = default_filters(args.symbol), False
    else:
        filters, from_net = fetch_filters_or_default(args.symbol)
    if not from_net:
        print("[filters] network unavailable or skipped — using offline "
              "defaults (tick 0.01 / step 1e-5 / minNotional 5)")

    config = GridConfig(
        symbol=args.symbol, lower_price=lower, upper_price=upper,
        n_levels=args.levels, spacing=args.spacing,
        quote_budget=args.budget, fee_rate=args.fee,
        min_notional=filters.min_notional, qty_step=filters.qty_step,
        price_step=filters.price_step,
        logic=args.logic,
        tp_mult=args.rr if args.logic == "tp" else 1.0,
    )

    report, curve = run_backtest(df, config)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / (f"equity_{args.symbol}_{args.interval}_"
                         f"{pd.Timestamp(start).strftime('%Y%m%d')}_"
                         f"{pd.Timestamp(end).strftime('%Y%m%d')}.csv")
    curve.to_csv(out_csv, index=False)

    print(report.summary())
    print(f"equity curve saved: {out_csv}")
    return report


if __name__ == "__main__":
    main()
