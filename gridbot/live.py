"""LIVE trading: real Binance spot orders driven by the grid engine.

The engine stays the single source of truth for accounting; this module
mirrors its orders on the exchange and feeds real fills back through
``GridEngine.apply_external_fill`` (which reuses the exact candle-path
accounting).

Safety model
------------
* NO withdrawal/transfer code exists anywhere (see ``exchange.py``) —
  keys without withdrawal permission are enough for full operation.
* Hard budget cap: the quote ever committed to the exchange (initial
  market buy + notionals of open BUY limits) may never exceed
  ``quote_budget * 1.001``; every placement is checked and refused with
  an ERROR log on violation.
* Every state change is persisted atomically (tmp + ``os.replace``) to
  a JSON file, so a crash/restart recovers cleanly: offline fills are
  applied in ``updateTime`` order and missing paired orders re-placed.
* An order cancelled EXTERNALLY (by the user on the exchange) stops the
  bot safely: remaining tracked orders are cancelled (unless configured
  to keep) and ``LiveStopped`` is raised.
* Partial fills are waited out (logged once) — the engine only learns
  about an order once it is fully FILLED.

CLI (TESTNET by default; keys ONLY via environment variables):
    set GRIDBOT_API_KEY=...    (PowerShell: $env:GRIDBOT_API_KEY="...")
    set GRIDBOT_API_SECRET=...
    python -m gridbot.live --symbol BTCUSDT --lower 70000 --upper 110000 \
        --levels 30 --budget 100 --testnet
    python -m gridbot.live ... --mainnet          # asks to type YES
    python -m gridbot.live ... --mainnet --yes    # non-interactive
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .engine import Fill, GridConfig, GridEngine, Order
from .exchange import (BINANCE_BASE, TESTNET_BASE, BinanceSignedClient,
                       fetch_filters_or_default)

LOGS_DIR = Path(__file__).resolve().parent / "logs"
STATE_DIR = Path(__file__).resolve().parent / "state"

#: quote ever sent to the exchange may exceed the budget by at most 0.1 %
BUDGET_CAP_FACTOR = 1.001
_EPS = 1e-9

#: terminal order states that mean "gone without a full fill"
CANCELLED_STATES = ("CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH",
                    "PENDING_CANCEL")

#: known quote assets, longest first, for splitting SYMBOL -> base/quote
QUOTE_ASSETS = ("USDT", "FDUSD", "USDC", "TUSD", "BUSD", "DAI",
                "EUR", "TRY", "BRL", "BTC", "ETH", "BNB")


class LiveStopped(RuntimeError):
    """The live trader stopped itself (message is user-facing Russian)."""


def split_symbol(symbol: str) -> tuple[str, str]:
    """``BTCUSDT`` -> ``("BTC", "USDT")`` using the known quote suffixes."""
    s = str(symbol or "").upper()
    for q in sorted(QUOTE_ASSETS, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)], q
    raise ValueError(f"cannot split symbol {symbol!r} into base/quote")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_logger(symbol: str, log_dir: Path | None = None) -> logging.Logger:
    d = Path(log_dir) if log_dir else LOGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"gridbot.live.{symbol}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s  %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(d / f"live_{symbol}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


class LiveTrader:
    """Grid engine + REAL Binance orders + persistent state.

    ``client`` is anything with the ``BinanceSignedClient`` trading
    surface (tests inject an in-memory fake).
    """

    def __init__(self, config: GridConfig, client, state_file: Path,
                 logger: logging.Logger, poll_seconds: float = 5.0,
                 keep_orders_on_stop: bool = False):
        self.client = client
        self.state_file = Path(state_file)
        self.logger = logger
        self.poll_seconds = poll_seconds
        self.keep_orders_on_stop = keep_orders_on_stop

        self.run_id: str = secrets.token_hex(3)  # 6 hex chars
        self.placed: dict[int, str] = {}   # engine order id -> clientOrderId
        self.pending: list[int] = []       # engine ids awaiting real placement
        self.market_cost: float = 0.0      # quote spent in the initial buy
        self.balances: dict[str, float] = {}
        self.stop_reason: Optional[str] = None
        self._partial_logged: set[str] = set()
        self._cap_logged: set[int] = set()

        if self.state_file.exists():
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.engine = GridEngine.from_dict(saved["engine"])
            lv = saved.get("live", {})
            self.run_id = lv.get("run_id", self.run_id)
            self.placed = {int(k): v for k, v in
                           lv.get("placed", {}).items()}
            self.pending = [int(x) for x in lv.get("pending", [])]
            self.market_cost = float(lv.get("market_cost", 0.0))
            self.logger.info(
                "ВОЗОБНОВЛЕНИЕ  состояние из %s (equity=%.2f, "
                "%d ордеров на бирже, %d в очереди)",
                self.state_file, self.engine.equity(),
                len(self.placed), len(self.pending))
            if saved["engine"]["config"] != config.to_dict():
                self.logger.info(
                    "ВНИМАНИЕ  сохранённая конфигурация отличается от "
                    "заданной; действует СОХРАНЁННАЯ (удалите файл "
                    "состояния, чтобы начать заново)")
        else:
            self.engine = GridEngine(config)

    # ------------------------------------------------------------- helpers

    @property
    def config(self) -> GridConfig:
        return self.engine.config

    def _coid(self, engine_order_id) -> str:
        """Deterministic clientOrderId for an engine order."""
        return f"gb{self.run_id}-{engine_order_id}"

    def _budget_allows(self, order: Order) -> bool:
        """Hard cap on quote at risk: never exceed budget * 1.001.

        Quote at risk on the exchange = everything ever sent (initial
        market buy + filled BUY notionals + modelled fees) minus what
        SELL fills returned, plus the notionals of currently open BUY
        limits, plus the candidate.  The "sent minus returned" part is
        exactly ``quote_budget - engine.cash`` by the engine's exact
        accounting identity.  (At start this equals the naive
        "market cost + open BUY notionals" formula; after sells return
        quote, the naive formula would wrongly block legitimate grid
        buys.)  SELL orders never send quote, so they always pass.
        """
        if order.side != "BUY":
            return True
        at_risk = self.config.quote_budget - self.engine.cash
        for oid in self.placed:
            eo = self.engine.orders.get(oid)
            if eo is not None and eo.side == "BUY":
                at_risk += eo.price * eo.qty
        at_risk += order.price * order.qty
        return at_risk <= (self.config.quote_budget * BUDGET_CAP_FACTOR
                           + _EPS)

    def _place_real(self, engine_order_id: int) -> bool:
        """Mirror one engine order on the exchange; queue on failure."""
        o = self.engine.orders.get(engine_order_id)
        if o is None:  # already gone from the engine
            if engine_order_id in self.pending:
                self.pending.remove(engine_order_id)
            return False
        if not self._budget_allows(o):
            if engine_order_id not in self._cap_logged:
                self._cap_logged.add(engine_order_id)
                self.logger.error(
                    "БЮДЖЕТ  отказ: %s %s @ %.8g превысил бы лимит "
                    "бюджета (%.2f x %.3f) — ордер НЕ выставлен",
                    o.side, self.config.symbol, o.price,
                    self.config.quote_budget, BUDGET_CAP_FACTOR)
            if engine_order_id not in self.pending:
                self.pending.append(engine_order_id)
            self.save_state()
            return False
        coid = self._coid(engine_order_id)
        try:
            self.client.place_limit(
                self.config.symbol, o.side, o.price, o.qty, coid,
                price_step=self.config.price_step,
                qty_step=self.config.qty_step)
        except Exception as exc:
            self.logger.error(
                "ОШИБКА  не удалось выставить %s lvl %d @ %.8g (%s): %s — "
                "повторим в следующем цикле", o.side, o.level, o.price,
                coid, exc)
            if engine_order_id not in self.pending:
                self.pending.append(engine_order_id)
            self.save_state()
            return False
        self.placed[engine_order_id] = coid
        if engine_order_id in self.pending:
            self.pending.remove(engine_order_id)
        self._cap_logged.discard(engine_order_id)
        self.logger.info("ОРДЕР   %s lvl %2d @ %.8g qty %.8g (%s)",
                         o.side, o.level, o.price, o.qty, coid)
        self.save_state()
        return True

    def _refresh_balances(self) -> None:
        try:
            self.balances = self.client.get_balances()
        except Exception as exc:  # display-only data: never fatal
            self.logger.info("СБОЙ    не удалось обновить балансы: %s", exc)

    # --------------------------------------------------------------- start

    def start(self) -> None:
        """Fresh start (market buy + limit ladder) or state recovery."""
        if self.engine.started:
            self._recover()
            return

        base_asset, quote_asset = split_symbol(self.config.symbol)
        balances = self.client.get_balances()
        free_quote = float(balances.get(quote_asset, 0.0))
        if free_quote + _EPS < self.config.quote_budget:
            raise RuntimeError(
                f"Недостаточно средств на бирже: свободно "
                f"{free_quote:.2f} {quote_asset}, требуется бюджет "
                f"{self.config.quote_budget:.2f} {quote_asset}.")
        self.balances = balances

        ticker = self.client.get_book_ticker(self.config.symbol)
        price = ticker.mid
        info = self.engine.start(_now_iso(), price)
        self.logger.info(
            "СТАРТ   %s @ %.8g | модель: базовая покупка %.8g "
            "(стоимость %.2f, комиссия %.4f) | %d ордеров сетки",
            self.config.symbol, price, info["initial_base"],
            info["cost"], info["fee"], len(self.engine.orders))

        # real initial market buy — BEFORE any limit order and BEFORE the
        # first state persist, so a failure aborts cleanly with nothing
        # on the exchange and no state file left behind
        if info["initial_base"] > 0:
            cost = float(info["cost"])
            if cost > self.config.quote_budget * BUDGET_CAP_FACTOR + _EPS:
                raise RuntimeError(
                    "Внутренняя ошибка: стартовая покупка превышает бюджет.")
            try:
                resp = self.client.place_market_quote(
                    self.config.symbol, "BUY", cost,
                    client_order_id=f"gb{self.run_id}-m")
            except Exception as exc:
                raise RuntimeError(
                    f"Стартовая покупка по рынку не удалась: {exc}. "
                    "Live-режим не запущен, лимитные ордера не "
                    "выставлялись.") from exc
            executed = float(resp.get("executedQty", 0.0) or 0.0)
            spent = float(resp.get("cummulativeQuoteQty", 0.0) or 0.0)
            avg = spent / executed if executed > 0 else 0.0
            self.market_cost = spent if spent > 0 else cost
            # a small mismatch vs the engine model is EXPECTED (fees in
            # base/BNB, price movement between ticker and execution)
            self.logger.info(
                "ПОКУПКА рынок: исполнено %.8g %s по средней %.8g "
                "(потрачено %.2f %s); модель движка: %.8g @ %.8g "
                "(расхождение qty %.3g)",
                executed, base_asset, avg, spent, quote_asset,
                info["initial_base"], price,
                executed - info["initial_base"])

        self.save_state()
        for oid in sorted(self.engine.orders):
            self._place_real(oid)
        self._refresh_balances()
        self.save_state()

    # ------------------------------------------------------------ recovery

    def _recover(self) -> None:
        """After a restart: apply offline fills, re-place missing orders."""
        symbol = self.config.symbol
        self.logger.info("ВОССТАНОВЛЕНИЕ  сверка с биржей (%d ордеров "
                         "отслеживалось)", len(self.placed))
        open_orders = self.client.get_open_orders(symbol)
        open_coids = {o.get("clientOrderId") for o in open_orders}

        filled: list[tuple[int, int, str, dict]] = []
        for oid, coid in list(self.placed.items()):
            if coid in open_coids:
                continue
            od = self.client.get_order(symbol, coid)
            status = od.get("status")
            if status == "FILLED":
                filled.append((int(od.get("updateTime", 0)), oid, coid, od))
            elif status in CANCELLED_STATES:
                self.logger.info(
                    "ВОССТАНОВЛЕНИЕ  ордер %s был отменён (%s) — выставим "
                    "заново", coid, status)
                del self.placed[oid]
                if oid in self.engine.orders and oid not in self.pending:
                    self.pending.append(oid)
        # apply offline fills strictly in exchange updateTime order
        for _ut, oid, coid, od in sorted(filled):
            fill, new_order = self.engine.apply_external_fill(oid, _now_iso())
            del self.placed[oid]
            self._log_fill(fill, offline=True)
            if new_order is not None and new_order.order_id not in \
                    self.pending:
                self.pending.append(new_order.order_id)
            self.save_state()
        # re-place everything the exchange is missing
        for oid in list(self.pending):
            self._place_real(oid)
        for oid in sorted(self.engine.orders):
            if oid not in self.placed and oid not in self.pending:
                self._place_real(oid)
        self._refresh_balances()
        self.save_state()

    # ----------------------------------------------------------- poll cycle

    def _log_fill(self, f: Fill, offline: bool = False) -> None:
        tag = "ОФЛАЙН " if offline else ""
        if f.side == "BUY":
            self.logger.info(
                "СДЕЛКА  %sBUY  lvl %2d @ %.8g qty %.8g",
                tag, f.level, f.price, f.qty)
        else:
            self.logger.info(
                "СДЕЛКА  %sSELL lvl %2d @ %.8g qty %.8g | круг: %+.6f",
                tag, f.level, f.price, f.qty, f.realized_net)

    def poll_once(self) -> list[Fill]:
        """One poll cycle: retry queue, diff open orders, apply fills.

        Uses a single ``get_open_orders`` request; ``get_order`` is only
        called for tracked orders that disappeared from the open list.
        Raises ``LiveStopped`` if a tracked order was cancelled
        externally (the bot stops itself safely first).
        """
        for oid in list(self.pending):
            self._place_real(oid)

        symbol = self.config.symbol
        open_orders = self.client.get_open_orders(symbol)
        open_by_coid = {o.get("clientOrderId"): o for o in open_orders}

        filled: list[tuple[int, int, str, dict]] = []
        for oid, coid in list(self.placed.items()):
            od = open_by_coid.get(coid)
            if od is not None:
                if od.get("status") == "PARTIALLY_FILLED" and \
                        coid not in self._partial_logged:
                    self._partial_logged.add(coid)
                    self.logger.info(
                        "ЧАСТИЧНО ордер %s исполнен частично (%s из %s) — "
                        "ждём полного исполнения", coid,
                        od.get("executedQty"), od.get("origQty"))
                continue
            try:
                od = self.client.get_order(symbol, coid)
            except Exception as exc:
                self.logger.info("СБОЙ    статус ордера %s недоступен: %s",
                                 coid, exc)
                continue
            status = od.get("status")
            if status == "FILLED":
                filled.append((int(od.get("updateTime", 0)), oid, coid, od))
            elif status in CANCELLED_STATES:
                self.stop_reason = "ордер отменён извне"
                self.logger.error(
                    "ОШИБКА  ордер %s отменён извне (статус %s) — "
                    "безопасная остановка бота", coid, status)
                del self.placed[oid]
                self.stop()
                raise LiveStopped(
                    f"Live-режим остановлен: ордер отменён извне ({coid}).")
            # else: NEW-but-not-listed race — check again next cycle

        fills: list[Fill] = []
        for _ut, oid, coid, od in sorted(filled):
            fill, new_order = self.engine.apply_external_fill(oid, _now_iso())
            del self.placed[oid]
            self._partial_logged.discard(coid)
            self._log_fill(fill)
            fills.append(fill)
            self.save_state()
            if new_order is not None:
                self._place_real(new_order.order_id)
        if fills:
            self._refresh_balances()
            s = self.engine.snapshot()
            self.logger.info(
                "ИТОГ    equity %.2f | кэш %.2f | монета %.8g | "
                "реализовано %.4f | комиссии(модель) %.4f | кругов %d",
                s["equity"], s["cash"], s["base"], s["realized_pnl"],
                s["fees_paid"], s["n_round_trips"])
            self.save_state()
        return fills

    # ----------------------------------------------------------------- stop

    def stop(self, cancel_orders: Optional[bool] = None) -> None:
        """Cancel tracked orders (unless told to keep them) and persist."""
        do_cancel = (not self.keep_orders_on_stop) if cancel_orders is None \
            else bool(cancel_orders)
        if do_cancel:
            for oid, coid in list(self.placed.items()):
                try:
                    self.client.cancel(self.config.symbol, coid)
                except Exception as exc:  # each failure logged, not fatal
                    self.logger.error(
                        "ОШИБКА  не удалось отменить ордер %s: %s",
                        coid, exc)
                    continue
                self.logger.info("ОТМЕНА  ордер %s снят с биржи", coid)
                del self.placed[oid]
                # keep the engine order queued so a future resume
                # re-places it
                if oid in self.engine.orders and oid not in self.pending:
                    self.pending.append(oid)
        else:
            self.logger.info(
                "СТОП    ордера ОСТАВЛЕНЫ на бирже (%d шт.) — они "
                "продолжат исполняться без бота", len(self.placed))
        self.save_state()
        self.logger.info("СТОП    live-режим остановлен, состояние в %s",
                         self.state_file)

    # ------------------------------------------------------------ persistence

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _now_iso(),
            "engine": self.engine.to_dict(),
            "live": {
                "run_id": self.run_id,
                "placed": {str(k): v for k, v in self.placed.items()},
                "pending": list(self.pending),
                "market_cost": self.market_cost,
            },
        }
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_file)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

MAINNET_WARNING = """
\033[91m================================================================
  ВНИМАНИЕ: РЕАЛЬНАЯ ТОРГОВЛЯ НА BINANCE (MAINNET)
  Бот будет выставлять НАСТОЯЩИЕ ордера на НАСТОЯЩИЕ деньги.
  - Сетка в устойчивом тренде вниз накапливает убыток.
  - API-ключи должны быть БЕЗ права вывода средств.
  - Начинайте с малого бюджета.
================================================================\033[0m"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m gridbot.live",
        description="Grid LIVE trader — РЕАЛЬНЫЕ ордера на Binance spot. "
                    "Ключи только через переменные окружения "
                    "GRIDBOT_API_KEY / GRIDBOT_API_SECRET.")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--lower", type=float, required=True)
    p.add_argument("--upper", type=float, required=True)
    p.add_argument("--levels", type=int, default=30)
    p.add_argument("--budget", type=float, default=100.0)
    p.add_argument("--spacing", choices=["arithmetic", "geometric"],
                   default="arithmetic")
    p.add_argument("--logic", choices=["classic", "tp"], default="classic",
                   help="classic: paired buy<->sell levels; tp: buy-ladder "
                        "where each buy closes at its own TP (see --rr)")
    p.add_argument("--rr", type=float, default=3.0,
                   help="tp logic only: TP distance in grid steps "
                        "(RR, default 3.0)")
    p.add_argument("--fee", type=float, default=0.001)
    p.add_argument("--poll", type=float, default=5.0,
                   help="seconds between order-status polls")
    net = p.add_mutually_exclusive_group()
    net.add_argument("--testnet", action="store_true", default=True,
                     help="Binance Spot Testnet (по умолчанию)")
    net.add_argument("--mainnet", action="store_true",
                     help="РЕАЛЬНАЯ биржа — потребует подтверждения YES")
    p.add_argument("--yes", action="store_true",
                   help="пропустить интерактивное подтверждение mainnet")
    p.add_argument("--keep-orders", action="store_true",
                   help="при остановке НЕ отменять ордера на бирже")
    p.add_argument("--state-file", default=None,
                   help="JSON state path (default "
                        "gridbot/state/live_{network}_{symbol}.json)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    api_key = os.environ.get("GRIDBOT_API_KEY", "").strip()
    api_secret = os.environ.get("GRIDBOT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise SystemExit(
            "Задайте переменные окружения GRIDBOT_API_KEY и "
            "GRIDBOT_API_SECRET (ключи в аргументах командной строки "
            "не принимаются).")

    network = "mainnet" if args.mainnet else "testnet"
    if network == "mainnet" and not args.yes:
        print(MAINNET_WARNING)
        answer = input("Введите YES (заглавными), чтобы продолжить: ")
        if answer.strip() != "YES":
            raise SystemExit("Отменено пользователем.")

    base_url = BINANCE_BASE if network == "mainnet" else TESTNET_BASE
    client = BinanceSignedClient(api_key, api_secret, base_url=base_url)
    symbol = args.symbol.strip().upper()

    filters, from_net = fetch_filters_or_default(symbol, client)
    if not from_net:
        raise SystemExit(
            "Не удалось получить фильтры символа с биржи — live-режим "
            "требует работающего соединения с выбранной сетью.")

    config = GridConfig(
        symbol=symbol, lower_price=args.lower, upper_price=args.upper,
        n_levels=args.levels, spacing=args.spacing,
        quote_budget=args.budget, fee_rate=args.fee,
        min_notional=filters.min_notional, qty_step=filters.qty_step,
        price_step=filters.price_step,
        logic=args.logic,
        tp_mult=args.rr if args.logic == "tp" else 1.0,
    )
    state_file = Path(args.state_file) if args.state_file else (
        STATE_DIR / f"live_{network}_{symbol}.json")
    logger = make_logger(symbol)
    logger.info("СЕТЬ    %s (%s)", network, base_url)

    trader = LiveTrader(config, client, state_file, logger,
                        poll_seconds=args.poll,
                        keep_orders_on_stop=args.keep_orders)
    try:
        trader.start()
    except Exception as exc:
        logger.error("ОШИБКА  запуск не удался: %s", exc)
        return 1

    logger.info("live-трейдер: опрос каждые %.0f с (Ctrl+C — остановка)",
                args.poll)
    try:
        while True:
            try:
                trader.poll_once()
            except LiveStopped as exc:
                logger.error("СТОП    %s", exc)
                return 1
            except Exception as exc:  # network hiccup: log and retry
                logger.info("СБОЙ    цикл опроса: %s", exc)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        trader.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
