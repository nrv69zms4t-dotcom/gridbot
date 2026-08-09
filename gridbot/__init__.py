"""gridbot — spot crypto grid-trading bot (R&D).

Modules:
    engine    — pure grid state machine (no I/O)
    data      — Binance klines download + local cache
    backtest  — chronological backtester + CLI
    paper     — paper trading on live prices, simulated fills + CLI
    exchange  — exchange abstraction (read-only Binance public client;
                live order placement intentionally NOT implemented)
"""

__version__ = "0.1.0"
