"""Backend bridge for the GridBot desktop app (pywebview ``js_api``).

Design rules (contract with the frontend):
* every public method returns a JSON-serializable ``dict`` and NEVER
  raises — on failure it returns ``{"ok": False, "error": "<по-русски>"}``;
* all mutable state is guarded by ``threading.RLock``;
* the paper trader and the backtest each run in their own background
  thread; the UI polls ``get_status()`` / ``get_backtest_status()``.

The heavy lifting is done by the existing gridbot modules (engine,
paper, backtest, data, exchange) — this layer only orchestrates them.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from ..backtest import run_backtest as _run_backtest_core
from ..data import fetch_klines_raw, load_klines
from ..engine import GridConfig, GridEngine
from ..exchange import (BINANCE_BASE, TESTNET_BASE, BinancePublicClient,
                        BinanceSignedClient, ExchangeClient, SymbolFilters,
                        default_filters, fetch_filters_or_default)
from ..live import LiveStopped, LiveTrader, split_symbol
from ..paper import PaperTrader
from . import paths, secrets_store

DEFAULT_CONFIG = {
    "symbol": "BTCUSDT",
    "lower": None,
    "upper": None,
    "levels": 20,
    "spacing": "arithmetic",
    "budget": 1000.0,
    "fee": 0.001,          # fraction, not percent
    "poll": 10.0,          # seconds
    "grid_mode": "manual",  # "manual" (lower/upper) | "step" (price+step)
    "step_pct": 0.5,       # level step in % for grid_mode == "step"
    "logic": "classic",    # "classic" (pairs) | "tp" (buy-ladder + TP/RR)
    "rr": 3.0,             # logic == "tp": TP distance in grid steps
}

# --- "tp" grid logic (RR bounds mirror GridConfig.tp_mult validation) ---
RR_MIN = 0.5
RR_MAX = 50.0

BACKTEST_DAYS = 60
SUGGEST_DAYS = 30
MAX_CURVE_POINTS = 1000
PRICE_HISTORY_LEN = 300
RECENT_FILLS_LEN = 50
LOG_TAIL_LEN = 20

# --- "price + step" grid mode (compute_range) ---
STEP_PCT_MIN = 0.05
STEP_PCT_MAX = 10.0
RANGE_LEVELS_MIN = 2
RANGE_LEVELS_MAX = 200

# --- instrument picker (list_symbols) ---
SYMBOLS_CACHE_FILE = "symbols_usdt.json"
SYMBOLS_CACHE_TTL_S = 24 * 3600.0
#: Offline last resort: popular USDT spot pairs (order = popularity).
BUILTIN_USDT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "TONUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "TRXUSDT",
    "POLUSDT", "LTCUSDT", "NEARUSDT", "ATOMUSDT", "UNIUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "SUIUSDT", "PEPEUSDT", "SHIBUSDT", "FILUSDT",
    "INJUSDT", "RENDERUSDT", "WIFUSDT", "TIAUSDT", "SEIUSDT", "ETCUSDT",
]

# --- candlestick chart feed ---
CHART_CANDLES = 300            # candles returned to the UI
CHART_THROTTLE_S = 5.0         # per (symbol, interval): no HTTP more often
CHART_INTERVALS = {"1m": 60_000, "5m": 300_000, "15m": 900_000,
                   "1h": 3_600_000}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _num(v) -> Optional[float]:
    """Value as a finite float, else None (bool is NOT a number here)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _round_sig(x: float, sig: int = 6) -> float:
    """Round to ``sig`` significant digits (for suggested ranges)."""
    try:
        return float(f"{float(x):.{sig}g}")
    except (TypeError, ValueError):
        return x


def _downsample_idx(n: int, max_pts: int) -> list[int]:
    if n <= max_pts:
        return list(range(n))
    step = (n - 1) / (max_pts - 1)
    idx = {round(i * step) for i in range(max_pts)}
    idx.add(n - 1)
    return sorted(idx)


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _humanize_net_error(exc, generic: Optional[str] = None) -> str:
    """Russian human message for a network/exchange failure.

    HTTP 451 means Binance blocks the region: public market data still
    works through the official mirror, but SIGNED (trading) endpoints
    have no mirror — say so explicitly.  Otherwise return ``generic``
    (the caller's usual message) unchanged.
    """
    if "451" in str(exc):
        return ("Binance ограничивает доступ из вашего региона "
                "(ошибка 451). Рыночные данные работают через зеркало, "
                "но торговые операции (live) с этой сети недоступны.")
    if generic:
        return generic
    return f"Сетевая ошибка: {exc}"


def _net_hint(exc) -> str:
    """Short suffix for PUBLIC-data errors (rare after the fallback)."""
    if "451" in str(exc):
        return (" Binance отвечает 451 (регион): используется зеркало "
                "данных data-api.binance.vision.")
    return ""


class _RingHandler(logging.Handler):
    """Logging handler that appends formatted lines to a shared deque."""

    def __init__(self, buf: deque):
        super().__init__()
        self._buf = buf

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            self._buf.append(self.format(record))
        except Exception:
            pass


class Api:
    """pywebview js_api object.

    Injection points (used by tests, defaults are the real thing):
    * ``client``       — ``ExchangeClient`` (default ``BinancePublicClient``)
    * ``kline_loader`` — ``(symbol, interval, start, end) -> DataFrame``
                         (default: ``gridbot.data.load_klines`` with cache)
    * ``raw_kline_fetcher`` — ``(symbol, interval, start_ms, end_ms) ->
                         list[raw kline rows]`` for the paper catch-up
                         replay (default: ``gridbot.data.fetch_klines_raw``)
    * ``data_dir``     — base folder for state/logs/cache/config
                         (default: ``paths.base_data_dir()``)
    """

    def __init__(self,
                 client: Optional[ExchangeClient] = None,
                 kline_loader: Optional[Callable[..., pd.DataFrame]] = None,
                 data_dir: Optional[str | Path] = None,
                 signed_client_factory: Optional[Callable] = None,
                 raw_kline_fetcher: Optional[Callable[..., list]] = None):
        self._lock = threading.RLock()
        self._client: ExchangeClient = client or BinancePublicClient()
        self._base_dir = Path(data_dir) if data_dir else paths.base_data_dir()
        self._kline_loader = kline_loader or self._default_loader
        # (symbol, interval, start_ms, end_ms) -> raw rows; None means
        # "use fetch_klines_raw with the paper thread's own client"
        self._raw_kline_fetcher = raw_kline_fetcher
        # (symbol, interval) -> (monotonic_ts, ok-result) throttle cache
        self._chart_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        # (network, cancel_event) -> signed client; injectable for tests
        self._signed_client_factory = (signed_client_factory
                                       or self._default_signed_factory)

        # --- paper state ---
        self._trader: Optional[PaperTrader] = None
        self._paper_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # --- live state ---
        self._live_trader: Optional[LiveTrader] = None
        self._live_thread: Optional[threading.Thread] = None
        self._live_stop_event = threading.Event()
        self._live_cancel_orders = True
        self._live_network: Optional[str] = None
        self._last_mode: str = "paper"   # which engine get_status shows
                                         # when nothing is running
        self._price_history: deque = deque(maxlen=PRICE_HISTORY_LEN)
        self._recent_fills: deque = deque(maxlen=RECENT_FILLS_LEN)
        self._log_buf: deque = deque(maxlen=200)
        self._last_price: Optional[float] = None
        self._last_symbol: Optional[str] = None
        self._last_tick_ts: Optional[float] = None  # last successful poll
        self._net_error: Optional[str] = None       # current poll failure
        self._last_error: Optional[str] = None      # why paper stopped

        # --- backtest state (separate lock: download can be slow) ---
        self._bt_lock = threading.Lock()
        self._bt_thread: Optional[threading.Thread] = None
        self._bt_running = False
        self._bt_report: Optional[dict] = None
        self._bt_error: Optional[str] = None

    # ------------------------------------------------------------- plumbing

    def _dir(self, name: str) -> Path:
        d = self._base_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def _config_path(self) -> Path:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        return self._base_dir / "config.json"

    def _default_loader(self, symbol: str, interval: str, start: str,
                        end: str) -> pd.DataFrame:
        return load_klines(symbol, interval, start, end,
                           client=self._client,
                           cache_dir=self._dir("data_cache"))

    def _paper_running(self) -> bool:
        t = self._paper_thread
        return bool(t and t.is_alive())

    def _live_running(self) -> bool:
        t = self._live_thread
        return bool(t and t.is_alive())

    def _default_signed_factory(self, network: str,
                                cancel_event: threading.Event
                                ) -> BinanceSignedClient:
        creds = secrets_store.load_credentials(self._base_dir)
        if creds is None:
            raise RuntimeError("API-ключи не сохранены.")
        base = TESTNET_BASE if network == "testnet" else BINANCE_BASE
        return BinanceSignedClient(creds[0], creds[1], base_url=base,
                                   cancel_event=cancel_event)

    def _find_saved_state(self) -> Optional[tuple[str, Path]]:
        """Newest saved paper state as (symbol, path), or None."""
        try:
            state_dir = self._base_dir / "state"
            if not state_dir.exists():
                return None
            files = sorted(state_dir.glob("paper_*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if not files:
                return None
            p = files[0]
            return p.stem[len("paper_"):], p
        except Exception:
            return None

    def _save_config(self, cfg: dict) -> None:
        try:
            out = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
            self._config_path.write_text(
                json.dumps(out, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception:
            pass  # autosave is best-effort, never blocks the action

    def _make_logger(self, symbol: str, kind: str = "paper"
                     ) -> logging.Logger:
        logger = logging.getLogger(f"gridbot.app.{kind}.{symbol}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        fmt = logging.Formatter("%(asctime)s  %(message)s",
                                datefmt="%H:%M:%S")
        try:
            fh = logging.FileHandler(
                self._dir("logs") / f"{kind}_{symbol}.log", encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception:
            pass  # no file logging is not fatal
        rb = _RingHandler(self._log_buf)
        rb.setFormatter(fmt)
        logger.addHandler(rb)
        return logger

    # ----------------------------------------------------------- validation

    def _filters_for(self, symbol: str) -> SymbolFilters:
        try:
            filters, _ = fetch_filters_or_default(symbol, self._client)
            return filters
        except Exception:
            return default_filters(symbol)

    def _validate(self, cfg: dict, filters: SymbolFilters
                  ) -> tuple[Optional[GridConfig], float, Optional[str]]:
        """Returns (GridConfig | None, poll_seconds, error | None)."""
        if not isinstance(cfg, dict):
            return None, 0.0, "Некорректный формат настроек."
        symbol = str(cfg.get("symbol") or "").strip().upper()
        if not symbol:
            return None, 0.0, ("Укажите торговую пару (символ), "
                               "например BTCUSDT.")
        lower = _num(cfg.get("lower"))
        upper = _num(cfg.get("upper"))
        if lower is None or upper is None or lower <= 0 or upper <= 0:
            return None, 0.0, ("Нижняя и верхняя границы должны быть "
                               "положительными числами.")
        if lower >= upper:
            return None, 0.0, "Нижняя граница должна быть меньше верхней."
        levels_raw = cfg.get("levels")
        levels_f = _num(levels_raw)
        if levels_f is None or int(levels_f) != levels_f or int(levels_f) < 2:
            return None, 0.0, ("Число уровней должно быть целым числом "
                               "не меньше 2.")
        levels = int(levels_f)
        spacing = cfg.get("spacing", "arithmetic")
        if spacing not in ("arithmetic", "geometric"):
            return None, 0.0, ("Шаг сетки: выберите арифметический или "
                               "геометрический.")
        budget = _num(cfg.get("budget"))
        if budget is None or budget <= 0:
            return None, 0.0, "Бюджет должен быть положительным числом."
        fee = _num(cfg.get("fee", DEFAULT_CONFIG["fee"]))
        if fee is None or not (0 <= fee < 0.1):
            return None, 0.0, "Комиссия должна быть в диапазоне от 0 до 10%."
        poll = _num(cfg.get("poll", DEFAULT_CONFIG["poll"]))
        if poll is None or poll < 1.0:
            return None, 0.0, ("Интервал опроса должен быть числом "
                               "не меньше 1 секунды.")
        logic = str(cfg.get("logic", DEFAULT_CONFIG["logic"]) or "classic")
        if logic not in ("classic", "tp"):
            return None, 0.0, ("Логика сетки: выберите классическую или "
                               "buy-сетку с TP (RR).")
        rr = _num(cfg.get("rr", DEFAULT_CONFIG["rr"]))
        if logic == "tp" and (rr is None or not (RR_MIN <= rr <= RR_MAX)):
            return None, 0.0, (f"RR должен быть от {RR_MIN:g} до "
                               f"{RR_MAX:g} шагов сетки.")
        try:
            gc = GridConfig(
                symbol=symbol, lower_price=lower, upper_price=upper,
                n_levels=levels, spacing=spacing, quote_budget=budget,
                fee_rate=fee, min_notional=filters.min_notional,
                qty_step=filters.qty_step, price_step=filters.price_step,
                logic=logic, tp_mult=(rr if logic == "tp" else 1.0))
            GridEngine(gc)  # validates min_notional and level spacing
        except ValueError as exc:
            msg = str(exc)
            if "tp_mult" in msg:  # defensive: pre-validated above
                return None, 0.0, (f"RR должен быть от {RR_MIN:g} до "
                                   f"{RR_MAX:g} шагов сетки.")
            if "min_notional" in msg:
                return None, 0.0, (
                    "Бюджет слишком мал для такого числа уровней: объём на "
                    "уровне меньше минимального ордера биржи "
                    f"({filters.min_notional:g} USDT). Увеличьте бюджет или "
                    "уменьшите число уровней.")
            if "collapse" in msg or "price_step too coarse" in msg:
                return None, 0.0, (
                    "Слишком много уровней для такого диапазона: соседние "
                    "уровни сливаются. Уменьшите число уровней или "
                    "расширьте диапазон.")
            return None, 0.0, f"Некорректная конфигурация: {msg}"
        return gc, float(poll), None

    # ========================================================== public API

    # ------------------------------------------------------------- defaults

    def get_defaults(self) -> dict:
        try:
            cfg = dict(DEFAULT_CONFIG)
            p = self._config_path
            if p.exists():
                saved = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    cfg.update({k: saved[k] for k in DEFAULT_CONFIG
                                if k in saved})
            state = self._find_saved_state()
            with self._lock:
                running = self._paper_running()
            return {"ok": True,
                    "config": cfg,
                    "has_saved_state": bool(state) and not running,
                    "saved_state_symbol": state[0] if state else None}
        except Exception as exc:
            return _err(f"Не удалось прочитать сохранённые настройки: {exc}")

    # --------------------------------------------------------------- market

    def fetch_market(self, symbol: str) -> dict:
        try:
            symbol = str(symbol or "").strip().upper()
            if not symbol:
                return _err("Укажите торговую пару (символ).")
            t = self._client.get_book_ticker(symbol)
            try:
                f = self._client.get_symbol_filters(symbol)
                from_net = True
            except Exception:
                f = default_filters(symbol)
                from_net = False
            with self._lock:
                # never clobber the running trader's symbol/price pair
                if not self._paper_running():
                    self._last_price = float(t.mid)
                    self._last_symbol = symbol
            return {"ok": True, "symbol": symbol,
                    "bid": float(t.bid), "ask": float(t.ask),
                    "price": float(t.mid),
                    "filters": {"price_step": float(f.price_step),
                                "qty_step": float(f.qty_step),
                                "min_notional": float(f.min_notional),
                                "from_network": from_net}}
        except Exception as exc:
            return _err("Не удалось получить данные с биржи. Проверьте "
                        "интернет-соединение и правильность символа."
                        f"{_net_hint(exc)} (детали: {exc})")

    def get_chart_data(self, symbol: str, interval: str) -> dict:
        """Last ~300 candles for the UI candlestick chart.

        Hot path: parses raw kline rows directly (no DataFrame, no disk
        cache).  Per (symbol, interval) the HTTP call is throttled to one
        per ``CHART_THROTTLE_S`` seconds — repeat calls inside the window
        return the cached result.
        """
        try:
            symbol = str(symbol or "").strip().upper()
            if not symbol:
                return _err("Укажите торговую пару (символ).")
            interval = str(interval or "").strip()
            if interval not in CHART_INTERVALS:
                return _err("Неизвестный интервал графика: выберите "
                            "1m, 5m, 15m или 1h.")
            key = (symbol, interval)
            now_mono = time.monotonic()
            with self._lock:
                cached = self._chart_cache.get(key)
                if cached and now_mono - cached[0] < CHART_THROTTLE_S:
                    return cached[1]
            # HTTP outside the lock: a slow fetch must not block get_status
            step_ms = CHART_INTERVALS[interval]
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - step_ms * CHART_CANDLES
            rows = self._client.get_klines(symbol, interval, start_ms,
                                           end_ms, limit=1000)
            candles = []
            for r in rows:
                candles.append({"time": int(int(r[0]) // 1000),
                                "open": float(r[1]), "high": float(r[2]),
                                "low": float(r[3]), "close": float(r[4])})
            candles = candles[-CHART_CANDLES:]
            out = {"ok": True, "symbol": symbol, "interval": interval,
                   "candles": candles}
            with self._lock:
                self._chart_cache[key] = (now_mono, out)
            return out
        except Exception as exc:
            return _err("Не удалось загрузить свечи для графика. Проверьте "
                        "интернет-соединение и правильность символа."
                        f"{_net_hint(exc)} (детали: {exc})")

    def suggest_range(self, symbol: str) -> dict:
        try:
            symbol = str(symbol or "").strip().upper()
            if not symbol:
                return _err("Укажите торговую пару (символ).")
            now = pd.Timestamp.utcnow()
            start = str((now - pd.Timedelta(days=SUGGEST_DAYS)).date())
            end = str((now + pd.Timedelta(days=1)).date())
            df = self._kline_loader(symbol, "1h", start, end)
            if df is None or df.empty:
                return _err(f"Нет исторических данных по {symbol}. Проверьте "
                            "символ и интернет-соединение.")
            lower = float(df["low"].quantile(0.05))
            upper = float(df["high"].quantile(0.95))
            if upper <= lower:
                return _err("Не удалось подобрать диапазон: данные "
                            "вырожденные.")
            try:
                current = float(self._client.get_book_ticker(symbol).mid)
            except Exception:
                current = float(df["close"].iloc[-1])
            return {"ok": True, "symbol": symbol,
                    "lower": _round_sig(lower), "upper": _round_sig(upper),
                    "current": _round_sig(current),
                    "days": SUGGEST_DAYS}
        except Exception as exc:
            return _err("Не удалось подобрать диапазон. Проверьте "
                        f"интернет-соединение.{_net_hint(exc)} "
                        f"(детали: {exc})")

    def compute_range(self, symbol: str, step_pct, levels, spacing,
                      logic: str = "classic") -> dict:
        """Grid bounds from the CURRENT price + a per-level step in %.

        logic == "classic" (default — the parameter is optional for
        backward compatibility): bounds symmetric around the price.
        arithmetic: ``step_abs = price*step/100``, span = step_abs*(N-1).
        geometric: ``r = 1+step/100``, ``lower = price / r**((N-1)/2)``,
        ``upper = price * r**((N-1)/2)`` (equal % step between adjacent
        levels, price at the middle).

        logic == "tp" (buy-ladder + TP): the grid is built DOWN from the
        current price — arithmetic: ``lower = price - step_abs*N``,
        ``upper = price``; geometric: ``lower = price / r**N``,
        ``upper = price``.
        """
        try:
            symbol = str(symbol or "").strip().upper()
            if not symbol:
                return _err("Укажите торговую пару (символ).")
            logic = str(logic or "classic")
            if logic not in ("classic", "tp"):
                return _err("Логика сетки: выберите классическую или "
                            "buy-сетку с TP (RR).")
            step = _num(step_pct)
            if step is None or not (STEP_PCT_MIN <= step <= STEP_PCT_MAX):
                return _err("Шаг между уровнями должен быть числом от "
                            f"{STEP_PCT_MIN:g} до {STEP_PCT_MAX:g} "
                            "процентов.")
            lv = _num(levels)
            if (lv is None or int(lv) != lv
                    or not (RANGE_LEVELS_MIN <= int(lv)
                            <= RANGE_LEVELS_MAX)):
                return _err("Число уровней должно быть целым числом от "
                            f"{RANGE_LEVELS_MIN} до {RANGE_LEVELS_MAX}.")
            n = int(lv)
            if spacing not in ("arithmetic", "geometric"):
                return _err("Шаг сетки: выберите арифметический или "
                            "геометрический.")
            try:
                price = float(self._client.get_book_ticker(symbol).mid)
            except Exception as exc:
                return _err("Не удалось получить текущую цену с биржи. "
                            "Проверьте интернет-соединение и правильность "
                            f"символа.{_net_hint(exc)} (детали: {exc})")
            if not (math.isfinite(price) and price > 0):
                return _err("Биржа вернула некорректную цену.")
            step_abs = price * step / 100.0
            if logic == "tp":
                # buy-ladder: levels go DOWN from the current price
                if spacing == "arithmetic":
                    lower = price - step_abs * n
                else:
                    r = 1.0 + step / 100.0
                    lower = price / r ** n
                upper = price
            elif spacing == "arithmetic":
                span = step_abs * (n - 1)
                lower = price - span / 2.0
                upper = price + span / 2.0
            else:
                r = 1.0 + step / 100.0
                half = (n - 1) / 2.0
                lower = price / r ** half
                upper = price * r ** half
            if lower <= 0:
                return _err("Такой шаг и число уровней опускают нижнюю "
                            "границу до нуля или ниже. Уменьшите шаг или "
                            "число уровней.")
            return {"ok": True, "symbol": symbol,
                    "lower": _round_sig(lower), "upper": _round_sig(upper),
                    "price": _round_sig(price),
                    "step_abs": _round_sig(step_abs),
                    "levels": n, "spacing": spacing, "logic": logic}
        except Exception as exc:
            return _err(f"Не удалось рассчитать диапазон: {exc}")

    def list_symbols(self) -> dict:
        """USDT spot symbols for the picker: network → cache → builtin.

        The network result (all TRADING spot pairs quoted in USDT from
        exchangeInfo, public endpoint — the mirror fallback applies) is
        cached to ``data_cache/symbols_usdt.json`` for 24 h (timestamp
        inside the file).  On any network failure the cache is used
        regardless of age, then the built-in static list.  Never raises.
        """
        try:
            cache_path = self._dir("data_cache") / SYMBOLS_CACHE_FILE
            cached: Optional[list] = None
            cached_ts = 0.0
            try:
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if (isinstance(raw, dict)
                        and isinstance(raw.get("symbols"), list)
                        and raw["symbols"]):
                    cached = [str(s) for s in raw["symbols"]]
                    ts = _num(raw.get("timestamp"))
                    cached_ts = ts if ts is not None else 0.0
            except Exception:
                cached = None
            if cached is not None and \
                    time.time() - cached_ts < SYMBOLS_CACHE_TTL_S:
                return {"ok": True, "symbols": cached, "source": "cache"}
            try:
                info = self._client.get_exchange_info()  # AttributeError
                symbols = sorted(                        # for stubs -> except
                    s["symbol"] for s in info.get("symbols", [])
                    if s.get("status") == "TRADING"
                    and s.get("quoteAsset") == "USDT"
                    and s.get("isSpotTradingAllowed", True))
                if not symbols:
                    raise RuntimeError("пустой список символов")
                try:
                    cache_path.write_text(
                        json.dumps({"timestamp": time.time(),
                                    "symbols": symbols}),
                        encoding="utf-8")
                except Exception:
                    pass  # cache write is best-effort
                return {"ok": True, "symbols": symbols,
                        "source": "network"}
            except Exception:
                if cached is not None:  # stale cache beats builtin
                    return {"ok": True, "symbols": cached,
                            "source": "cache"}
                return {"ok": True,
                        "symbols": list(BUILTIN_USDT_SYMBOLS),
                        "source": "builtin"}
        except Exception:
            return {"ok": True, "symbols": list(BUILTIN_USDT_SYMBOLS),
                    "source": "builtin"}

    # ------------------------------------------------------------- backtest

    def run_backtest(self, cfg: dict) -> dict:
        try:
            with self._bt_lock:
                if self._bt_running:
                    return _err("Бэктест уже выполняется — дождитесь "
                                "завершения.")
            symbol = str((cfg or {}).get("symbol") or "").strip().upper()
            filters = self._filters_for(symbol) if symbol else \
                default_filters("?")
            gc, _poll, error = self._validate(cfg, filters)
            if error:
                return _err(error)
            self._save_config(cfg)
            with self._bt_lock:
                if self._bt_running:
                    return _err("Бэктест уже выполняется — дождитесь "
                                "завершения.")
                self._bt_running = True
                self._bt_report = None
                self._bt_error = None
                t = threading.Thread(target=self._bt_worker, args=(gc,),
                                     daemon=True, name="gridbot-backtest")
                self._bt_thread = t
                t.start()
            return {"ok": True, "running": True}
        except Exception as exc:
            with self._bt_lock:
                self._bt_running = False
            return _err(f"Не удалось запустить бэктест: {exc}")

    def _bt_worker(self, gc: GridConfig) -> None:
        try:
            now = pd.Timestamp.utcnow()
            start = str((now - pd.Timedelta(days=BACKTEST_DAYS)).date())
            end = str((now + pd.Timedelta(days=1)).date())
            df = self._kline_loader(gc.symbol, "1h", start, end)
            if df is None or df.empty:
                raise RuntimeError("исторические данные не загружены "
                                   "(нет соединения с интернетом?)")
            report, curve = _run_backtest_core(df, gc)

            idx = _downsample_idx(len(curve), MAX_CURVE_POINTS)
            times = curve["open_time"].tolist()
            eqs = curve["equity"].tolist()
            closes = curve["close"].tolist()
            p0 = float(report.start_price)

            def _iso(t) -> str:
                try:
                    return t.isoformat()
                except AttributeError:
                    return str(t)

            eq_curve = [{"t": _iso(times[i]), "v": float(eqs[i])}
                        for i in idx]
            bh_curve = [{"t": _iso(times[i]),
                         "v": float(gc.quote_budget * closes[i] / p0)}
                        for i in idx]
            rep = {
                "symbol": report.symbol,
                "n_candles": int(report.n_candles),
                "start_time": str(report.start_time),
                "end_time": str(report.end_time),
                "start_price": float(report.start_price),
                "final_close": float(report.final_close),
                "final_equity": float(report.final_equity),
                "total_return": float(report.total_return_pct),
                "buyhold_return": float(report.buy_hold_return_pct),
                "realized": float(report.realized_grid_profit),
                "unrealized": float(report.unrealized_pnl),
                "fees": float(report.fees_paid),
                "max_dd": float(report.max_drawdown_pct),
                "n_trades": int(report.n_trades),
                "n_round_trips": int(report.n_round_trips),
                "time_in_range": float(report.time_in_range_pct),
                "equity_curve": eq_curve,
                "buyhold_curve": bh_curve,
            }
            with self._bt_lock:
                self._bt_report = rep
                self._bt_running = False
        except Exception as exc:
            with self._bt_lock:
                self._bt_error = f"Ошибка бэктеста: {exc}{_net_hint(exc)}"
                self._bt_running = False

    def get_backtest_status(self) -> dict:
        try:
            with self._bt_lock:
                if self._bt_running:
                    return {"ok": True, "running": True}
                if self._bt_error:
                    return {"ok": False, "running": False,
                            "error": self._bt_error}
                return {"ok": True, "running": False,
                        "report": self._bt_report}
        except Exception as exc:
            return _err(f"Ошибка статуса бэктеста: {exc}")

    # ---------------------------------------------------------------- paper

    def start_paper(self, cfg: dict) -> dict:
        try:
            with self._lock:
                if self._live_running():
                    return _err("Сначала остановите live-режим.")
                if self._paper_running():
                    return _err("Paper-режим уже запущен. Сначала "
                                "остановите его.")
            symbol = str((cfg or {}).get("symbol") or "").strip().upper()
            filters = self._filters_for(symbol) if symbol else \
                default_filters("?")
            gc, poll, error = self._validate(cfg, filters)
            if error:
                return _err(error)
            self._save_config(cfg)
            return self._start_trader(gc, poll, fresh=True)
        except Exception as exc:
            return _err(f"Не удалось запустить paper-режим: {exc}")

    def resume_paper(self) -> dict:
        try:
            with self._lock:
                if self._live_running():
                    return _err("Сначала остановите live-режим.")
                if self._paper_running():
                    return _err("Paper-режим уже запущен.")
            state = self._find_saved_state()
            if not state:
                return _err("Сохранённое paper-состояние не найдено.")
            _symbol, path = state
            saved = json.loads(path.read_text(encoding="utf-8"))
            gc = GridConfig.from_dict(saved["engine"]["config"])
            poll = float(DEFAULT_CONFIG["poll"])
            try:
                app_cfg = json.loads(
                    self._config_path.read_text(encoding="utf-8"))
                p = _num(app_cfg.get("poll"))
                if p is not None and p >= 1.0:
                    poll = float(p)
            except Exception:
                pass
            return self._start_trader(gc, poll, fresh=False)
        except Exception as exc:
            return _err(f"Не удалось восстановить сохранённое состояние: "
                        f"{exc}")

    def discard_saved_state(self) -> dict:
        try:
            with self._lock:
                if self._paper_running():
                    return _err("Нельзя удалить состояние, пока paper-режим "
                                "запущен.")
            removed = 0
            state_dir = self._base_dir / "state"
            if state_dir.exists():
                for p in list(state_dir.glob("paper_*.json")) + \
                        list(state_dir.glob("paper_*.tmp")):
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
            return {"ok": True, "removed": removed}
        except Exception as exc:
            return _err(f"Не удалось удалить сохранённое состояние: {exc}")

    def _start_trader(self, gc: GridConfig, poll: float,
                      fresh: bool) -> dict:
        with self._lock:
            if self._paper_running():
                return _err("Paper-режим уже запущен.")
            if self._live_running():
                return _err("Сначала остановите live-режим.")
            state_file = self._dir("state") / f"paper_{gc.symbol}.json"
            if fresh:
                for stale in (state_file,
                              state_file.with_suffix(".tmp")):
                    if stale.exists():
                        stale.unlink()
                self._price_history.clear()
                self._recent_fills.clear()
                self._log_buf.clear()
            logger = self._make_logger(gc.symbol)
            self._stop_event = threading.Event()
            # Dedicated client for the paper thread: its rate-limit waits
            # abort on the stop event, so "Стоп" never hangs for minutes.
            paper_client: ExchangeClient = self._client
            if isinstance(self._client, BinancePublicClient):
                paper_client = BinancePublicClient(
                    base_url=self._client.base_url,
                    timeout=self._client.timeout,
                    cancel_event=self._stop_event)
            trader = PaperTrader(gc, paper_client, state_file, logger,
                                 poll_seconds=poll)
            self._trader = trader
            self._last_mode = "paper"
            self._last_symbol = gc.symbol
            self._last_tick_ts = None
            self._net_error = None
            self._last_error = None
            th = threading.Thread(target=self._paper_loop, daemon=True,
                                  name="gridbot-paper")
            self._paper_thread = th
            th.start()
            return {"ok": True, "running": True, "symbol": gc.symbol,
                    "resumed": not fresh}

    def _paper_loop(self) -> None:
        trader = self._trader
        stop = self._stop_event
        if trader is None:
            return
        symbol = trader.engine.config.symbol
        poll = max(float(trader.poll_seconds), 0.05)
        trader.logger.info("ЗАПУСК  paper-режим %s, опрос каждые %.0f с",
                           symbol, poll)
        # resume: replay the candles missed while the app was closed.
        # catch_up() is a no-op on fresh grids / tiny gaps, never raises,
        # downloads WITHOUT the lock and mutates the engine UNDER it.
        try:
            raw_fetch = self._raw_kline_fetcher or (
                lambda s, i, a, b:
                fetch_klines_raw(s, i, a, b, client=trader.client))
            rep = trader.catch_up(raw_fetch, lock=self._lock)
            if rep.get("fills"):
                with self._lock:
                    for f in rep["fills"]:
                        self._recent_fills.append(f)
        except Exception as exc:  # defensive: resume must survive anything
            trader.logger.info("ДОГОН   пропущен из-за ошибки: %s", exc)
        clean_stop = True
        while not stop.is_set():
            try:
                ticker = trader.client.get_book_ticker(symbol)
            except Exception as exc:
                if not stop.is_set():
                    with self._lock:
                        self._net_error = str(exc)
                    trader.logger.info("СБОЙ    опрос цены не удался: %s",
                                       exc)
                stop.wait(poll)
                continue
            try:
                with self._lock:
                    fills = trader.step(ticker)
                    self._last_price = float(ticker.mid)
                    self._last_tick_ts = time.time()
                    self._net_error = None
                    self._price_history.append(
                        {"t": _now_iso(), "p": float(ticker.mid)})
                    for f in fills:
                        self._recent_fills.append(
                            {"time": str(f.timestamp), "side": f.side,
                             "price": float(f.price), "qty": float(f.qty)})
            except Exception as exc:
                with self._lock:
                    self._last_error = (f"Paper-режим остановлен из-за "
                                        f"ошибки: {exc}")
                trader.logger.info("ОШИБКА  шаг движка: %s — paper-режим "
                                   "остановлен", exc)
                clean_stop = False
                break
            stop.wait(poll)
        if clean_stop:
            trader.logger.info("СТОП    paper-режим остановлен, состояние "
                               "сохранено")

    def stop_paper(self) -> dict:
        try:
            with self._lock:
                th = self._paper_thread
                if not (th and th.is_alive()):
                    return _err("Paper-режим не запущен.")
                self._stop_event.set()
            th.join(timeout=15.0)
            if th.is_alive():
                return _err("Не удалось остановить paper-поток за отведённое "
                            "время — попробуйте ещё раз.")
            return {"ok": True, "running": False}
        except Exception as exc:
            return _err(f"Не удалось остановить paper-режим: {exc}")

    # -------------------------------------------------------------- api keys

    def save_api_keys(self, key: str, secret: str) -> dict:
        """Encrypt and store the pair; only a masked preview goes back."""
        try:
            key = str(key or "").strip()
            secret = str(secret or "").strip()
            if not key or not secret:
                return _err("Укажите API-ключ и секретный ключ.")
            secrets_store.save_credentials(self._base_dir, key, secret)
            return {"ok": True, "saved": True,
                    "key_preview": secrets_store.key_preview(key)}
        except Exception as exc:
            return _err(f"Не удалось сохранить ключи: {exc}")

    def get_api_status(self) -> dict:
        try:
            try:
                creds = secrets_store.load_credentials(self._base_dir)
            except Exception:
                # file exists but cannot be decrypted
                return {"ok": True, "saved": True, "key_preview": None,
                        "corrupt": True}
            if creds is None:
                return {"ok": True, "saved": False, "key_preview": None}
            return {"ok": True, "saved": True,
                    "key_preview": secrets_store.key_preview(creds[0])}
        except Exception as exc:
            return _err(f"Не удалось прочитать статус ключей: {exc}")

    def clear_api_keys(self) -> dict:
        try:
            removed = secrets_store.clear_credentials(self._base_dir)
            return {"ok": True, "removed": bool(removed)}
        except Exception as exc:
            return _err(f"Не удалось удалить ключи: {exc}")

    def test_connection(self, network: str) -> dict:
        """Ping the chosen network and show base/quote balances."""
        try:
            network = str(network or "").strip().lower()
            if network not in ("testnet", "mainnet"):
                return _err("Неизвестная сеть: выберите testnet или "
                            "mainnet.")
            try:
                creds = secrets_store.load_credentials(self._base_dir)
            except Exception as exc:
                return _err(str(exc))
            if creds is None:
                return _err("Сначала сохраните API-ключи.")
            client = self._signed_client_factory(network, threading.Event())
            t0 = time.time()
            client.get_server_time()
            ping_ms = (time.time() - t0) * 1000.0
            balances = client.get_balances()
            symbol = DEFAULT_CONFIG["symbol"]
            try:
                saved = json.loads(
                    self._config_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict) and saved.get("symbol"):
                    symbol = str(saved["symbol"]).strip().upper()
            except Exception:
                pass
            try:
                b_asset, q_asset = split_symbol(symbol)
                out_bal = {b_asset: float(balances.get(b_asset, 0.0)),
                           q_asset: float(balances.get(q_asset, 0.0))}
            except ValueError:
                out_bal = {k: float(v)
                           for k, v in sorted(balances.items())[:6]}
            return {"ok": True, "network": network,
                    "ping_ms": round(ping_ms, 1),
                    "symbol": symbol, "balances": out_bal}
        except Exception as exc:
            return _err(_humanize_net_error(
                exc, "Проверка подключения не удалась. Проверьте ключи, "
                     f"сеть и интернет-соединение. (детали: {exc})"))

    # ----------------------------------------------------------------- live

    def start_live(self, cfg: dict, network: str,
                   confirm_text: str = "") -> dict:
        """Start REAL trading.  Mainnet requires typing the symbol name."""
        try:
            network = str(network or "").strip().lower()
            if network not in ("testnet", "mainnet"):
                return _err("Неизвестная сеть: выберите testnet или "
                            "mainnet.")
            with self._lock:
                if self._paper_running():
                    return _err("Сначала остановите paper-режим.")
                if self._live_running():
                    return _err("Live-режим уже запущен. Сначала "
                                "остановите его.")
            try:
                creds = secrets_store.load_credentials(self._base_dir)
            except Exception as exc:
                return _err(str(exc))
            if creds is None:
                return _err("Сначала сохраните API-ключи "
                            "(блок «API-ключи»).")
            symbol = str((cfg or {}).get("symbol") or "").strip().upper()
            if not symbol:
                return _err("Укажите торговую пару (символ), "
                            "например BTCUSDT.")
            if network == "mainnet" and \
                    str(confirm_text or "").strip().upper() != symbol:
                return _err(
                    "Подтверждение не совпадает: введите название пары "
                    f"({symbol}), чтобы запустить live на реальные "
                    "деньги.")
            stop_event = threading.Event()
            try:
                client = self._signed_client_factory(network, stop_event)
            except Exception as exc:
                return _err(f"Не удалось создать клиент биржи: {exc}")
            filters, from_net = fetch_filters_or_default(symbol, client)
            if not from_net:
                return _err("Не удалось получить фильтры символа с биржи "
                            "— live-режим требует работающего соединения "
                            "с выбранной сетью.")
            gc, poll, error = self._validate(cfg, filters)
            if error:
                return _err(error)
            self._save_config(cfg)
            with self._lock:
                if self._paper_running() or self._live_running():
                    return _err("Режим уже запущен.")
                state_file = self._dir("state") / \
                    f"live_{network}_{gc.symbol}.json"
                logger = self._make_logger(gc.symbol, kind="live")
                self._price_history.clear()
                self._recent_fills.clear()
                self._log_buf.clear()
                self._live_stop_event = stop_event
                self._live_cancel_orders = True
                trader = LiveTrader(gc, client, state_file, logger,
                                    poll_seconds=poll)
                self._live_trader = trader
                self._live_network = network
                self._last_mode = "live"
                self._last_symbol = gc.symbol
                self._last_tick_ts = None
                self._net_error = None
                self._last_error = None
                th = threading.Thread(target=self._live_loop, daemon=True,
                                      name="gridbot-live")
                self._live_thread = th
                th.start()
                return {"ok": True, "running": True, "symbol": gc.symbol,
                        "mode": "live", "network": network,
                        "resumed": trader.engine.started}
        except Exception as exc:
            return _err(_humanize_net_error(
                exc, f"Не удалось запустить live-режим: {exc}"))

    def _live_loop(self) -> None:
        trader = self._live_trader
        stop = self._live_stop_event
        if trader is None:
            return
        symbol = trader.engine.config.symbol
        poll = max(float(trader.poll_seconds), 0.05)
        trader.logger.info("ЗАПУСК  live-режим %s (%s), опрос каждые "
                           "%.0f с", symbol, self._live_network, poll)
        try:
            with self._lock:
                trader.start()
        except Exception as exc:
            with self._lock:
                self._last_error = _humanize_net_error(
                    exc, f"Live-режим не запущен: {exc}")
            trader.logger.error("ОШИБКА  %s", exc)
            return
        clean_stop = True
        while not stop.is_set():
            # current price for the UI chart / unrealized PnL
            try:
                ticker = trader.client.get_book_ticker(symbol)
                with self._lock:
                    self._last_price = float(ticker.mid)
                    self._last_tick_ts = time.time()
                    self._net_error = None
                    self._price_history.append(
                        {"t": _now_iso(), "p": float(ticker.mid)})
            except Exception as exc:
                if not stop.is_set():
                    with self._lock:
                        self._net_error = str(exc)
            try:
                with self._lock:
                    fills = trader.poll_once()
                    for f in fills:
                        self._recent_fills.append(
                            {"time": str(f.timestamp), "side": f.side,
                             "price": float(f.price), "qty": float(f.qty)})
            except LiveStopped as exc:
                with self._lock:
                    self._last_error = str(exc)
                clean_stop = False
                break
            except Exception as exc:  # network hiccup: log and retry
                if not stop.is_set():
                    with self._lock:
                        self._net_error = str(exc)
                    trader.logger.info("СБОЙ    цикл опроса: %s", exc)
            stop.wait(poll)
        if clean_stop and stop.is_set():
            try:
                with self._lock:
                    trader.stop(cancel_orders=self._live_cancel_orders)
            except Exception as exc:
                trader.logger.error("ОШИБКА  при остановке: %s", exc)

    def stop_live(self, cancel_orders: bool = True) -> dict:
        try:
            with self._lock:
                th = self._live_thread
                if not (th and th.is_alive()):
                    return _err("Live-режим не запущен.")
                self._live_cancel_orders = bool(cancel_orders)
                self._live_stop_event.set()
            th.join(timeout=60.0)
            if th.is_alive():
                return _err("Не удалось остановить live-поток за "
                            "отведённое время — попробуйте ещё раз.")
            return {"ok": True, "running": False}
        except Exception as exc:
            return _err(f"Не удалось остановить live-режим: {exc}")

    # ---------------------------------------------------------------- status

    def get_status(self) -> dict:
        try:
            with self._lock:
                paper_running = self._paper_running()
                live_running = self._live_running()
                running = paper_running or live_running
                mode = ("live" if live_running
                        else ("paper" if paper_running else None))
                # engine to display: the running one, else the last used
                if live_running:
                    trader = self._live_trader
                elif paper_running:
                    trader = self._trader
                elif self._last_mode == "live" and \
                        self._live_trader is not None:
                    trader = self._live_trader
                else:
                    trader = self._trader
                st: dict = {
                    "ok": True,
                    "running": running,
                    "mode": mode,
                    "network": (self._live_network if live_running
                                else None),
                    "balances": None,
                    "symbol": (trader.engine.config.symbol if trader
                               else self._last_symbol),
                    "logic": (trader.engine.config.logic if trader
                              else None),
                    "price": self._last_price,
                    "equity": None, "cash": None, "base": None,
                    "realized_pnl": None, "unrealized_pnl": None,
                    "fees": None,
                    "n_trades": 0, "n_round_trips": 0,
                    "in_range": None, "status": "stopped",
                    "poll_seconds": (float(trader.poll_seconds)
                                     if trader else None),
                    "data_age": ((time.time() - self._last_tick_ts)
                                 if self._last_tick_ts else None),
                    "net_error": self._net_error,
                    "last_error": self._last_error,
                    "open_orders": [], "grid_levels": [],
                    "recent_fills": list(self._recent_fills),
                    "price_history": list(self._price_history),
                    "log_tail": list(self._log_buf)[-LOG_TAIL_LEN:],
                }
                if live_running and self._live_trader is not None:
                    lt = self._live_trader
                    try:
                        b_asset, q_asset = split_symbol(
                            lt.engine.config.symbol)
                        st["balances"] = {
                            "base_asset": b_asset,
                            "quote_asset": q_asset,
                            "base": float(lt.balances.get(b_asset, 0.0)),
                            "quote": float(lt.balances.get(q_asset, 0.0)),
                        }
                    except ValueError:
                        st["balances"] = None
                if trader is not None:
                    st["grid_levels"] = [float(x)
                                         for x in trader.engine.levels]
                if trader is not None and trader.engine.started:
                    # live engine only learns prices from fills, so feed
                    # the freshest polled price into the snapshot
                    px = (self._last_price
                          if (trader is self._live_trader
                              and self._last_price is not None) else None)
                    snap = trader.engine.snapshot(px)
                    st.update({
                        "equity": float(snap["equity"]),
                        "cash": float(snap["cash"]),
                        "base": float(snap["base"]),
                        "realized_pnl": float(snap["realized_pnl"]),
                        "unrealized_pnl": float(snap["unrealized_pnl"]),
                        "fees": float(snap["fees_paid"]),
                        "n_trades": int(snap["n_trades"]),
                        "n_round_trips": int(snap["n_round_trips"]),
                        "in_range": snap["status"] == "in_range",
                        "status": (snap["status"] if running else "stopped"),
                        "open_orders": [
                            {"price": float(o["price"]), "side": o["side"]}
                            for o in snap["open_orders"]],
                    })
                return st
        except Exception as exc:
            return _err(f"Ошибка чтения статуса: {exc}")
