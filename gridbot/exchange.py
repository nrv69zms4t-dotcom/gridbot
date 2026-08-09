"""Exchange abstraction.

``ExchangeClient`` is the ABC every mode talks to.  ``BinancePublicClient``
implements the read-only endpoints against the public Binance REST API
(no API key).  ``BinanceSignedClient`` adds the signed (HMAC-SHA256)
spot-trading endpoints for live mode — account, order placement and
cancellation ONLY.  Withdrawal / transfer endpoints are DELIBERATELY not
implemented anywhere in this codebase: the bot is designed to run with
API keys that have no withdrawal permission, and nothing here may ever
move funds off the exchange.  ``BinanceTradeClient`` remains as the
historical stub (still raises ``NotImplementedError``).
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlencode

import requests

BINANCE_BASE = "https://api.binance.com"
TESTNET_BASE = "https://testnet.binance.vision"

#: Official public market-data mirror (no signed endpoints!).  When the
#: production API answers 451/403 (regional block), public GETs fall
#: back here transparently — see ``BinancePublicClient._get``.
DATA_API_BASE = "https://data-api.binance.vision"

#: recvWindow for signed requests, milliseconds (Binance default is 5000).
RECV_WINDOW_MS = 5000

#: Offline fallback filters so tests / offline runs never need the network.
DEFAULT_FILTERS: dict[str, "SymbolFilters"] = {}


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    price_step: float      # PRICE_FILTER.tickSize
    qty_step: float        # LOT_SIZE.stepSize
    min_notional: float    # NOTIONAL.minNotional (or MIN_NOTIONAL)


def default_filters(symbol: str) -> SymbolFilters:
    """Conservative offline defaults (used when the network is unavailable)."""
    return SymbolFilters(symbol=symbol, price_step=0.01, qty_step=1e-5,
                         min_notional=5.0)


@dataclass(frozen=True)
class BookTicker:
    symbol: str
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


class ExchangeClient(ABC):
    """What the bot needs from an exchange."""

    @abstractmethod
    def get_klines(self, symbol: str, interval: str, start_ms: int,
                   end_ms: int, limit: int = 1000) -> list[list]:
        """Raw kline rows as returned by /api/v3/klines."""

    @abstractmethod
    def get_book_ticker(self, symbol: str) -> BookTicker:
        """Best bid/ask."""

    @abstractmethod
    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        """tickSize / stepSize / minNotional from /api/v3/exchangeInfo."""

    @abstractmethod
    def place_order(self, symbol: str, side: str, order_type: str,
                    price: float, qty: float) -> dict:
        ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: int) -> dict:
        ...


class BinancePublicClient(ExchangeClient):
    """Read-only client for the public Binance REST API (no key needed)."""

    def __init__(self, base_url: str = BINANCE_BASE, timeout: float = 15.0,
                 session: requests.Session | None = None,
                 cancel_event: "threading.Event | None" = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        # When set, rate-limit waits abort early instead of blocking the
        # calling thread (the paper loop passes its stop event here).
        self.cancel_event = cancel_event
        # Working base for PUBLIC (unsigned) GETs.  Starts at base_url;
        # switches to DATA_API_BASE after a successful mirror fallback
        # (regional 451/403 block) so later public calls skip the dead
        # production host.  Thread-safety: this is a single reference
        # assignment — atomic under the GIL; the worst case is two
        # threads independently rediscovering the mirror, which is
        # harmless.  Signed requests never read this attribute.
        self._public_base = self.base_url

    # -------------------------------------------------------------- helpers

    def _sleep(self, seconds: float) -> None:
        if self.cancel_event is None:
            time.sleep(seconds)
        elif self.cancel_event.wait(seconds):
            raise RuntimeError("запрос отменён (остановка)")

    def _request(self, method: str, path: str, params: dict | None = None,
                 headers: dict | None = None,
                 base_url: str | None = None) -> requests.Response:
        """One HTTP request with polite handling of 429/418 rate limits.

        Shared by the public GETs and the signed trading calls.  Returns
        the response WITHOUT raising on HTTP errors — callers decide how
        to surface them (plain ``raise_for_status`` for public reads,
        Binance code/msg extraction for signed calls).  ``base_url``
        overrides the target host (used by the public mirror fallback);
        signed calls never pass it, so they always hit ``self.base_url``.
        """
        base = base_url if base_url is not None else self.base_url
        for attempt in range(5):
            resp = self.session.request(method, base + path,
                                        params=params, headers=headers,
                                        timeout=self.timeout)
            if resp.status_code in (429, 418):
                wait = min(float(resp.headers.get("Retry-After", 10)), 300.0)
                self._sleep(wait)
                continue
            return resp
        raise RuntimeError(f"rate-limited too many times on {path}")

    def _get(self, path: str, params: dict) -> object:
        """Public (unsigned) GET with transparent mirror fallback.

        When the DEFAULT production base answers 451/403 (regional
        block), the same request is retried once against the official
        market-data mirror ``DATA_API_BASE``; on success the mirror
        becomes the sticky public base so later calls pay no double
        latency.  Testnet and custom bases never fall back (tests use
        custom bases; the mirror only mirrors production market data).
        """
        base = self._public_base
        resp = self._request("GET", path, params=params, base_url=base)
        if (resp.status_code in (451, 403)
                and self.base_url == BINANCE_BASE
                and base != DATA_API_BASE):
            mirror = self._request("GET", path, params=params,
                                   base_url=DATA_API_BASE)
            if mirror.status_code < 400:
                self._public_base = DATA_API_BASE  # sticky (atomic write)
                return mirror.json()
            mirror.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------ read-only

    def get_server_time(self) -> int:
        """Exchange server time, milliseconds since epoch."""
        return int(self._get("/api/v3/time", {})["serverTime"])

    def get_klines(self, symbol: str, interval: str, start_ms: int,
                   end_ms: int, limit: int = 1000) -> list[list]:
        return self._get("/api/v3/klines", {
            "symbol": symbol, "interval": interval,
            "startTime": start_ms, "endTime": end_ms, "limit": limit,
        })

    def get_book_ticker(self, symbol: str) -> BookTicker:
        d = self._get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        return BookTicker(symbol=symbol, bid=float(d["bidPrice"]),
                          ask=float(d["askPrice"]))

    def get_exchange_info(self) -> dict:
        """Full /api/v3/exchangeInfo (all symbols) — for symbol lists."""
        return self._get("/api/v3/exchangeInfo", {})

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        info = self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        sym = info["symbols"][0]
        price_step = qty_step = None
        min_notional = 0.0
        for f in sym["filters"]:
            t = f["filterType"]
            if t == "PRICE_FILTER":
                price_step = float(f["tickSize"])
            elif t == "LOT_SIZE":
                qty_step = float(f["stepSize"])
            elif t in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = float(f.get("minNotional", 0.0))
        if price_step is None or qty_step is None:
            raise RuntimeError(f"missing filters in exchangeInfo for {symbol}")
        return SymbolFilters(symbol=symbol, price_step=price_step,
                             qty_step=qty_step, min_notional=min_notional)

    # ---------------------------------------------------------- trade stubs

    def place_order(self, symbol: str, side: str, order_type: str,
                    price: float, qty: float) -> dict:
        raise NotImplementedError(
            "Live trading intentionally not implemented — verify on paper "
            "first"
        )

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        raise NotImplementedError(
            "Live trading intentionally not implemented — verify on paper "
            "first"
        )


class BinanceTradeClient(BinancePublicClient):
    """Live-trading client STUB (historical).

    Kept for backwards compatibility; the real signed implementation is
    ``BinanceSignedClient`` below.  Both methods here still raise.
    """

    def place_order(self, symbol: str, side: str, order_type: str,
                    price: float, qty: float) -> dict:
        raise NotImplementedError(
            "Live trading intentionally not implemented — verify on paper "
            "first"
        )

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        raise NotImplementedError(
            "Live trading intentionally not implemented — verify on paper "
            "first"
        )


# ----------------------------------------------------------------------------
# signed client (real trading)
# ----------------------------------------------------------------------------

def hmac_signature(secret: str, query: str) -> str:
    """HMAC-SHA256 of ``query`` keyed by ``secret``, hex-encoded.

    Exactly the Binance REST signature scheme; exposed at module level so
    tests can verify it against the known vector from the Binance docs.
    """
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def format_decimal(value: float, step: float) -> str:
    """Format ``value`` aligned to ``step`` as a PLAIN decimal string.

    Never produces scientific notation (Binance rejects it).  The number
    of decimals equals the decimals of the step (step 0.001 -> 3, step
    1 -> 0).  Values are expected to be pre-rounded to the step (the
    engine's ``round_step`` guarantees this); ROUND_HALF_UP here only
    absorbs float representation noise, it never shifts a well-formed
    value to another step.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    d_step = Decimal(str(step)).normalize()
    decimals = max(-d_step.as_tuple().exponent, 0)
    n = (Decimal(str(value)) / d_step).to_integral_value(
        rounding=ROUND_HALF_UP)
    return f"{n * d_step:.{decimals}f}"


def mask_key(api_key: str) -> str:
    """Short non-secret preview of an API key: ``AbCd…1234``."""
    k = str(api_key or "")
    if len(k) <= 8:
        return (k[:1] + "…") if k else "…"
    return f"{k[:4]}…{k[-4:]}"


class BinanceSignedClient(BinancePublicClient):
    """Signed Binance spot REST client for real trading.

    Scope is intentionally minimal: account balances, limit/market spot
    orders, cancellation and order status.  NO withdrawal, transfer,
    margin, futures or sub-account endpoints are implemented — this is
    deliberate, so the bot can never move funds off the exchange even
    if the API key mistakenly has such permissions.

    The API secret is kept in a private attribute, never logged and
    never included in ``repr``.
    """

    def __init__(self, api_key: str, api_secret: str,
                 base_url: str = BINANCE_BASE, timeout: float = 15.0,
                 session: requests.Session | None = None,
                 cancel_event: "threading.Event | None" = None):
        super().__init__(base_url=base_url, timeout=timeout,
                         session=session, cancel_event=cancel_event)
        self.api_key = api_key
        self._api_secret = api_secret  # private: never log / repr this
        self._time_offset_ms: float | None = None

    def __repr__(self) -> str:  # secret deliberately absent
        return (f"<BinanceSignedClient base={self.base_url} "
                f"key={mask_key(self.api_key)}>")

    # ------------------------------------------------------------ signing

    def _sync_time(self) -> None:
        """Cache the server-vs-local clock offset (one /api/v3/time GET)."""
        local_ms = time.time() * 1000.0
        self._time_offset_ms = self.get_server_time() - local_ms

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000.0 + (self._time_offset_ms or 0.0))

    def _signed(self, method: str, path: str,
                params: dict | None = None) -> object:
        """Send a signed request; auto-resync clock once on error -1021.

        The query string is built and signed HERE and appended to the
        URL verbatim, so the signature always matches the bytes sent.
        On HTTP error raises RuntimeError with the Binance code/msg.
        """
        if self._time_offset_ms is None:
            self._sync_time()
        for attempt in range(2):  # second pass only after a -1021 resync
            p = dict(params or {})
            p["timestamp"] = self._timestamp_ms()
            p["recvWindow"] = RECV_WINDOW_MS
            query = urlencode(p)
            query += "&signature=" + hmac_signature(self._api_secret, query)
            resp = self._request(method, f"{path}?{query}",
                                 headers={"X-MBX-APIKEY": self.api_key})
            if resp.status_code < 400:
                return resp.json()
            if resp.status_code == 451:
                # Regional block.  The data mirror has NO signed
                # endpoints, so there is nothing to fall back to — fail
                # loudly with "451" in the text so the app layer can
                # show the human explanation.
                raise RuntimeError(
                    f"Binance HTTP 451: доступ к торговым эндпоинтам "
                    f"ограничен для вашего региона ({method} {path})")
            try:
                body = resp.json()
                code, msg = body.get("code"), body.get("msg")
            except Exception:
                code, msg = None, (resp.text or "")[:200]
            if code == -1021 and attempt == 0:
                self._sync_time()  # clock drifted: resync and retry once
                continue
            raise RuntimeError(
                f"Binance error {code}: {msg} "
                f"(HTTP {resp.status_code}, {method} {path})")
        raise RuntimeError(f"request kept failing with -1021 on {path}")

    # ------------------------------------------------------------- account

    def get_balances(self) -> dict[str, float]:
        """Free (available) balances per asset, zero balances omitted."""
        try:
            acct = self._signed("GET", "/api/v3/account",
                                {"omitZeroBalances": "true"})
        except RuntimeError:
            # older/other deployments may not support the parameter
            acct = self._signed("GET", "/api/v3/account")
        out: dict[str, float] = {}
        for b in acct.get("balances", []):
            free = float(b.get("free", 0.0))
            if free > 0:
                out[b["asset"]] = free
        return out

    # -------------------------------------------------------------- orders

    def _steps_for(self, symbol: str,
                   price_step: float | None,
                   qty_step: float | None) -> tuple[float, float]:
        if price_step is not None and qty_step is not None:
            return price_step, qty_step
        f = self.get_symbol_filters(symbol)
        return (price_step if price_step is not None else f.price_step,
                qty_step if qty_step is not None else f.qty_step)

    def place_limit(self, symbol: str, side: str, price: float, qty: float,
                    client_order_id: str | None = None,
                    price_step: float | None = None,
                    qty_step: float | None = None) -> dict:
        """LIMIT GTC order; price/qty formatted to the exchange steps."""
        p_step, q_step = self._steps_for(symbol, price_step, qty_step)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": format_decimal(qty, q_step),
            "price": format_decimal(price, p_step),
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return self._signed("POST", "/api/v3/order", params)

    def place_market_quote(self, symbol: str, side: str, quote_qty: float,
                           client_order_id: str | None = None) -> dict:
        """MARKET order sized in QUOTE currency (quoteOrderQty).

        The response includes ``executedQty`` (base actually traded) and
        ``cummulativeQuoteQty`` (quote actually spent/received).
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            # 8 decimals covers quoteAssetPrecision on Binance spot pairs
            "quoteOrderQty": format_decimal(quote_qty, 1e-8),
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return self._signed("POST", "/api/v3/order", params)

    def cancel(self, symbol: str, orig_client_order_id: str) -> dict:
        return self._signed("DELETE", "/api/v3/order", {
            "symbol": symbol,
            "origClientOrderId": orig_client_order_id,
        })

    def get_open_orders(self, symbol: str) -> list:
        return self._signed("GET", "/api/v3/openOrders", {"symbol": symbol})

    def get_order(self, symbol: str, orig_client_order_id: str) -> dict:
        return self._signed("GET", "/api/v3/order", {
            "symbol": symbol,
            "origClientOrderId": orig_client_order_id,
        })

    # ----------------------------------------------------- ABC delegation

    def place_order(self, symbol: str, side: str, order_type: str,
                    price: float, qty: float) -> dict:
        if order_type.upper() != "LIMIT":
            raise ValueError(f"unsupported order type {order_type!r}; "
                             "use place_market_quote for MARKET")
        return self.place_limit(symbol, side, price, qty)

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        return self._signed("DELETE", "/api/v3/order", {
            "symbol": symbol,
            "orderId": order_id,
        })


def fetch_filters_or_default(symbol: str,
                             client: ExchangeClient | None = None
                             ) -> tuple[SymbolFilters, bool]:
    """Try the exchange; fall back to offline defaults.

    Returns (filters, from_network).
    """
    cl = client or BinancePublicClient()
    try:
        return cl.get_symbol_filters(symbol), True
    except Exception:
        return default_filters(symbol), False
