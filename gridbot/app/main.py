"""GridBot app entry point.

CLI:
    python -m gridbot.app             # open the window
    python -m gridbot.app --debug     # window with webview devtools
    python -m gridbot.app --smoke     # open the window for ~5 s, then exit 0
    python -m gridbot.app --selftest  # NO window, NO prints: run the engine
                                      # on synthetic candles, verify the
                                      # accounting identity, write JSON
                                      # {"ok": ..., "detail": ...} to
                                      # %TEMP%/gridbot_selftest.json,
                                      # exit 0 (ok) / 1 (failure)

The exe is windowed (console=False), so the selftest path must never
print to stdout/stderr — the JSON file in TEMP is the only output.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
from pathlib import Path
from typing import Optional

from . import paths

SELFTEST_FILENAME = "gridbot_selftest.json"
SMOKE_SECONDS = 5.0


# ---------------------------------------------------------------------------
# selftest (no window, no network, no prints)
# ---------------------------------------------------------------------------

def _zigzag_prices(start: float, targets: list[float],
                   step: float = 0.5) -> list[float]:
    """Deterministic zigzag path visiting each target in turn."""
    prices: list[float] = []
    prev = start
    for target in targets:
        d = step if target > prev else -step
        while abs(prev - target) > 1e-9:
            prev = round(prev + d, 6)
            prices.append(prev)
    return prices


def run_selftest(data_dir: Optional[str] = None) -> dict:
    """Initialize the Api, run the engine on synthetic candles and check
    the exact accounting identity:

        cash + base*close == budget + realized_gross + unrealized - fees

    Returns {"ok": bool, "detail": {...}} — never raises.
    """
    detail: dict = {}
    try:
        from ..engine import GridConfig, GridEngine
        from .api import Api

        api = Api(data_dir=data_dir)
        defaults = api.get_defaults()
        detail["defaults_ok"] = bool(defaults.get("ok"))
        status = api.get_status()
        detail["status_ok"] = bool(status.get("ok"))

        cfg = GridConfig(
            symbol="SELFTEST", lower_price=100.0, upper_price=110.0,
            n_levels=11, spacing="arithmetic", quote_budget=1100.0,
            fee_rate=0.001, min_notional=1.0, qty_step=0.001,
            price_step=0.01)
        eng = GridEngine(cfg)
        eng.start("t0", 105.0)

        prices = _zigzag_prices(
            105.0, [101.0, 109.0, 101.0, 109.0, 103.0])
        prev = 105.0
        max_err = 0.0
        for i, p in enumerate(prices):
            hi, lo = max(prev, p), min(prev, p)
            eng.on_price(f"t{i + 1}", hi, lo, p)
            snap = eng.snapshot(p)
            lhs = snap["cash"] + snap["base"] * p
            rhs = (cfg.quote_budget + eng.realized_pnl_gross
                   + snap["unrealized_pnl"] - snap["fees_paid"])
            max_err = max(max_err, abs(lhs - rhs))
            prev = p

        detail.update({
            "n_candles": len(prices),
            "n_trades": eng.n_trades,
            "n_round_trips": eng.n_round_trips,
            "max_identity_error": max_err,
        })
        ok = (detail["defaults_ok"] and detail["status_ok"]
              and eng.n_trades > 0 and eng.n_round_trips > 0
              and max_err < 1e-6)
        return {"ok": bool(ok), "detail": detail}
    except Exception as exc:
        detail["exception"] = f"{type(exc).__name__}: {exc}"
        return {"ok": False, "detail": detail}


def _selftest_main() -> int:
    result = run_selftest()
    out = Path(tempfile.gettempdir()) / SELFTEST_FILENAME
    try:
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    except Exception:
        return 1
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------

def run_app(debug: bool = False, smoke: bool = False) -> int:
    import webview  # imported lazily: --selftest must not need a GUI stack

    from .api import Api

    api = Api()
    index = paths.web_dir() / "index.html"
    window = webview.create_window(
        "GridBot — спотовый грид-бот",
        str(index),
        js_api=api,
        width=1280, height=840,
        min_size=(1000, 700),
    )
    if smoke:
        timer = threading.Timer(SMOKE_SECONDS, window.destroy)
        timer.daemon = True
        timer.start()
    webview.start(debug=debug)
    # window closed — stop the paper thread gracefully (state is saved
    # after every step anyway)
    try:
        api.stop_paper()
    except Exception:
        pass
    # live: stop polling but KEEP orders on the exchange (closing the
    # window is not consent to cancel them; state persists, so the next
    # start recovers cleanly)
    try:
        api.stop_live(cancel_orders=False)
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m gridbot.app",
                                description="GridBot — спотовый грид-бот "
                                            "(paper), настольное приложение")
    p.add_argument("--selftest", action="store_true",
                   help="headless engine check; JSON result in TEMP; "
                        "exit 0/1")
    p.add_argument("--debug", action="store_true",
                   help="open webview devtools")
    p.add_argument("--smoke", action="store_true",
                   help="open the window for ~5 s, then close and exit 0")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.selftest:
        return _selftest_main()
    return run_app(debug=args.debug, smoke=args.smoke)


if __name__ == "__main__":
    raise SystemExit(main())
