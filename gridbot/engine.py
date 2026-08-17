"""Core grid engine — pure logic, no I/O, no network, deterministic.

Design notes
------------
* Two grid logics, selected by ``GridConfig.logic``:
  - ``"classic"`` (default) — static spot grid: N price levels across
    [lower, upper].  At start, BUY limits are placed on every level
    strictly below the start price P0, SELL limits on every level
    strictly above P0 (a level exactly equal to P0 is skipped).  Sell
    levels are funded by an initial base purchase executed at market
    price P0 (fee modelled).  Buy at level i pairs a sell at level i+1.
  - ``"tp"`` — buy-ladder with individual take-profits: at start ONLY
    BUY limits are placed (every level strictly below P0; levels >= P0
    stay idle), NO initial market purchase.  A buy filled at level
    price p spawns a SELL of the same qty at the TP price
    ``p + tp_mult * step`` (arithmetic spacing) or
    ``p * ratio ** tp_mult`` (geometric), rounded DOWN to
    ``price_step`` (conservative: never overstate the harvested step)
    but never below ``p + price_step``.  The SELL remembers its ORIGIN
    level index in ``Order.level``; when it fills, the round-trip
    profit is realized and a fresh BUY is re-armed back at the origin
    level — a level cycles buy -> tp -> buy -> ...  TP sells may rest
    at arbitrary non-level prices; orders are id-keyed, so any number
    of concurrent sells coexist.
* Each level is allocated an equal slice of ``quote_budget``.  Order
  quantity at a level is sized as ``(slice / (1 + fee_rate)) / price``
  rounded DOWN to ``qty_step`` — the ``(1 + fee_rate)`` headroom
  guarantees that cash can never go negative even if every level is a
  buy level.
* Fill model (conservative, no lookahead): the engine consumes candles
  via ``on_price(timestamp, high, low, close)``.  A resting BUY limit
  at price p fills iff ``low <= p``; a resting SELL fills iff
  ``high >= p``; fill price is always the limit price.  Orders created
  DURING a candle (e.g. the sell spawned by a buy that just filled) are
  not eligible to fill until the NEXT candle — this removes intra-candle
  double-counting optimism.  Fills are processed deterministically:
  eligible buys lowest-price-first, then eligible sells
  highest-price-first, all from the start-of-candle order snapshot.
* Accounting (all in quote currency):
    - ``realized_pnl_gross``  = sum over sells of (sell_price - basis) * qty
    - ``realized_pnl``        = the same, net of the attributed buy fee
                                (basis * qty * fee_rate) and the sell fee
                                — "realized grid profit"
    - ``unrealized_pnl``      = sum over held base of (close - basis) * qty
    - ``fees_paid``           = every fee ever charged
  Exact identity, holding at every step (see tests):
      cash + base * close == quote_budget + realized_pnl_gross
                             + unrealized_pnl - fees_paid
* Price exiting [lower, upper]: orders simply stop filling and the
  snapshot reports an out-of-range status.  No trailing / re-centering
  is built; a re-centering module could later wrap the engine (stop it,
  read the snapshot, build a new engine) without touching this file.
  In tp mode a price ABOVE the range with no open sells means the grid
  is simply idle (all levels re-armed as buys below); by DESIGN CHOICE
  this still reports ``out_of_range_above`` — no extra status value,
  the UI treats it exactly like the classic out-of-range case.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Literal, Optional

Spacing = Literal["arithmetic", "geometric"]
Side = Literal["BUY", "SELL"]
Logic = Literal["classic", "tp"]

_EPS = 1e-9


# ----------------------------------------------------------------------------
# rounding helpers (exchange filters)
# ----------------------------------------------------------------------------

def round_step(value: float, step: float, mode: str = "floor") -> float:
    """Round ``value`` to a multiple of ``step``.

    mode="floor"   — round down (used for quantities: never over-commit)
    mode="nearest" — round half up (used for level prices)
    """
    if step <= 0:
        return value
    dv = Decimal(str(value)) / Decimal(str(step))
    if mode == "floor":
        n = dv.to_integral_value(rounding=ROUND_FLOOR)
    else:
        n = dv.to_integral_value(rounding=ROUND_HALF_UP)
    return float(n * Decimal(str(step)))


# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class GridConfig:
    symbol: str
    lower_price: float
    upper_price: float
    n_levels: int
    spacing: Spacing = "arithmetic"
    quote_budget: float = 1000.0
    fee_rate: float = 0.001          # single taker/maker rate
    min_notional: float = 5.0        # exchange MIN_NOTIONAL filter
    qty_step: float = 1e-6           # exchange LOT_SIZE stepSize
    price_step: float = 1e-2         # exchange PRICE_FILTER tickSize
    logic: Logic = "classic"         # "classic" pairs | "tp" buy-ladder
    tp_mult: float = 1.0             # tp only: TP distance in grid steps

    def __post_init__(self) -> None:
        if self.lower_price <= 0 or self.upper_price <= 0:
            raise ValueError("prices must be positive")
        if self.upper_price <= self.lower_price:
            raise ValueError("upper_price must be > lower_price")
        if self.n_levels < 2:
            raise ValueError("n_levels must be >= 2")
        if self.spacing not in ("arithmetic", "geometric"):
            raise ValueError(f"unknown spacing: {self.spacing!r}")
        if self.quote_budget <= 0:
            raise ValueError("quote_budget must be positive")
        if not (0 <= self.fee_rate < 0.1):
            raise ValueError("fee_rate out of sane range [0, 0.1)")
        if self.logic not in ("classic", "tp"):
            raise ValueError(f"unknown logic: {self.logic!r}")
        # tp_mult only matters (and is only validated) in tp mode
        if self.logic == "tp" and not (0.5 <= self.tp_mult <= 50.0):
            raise ValueError("tp_mult (RR) out of range [0.5, 50]")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GridConfig":
        # backward compatible: dicts saved before logic/tp_mult existed
        # simply omit the keys -> dataclass defaults ("classic", 1.0)
        return cls(**d)


def generate_levels(config: GridConfig) -> list[float]:
    """N level prices across [lower, upper], rounded to price_step."""
    n = config.n_levels
    if config.spacing == "arithmetic":
        step = (config.upper_price - config.lower_price) / (n - 1)
        raw = [config.lower_price + i * step for i in range(n)]
    else:  # geometric
        ratio = (config.upper_price / config.lower_price) ** (1.0 / (n - 1))
        raw = [config.lower_price * ratio ** i for i in range(n)]
    levels = [round_step(p, config.price_step, "nearest") for p in raw]
    for a, b in zip(levels, levels[1:]):
        if b - a <= _EPS:
            raise ValueError(
                "price_step too coarse: adjacent grid levels collapse "
                f"({a} -> {b}); reduce n_levels or widen the range"
            )
    return levels


# ----------------------------------------------------------------------------
# orders / fills
# ----------------------------------------------------------------------------

@dataclass
class Order:
    order_id: int
    side: Side
    price: float
    qty: float
    level: int                     # index into the level ladder; for a
                                   # tp-mode SELL: the ORIGIN buy level
    created_step: int              # candle index at creation (-1 = pre-start)
    basis: Optional[float] = None  # SELL only: acquisition price of the base

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        return cls(**d)


@dataclass(frozen=True)
class Fill:
    timestamp: object
    order_id: int
    side: Side
    price: float
    qty: float
    fee: float
    level: int
    realized_net: float = 0.0      # SELL only: round-trip profit net of fees


# ----------------------------------------------------------------------------
# engine
# ----------------------------------------------------------------------------

class GridEngine:
    """Pure grid state machine.  Feed candles, read snapshots."""

    def __init__(self, config: GridConfig):
        self.config = config
        self.levels: list[float] = generate_levels(config)
        slice_quote = config.quote_budget / config.n_levels
        self.level_qtys: list[float] = [
            round_step(slice_quote / (1.0 + config.fee_rate) / p,
                       config.qty_step, "floor")
            for p in self.levels
        ]
        # --- construction-time validation: every level must be tradeable ---
        # (validates the BUY notionals; in tp mode the TP sell notional is
        #  always >= the buy notional — same qty at a strictly higher
        #  price — so no extra sell-side check is needed)
        bad = [
            (i, p, q, p * q)
            for i, (p, q) in enumerate(zip(self.levels, self.level_qtys))
            if q <= 0 or p * q < config.min_notional - _EPS
        ]
        if bad:
            detail = ", ".join(
                f"level {i} @ {p:g} qty {q:g} notional {n:.4f}"
                for i, p, q, n in bad
            )
            raise ValueError(
                f"min_notional violation ({config.min_notional}): {detail}. "
                "Increase quote_budget or reduce n_levels."
            )

        # --- mutable state ---
        self.started = False
        self.cash: float = config.quote_budget
        self.base: float = 0.0
        self.fees_paid: float = 0.0
        self.realized_pnl_gross: float = 0.0
        self.realized_pnl: float = 0.0       # net of attributed fees
        self.n_trades: int = 0
        self.n_round_trips: int = 0
        self.orders: dict[int, Order] = {}   # open orders by id
        # base bought at the TOP level (no level above to pair a sell with):
        # held as (qty, basis) lots so unrealized PnL stays exact
        self.orphan_lots: list[tuple[float, float]] = []
        self._next_order_id: int = 1
        self._step: int = 0                  # candle counter
        self.last_close: Optional[float] = None
        self.start_price: Optional[float] = None

    # ------------------------------------------------------------------ start

    def start(self, timestamp, price: float) -> dict:
        """Initial placement around P0=``price``.

        classic: buys on levels < P0, sells on levels > P0 (level == P0
        skipped), sell inventory bought at market at P0 with fee
        modelled.  tp: ONLY buys on levels < P0 (levels >= P0 idle), no
        market purchase — the info dict reports zeros so callers (paper
        / live start logging, the real market-buy branch) need no
        special-casing.
        Returns a small info dict about the initial market purchase.
        """
        if self.started:
            raise RuntimeError("engine already started")
        self.started = True
        self.start_price = price
        self.last_close = price

        if self.config.logic == "tp":
            for i, (lvl, qty) in enumerate(zip(self.levels,
                                               self.level_qtys)):
                if lvl < price - _EPS:
                    self._place(Order(self._new_id(), "BUY", lvl, qty,
                                      i, -1))
            return {"initial_base": 0.0, "cost": 0.0, "fee": 0.0,
                    "timestamp": timestamp}

        sell_base_needed = 0.0
        for i, (lvl, qty) in enumerate(zip(self.levels, self.level_qtys)):
            if lvl < price - _EPS:
                self._place(Order(self._new_id(), "BUY", lvl, qty, i, -1))
            elif lvl > price + _EPS:
                sell_base_needed += qty

        cost = fee = 0.0
        if sell_base_needed > 0:
            cost = sell_base_needed * price
            fee = cost * self.config.fee_rate
            if cost + fee > self.cash + _EPS:
                raise ValueError(
                    "quote_budget too small for the initial base purchase "
                    f"(need {cost + fee:.2f}, have {self.cash:.2f})"
                )
            self.cash -= cost + fee
            self.base += sell_base_needed
            self.fees_paid += fee
            for i, (lvl, qty) in enumerate(zip(self.levels, self.level_qtys)):
                if lvl > price + _EPS:
                    self._place(Order(self._new_id(), "SELL", lvl, qty, i,
                                      -1, basis=price))
        return {"initial_base": sell_base_needed, "cost": cost, "fee": fee,
                "timestamp": timestamp}

    # ------------------------------------------------------------------ candle

    def on_price(self, timestamp, high: float, low: float,
                 close: float) -> list[Fill]:
        """Process one candle; return the fills it produced (may be [])."""
        if not self.started:
            raise RuntimeError("call start() before on_price()")
        if high < low:
            # paper mode maps bid/ask onto (high, low); allow high < low —
            # the engine only compares levels against each bound.
            pass
        step = self._step
        self._step += 1

        # snapshot: only orders that existed BEFORE this candle may fill
        snapshot = [o for o in self.orders.values() if o.created_step < step]
        fills: list[Fill] = []

        buys = sorted((o for o in snapshot if o.side == "BUY"
                       and low <= o.price + _EPS),
                      key=lambda o: (o.price, o.order_id))
        for o in buys:
            fills.append(self._fill_buy(o, timestamp, step))

        sells = sorted((o for o in snapshot if o.side == "SELL"
                        and high >= o.price - _EPS),
                       key=lambda o: (-o.price, o.order_id))
        for o in sells:
            fills.append(self._fill_sell(o, timestamp, step))

        self.last_close = close
        return fills

    # -------------------------------------------------- external (live) fill

    def apply_external_fill(self, order_id: int,
                            timestamp) -> "tuple[Fill, Optional[Order]]":
        """Apply the fill of one currently-open order at its limit price.

        Live-trading entry point: the exchange reported this order as
        FILLED, so mirror it here through the exact same accounting as
        the candle path (``_fill_buy`` / ``_fill_sell``).  Each call
        advances the candle counter and sets ``last_close`` to the fill
        price, so a sequence of external fills is byte-identical to
        feeding one candle per fill through ``on_price`` (tested).

        Returns ``(fill, paired_order_or_None)``; the second element is
        the newly created opposite order (for the caller to mirror on
        the exchange), or ``None`` (top-level buy -> orphan lot, or the
        paired level is already occupied).
        """
        if not self.started:
            raise RuntimeError("call start() before apply_external_fill()")
        o = self.orders.get(order_id)
        if o is None:
            raise KeyError(f"order {order_id} is not open")
        step = self._step
        self._step += 1
        paired_id = self._next_order_id  # id the paired order gets, if any
        if o.side == "BUY":
            fill = self._fill_buy(o, timestamp, step)
        else:
            fill = self._fill_sell(o, timestamp, step)
        self.last_close = o.price
        return fill, self.orders.get(paired_id)

    # ------------------------------------------------------------------ fills

    def _fill_buy(self, o: Order, timestamp, step: int) -> Fill:
        cost = o.price * o.qty
        fee = cost * self.config.fee_rate
        if cost + fee > self.cash + _EPS:  # defensive; sizing prevents this
            raise RuntimeError(
                f"internal accounting error: buy fill needs {cost + fee:.8f} "
                f"but cash is {self.cash:.8f}"
            )
        self.cash -= cost + fee
        self.base += o.qty
        self.fees_paid += fee
        self.n_trades += 1
        del self.orders[o.order_id]

        if self.config.logic == "tp":
            # tp rule: every buy gets its own SELL at the TP price,
            # remembering the ORIGIN level for the later re-arm (created
            # this step, therefore NOT eligible to fill inside the same
            # candle — the same conservative rule as classic)
            self._place(Order(self._new_id(), "SELL",
                              self._tp_price(o.price), o.qty, o.level,
                              step, basis=o.price))
        else:
            # pair rule: buy at level i -> sell at level i+1 (created this
            # step, therefore NOT eligible to fill inside the same candle)
            nxt = o.level + 1
            if nxt < len(self.levels):
                self._place(Order(self._new_id(), "SELL", self.levels[nxt],
                                  o.qty, nxt, step, basis=o.price))
            else:  # top-level buy: nowhere to pair a sell — orphan lot
                self.orphan_lots.append((o.qty, o.price))
        return Fill(timestamp, o.order_id, "BUY", o.price, o.qty, fee, o.level)

    def _fill_sell(self, o: Order, timestamp, step: int) -> Fill:
        proceeds = o.price * o.qty
        fee = proceeds * self.config.fee_rate
        if o.qty > self.base + _EPS:
            raise RuntimeError("internal accounting error: selling more base "
                               "than held")
        self.cash += proceeds - fee
        self.base -= o.qty
        self.fees_paid += fee
        self.n_trades += 1
        self.n_round_trips += 1

        basis = o.basis if o.basis is not None else o.price
        gross = (o.price - basis) * o.qty
        net = gross - basis * o.qty * self.config.fee_rate - fee
        self.realized_pnl_gross += gross
        self.realized_pnl += net
        del self.orders[o.order_id]

        if self.config.logic == "tp":
            # re-arm rule: the TP sell carries its ORIGIN level in
            # ``o.level`` — place a fresh BUY back there (qty is the
            # standard per-level allocation; occupancy-checked like
            # classic, so at most one open BUY per level and cash can
            # never be over-committed)
            if not self._has_open(o.level, "BUY"):
                self._place(Order(self._new_id(), "BUY",
                                  self.levels[o.level],
                                  self.level_qtys[o.level], o.level, step))
        else:
            # pair rule: sell at level j -> buy at level j-1 (occupancy-
            # checked: at most one open BUY per level, so cash can never
            # be over-committed)
            prv = o.level - 1
            if prv >= 0 and not self._has_open(prv, "BUY"):
                self._place(Order(self._new_id(), "BUY", self.levels[prv],
                                  self.level_qtys[prv], prv, step))
        return Fill(timestamp, o.order_id, "SELL", o.price, o.qty, fee,
                    o.level, realized_net=net)

    # ------------------------------------------------------------------ misc

    def _new_id(self) -> int:
        i = self._next_order_id
        self._next_order_id += 1
        return i

    def _place(self, order: Order) -> None:
        self.orders[order.order_id] = order

    def _has_open(self, level: int, side: Side) -> bool:
        return any(o.level == level and o.side == side
                   for o in self.orders.values())

    def _tp_price(self, buy_price: float) -> float:
        """tp mode: TP sell price for a buy filled at ``buy_price``.

        ``tp_mult`` grid steps above the entry: arithmetic spacing adds
        ``tp_mult * step_abs`` (the nominal level step), geometric
        multiplies by ``ratio ** tp_mult``.  Rounded DOWN to
        ``price_step`` — conservative: the modelled profit never
        overstates what the rounded exchange price can deliver — but
        never below ``buy_price + price_step`` (a TP must sit at least
        one tick above the entry).
        """
        cfg = self.config
        if cfg.spacing == "arithmetic":
            step_abs = (cfg.upper_price - cfg.lower_price) \
                / (cfg.n_levels - 1)
            raw = buy_price + cfg.tp_mult * step_abs
        else:  # geometric
            ratio = (cfg.upper_price / cfg.lower_price) \
                ** (1.0 / (cfg.n_levels - 1))
            raw = buy_price * ratio ** cfg.tp_mult
        if cfg.price_step <= 0:
            return raw
        tp = round_step(raw, cfg.price_step, "floor")
        min_tp = round_step(buy_price + cfg.price_step,
                            cfg.price_step, "nearest")
        return max(tp, min_tp)

    # ------------------------------------------------------------------ views

    def unrealized_pnl(self, close: Optional[float] = None) -> float:
        """(close - basis) * qty over held base (held base == open sells)."""
        px = self.last_close if close is None else close
        if px is None:
            return 0.0
        held = sum((px - o.basis) * o.qty
                   for o in self.orders.values()
                   if o.side == "SELL" and o.basis is not None)
        held += sum((px - basis) * qty for qty, basis in self.orphan_lots)
        return held

    def equity(self, close: Optional[float] = None) -> float:
        px = self.last_close if close is None else close
        return self.cash + self.base * (px if px is not None else 0.0)

    def status(self, close: Optional[float] = None) -> str:
        # tp mode above the range with no open sells is merely idle;
        # by design it is still reported as out_of_range_above (see the
        # module docstring) so the UI semantics stay identical.
        px = self.last_close if close is None else close
        if not self.started or px is None:
            return "inactive"
        if px < self.config.lower_price:
            return "out_of_range_below"
        if px > self.config.upper_price:
            return "out_of_range_above"
        return "in_range"

    def snapshot(self, close: Optional[float] = None) -> dict:
        """Full accounting snapshot (all quote-denominated)."""
        px = self.last_close if close is None else close
        return {
            "close": px,
            "logic": self.config.logic,
            "cash": self.cash,
            "base": self.base,
            "equity": self.equity(px),
            "realized_pnl": self.realized_pnl,
            "realized_pnl_gross": self.realized_pnl_gross,
            "unrealized_pnl": self.unrealized_pnl(px),
            "fees_paid": self.fees_paid,
            "n_trades": self.n_trades,
            "n_round_trips": self.n_round_trips,
            "n_open_orders": len(self.orders),
            "open_orders": [o.to_dict() for o in
                            sorted(self.orders.values(),
                                   key=lambda o: o.order_id)],
            "status": self.status(px),
        }

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        """Serializable full state (config + mutable state)."""
        return {
            "config": self.config.to_dict(),
            "state": {
                "started": self.started,
                "cash": self.cash,
                "base": self.base,
                "fees_paid": self.fees_paid,
                "realized_pnl_gross": self.realized_pnl_gross,
                "realized_pnl": self.realized_pnl,
                "n_trades": self.n_trades,
                "n_round_trips": self.n_round_trips,
                "orders": [o.to_dict() for o in
                           sorted(self.orders.values(),
                                  key=lambda o: o.order_id)],
                "orphan_lots": [list(l) for l in self.orphan_lots],
                "next_order_id": self._next_order_id,
                "step": self._step,
                "last_close": self.last_close,
                "start_price": self.start_price,
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GridEngine":
        eng = cls(GridConfig.from_dict(d["config"]))
        s = d["state"]
        eng.started = s["started"]
        eng.cash = s["cash"]
        eng.base = s["base"]
        eng.fees_paid = s["fees_paid"]
        eng.realized_pnl_gross = s["realized_pnl_gross"]
        eng.realized_pnl = s["realized_pnl"]
        eng.n_trades = s["n_trades"]
        eng.n_round_trips = s["n_round_trips"]
        eng.orders = {o["order_id"]: Order.from_dict(o) for o in s["orders"]}
        eng.orphan_lots = [tuple(l) for l in s.get("orphan_lots", [])]
        eng._next_order_id = s["next_order_id"]
        eng._step = s["step"]
        eng.last_close = s["last_close"]
        eng.start_price = s["start_price"]
        return eng
