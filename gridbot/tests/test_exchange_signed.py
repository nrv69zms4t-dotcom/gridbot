"""BinanceSignedClient: signature, formatting, resync, errors. NO network.

The HTTP layer is replaced by a scripted fake ``requests.Session`` that
VERIFIES every signature it receives, so signing is tested end-to-end.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from gridbot.exchange import (BINANCE_BASE, TESTNET_BASE,
                              BinanceSignedClient, format_decimal,
                              hmac_signature, mask_key)

# Known test vector from the official Binance API docs (SIGNED endpoint
# example) — verifiable by hand with any HMAC-SHA256 implementation.
DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
DOC_QUERY = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC"
             "&quantity=1&price=0.1&recvWindow=5000"
             "&timestamp=1499827319559")
DOC_SIG = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"

SECRET = "test-secret"
KEY = "TESTKEY1234567890"


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Scripted session: routes by path and VERIFIES signatures."""

    def __init__(self, secret=SECRET, server_time=1_500_000_000_000):
        self.secret = secret
        self.server_time = server_time
        self.calls: list[dict] = []
        self.order_responses: list[FakeResponse] = []
        self.account_payload = {"balances": [
            {"asset": "BTC", "free": "0.5", "locked": "0"},
            {"asset": "USDT", "free": "1000.25", "locked": "0"},
            {"asset": "ETH", "free": "0.00000000", "locked": "0"},
        ]}

    def request(self, method, url, params=None, headers=None, timeout=None):
        path, _, query = url.partition("?")
        self.calls.append({"method": method, "path": path, "query": query,
                           "headers": dict(headers or {})})
        if path.endswith("/api/v3/time"):
            return FakeResponse(200, {"serverTime": self.server_time})
        # everything else must be signed correctly
        assert "&signature=" in query, "signed request without signature"
        base, _, sig = query.rpartition("&signature=")
        assert sig == hmac_signature(self.secret, base), "BAD SIGNATURE"
        if path.endswith("/api/v3/account"):
            return FakeResponse(200, self.account_payload)
        if path.endswith("/api/v3/order"):
            if self.order_responses:
                return self.order_responses.pop(0)
            return FakeResponse(200, {"status": "NEW",
                                      "clientOrderId": "x"})
        return FakeResponse(400, {"code": -1000, "msg": "unknown path"})


def make_client(session=None) -> tuple[BinanceSignedClient, FakeSession]:
    s = session or FakeSession()
    return BinanceSignedClient(KEY, SECRET, base_url=TESTNET_BASE,
                               session=s), s


# ---------------------------------------------------------------------------
# signature and formatting primitives
# ---------------------------------------------------------------------------

class TestSignaturePrimitives:
    def test_known_vector_from_binance_docs(self):
        assert hmac_signature(DOC_SECRET, DOC_QUERY) == DOC_SIG

    def test_format_decimal_no_scientific_notation(self):
        cases = [
            (0.000027, 1e-6, "0.000027"),
            (105.0, 0.01, "105.00"),
            (1e-05, 1e-5, "0.00001"),
            (30000.1, 0.1, "30000.1"),
            (0.095, 0.001, "0.095"),
            (7.0, 1.0, "7"),
            (123456.0, 0.01, "123456.00"),
        ]
        for value, step, expected in cases:
            s = format_decimal(value, step)
            assert s == expected, (value, step)
            assert "e" not in s.lower()

    def test_format_decimal_absorbs_float_noise(self):
        # 0.1 + 0.2 style noise must not shift a step-aligned value
        assert format_decimal(0.30000000000000004, 0.01) == "0.30"
        assert format_decimal(104.99999999999999, 0.01) == "105.00"

    def test_format_decimal_rejects_bad_step(self):
        with pytest.raises(ValueError):
            format_decimal(1.0, 0.0)

    def test_mask_key(self):
        assert mask_key("ABCDEFGHIJKLMNOP") == "ABCD…MNOP"
        assert mask_key("short") == "s…"
        assert mask_key("") == "…"


# ---------------------------------------------------------------------------
# signed request mechanics
# ---------------------------------------------------------------------------

class TestSignedRequests:
    def test_header_recv_window_and_server_timestamp(self):
        client, session = make_client()
        balances = client.get_balances()
        assert balances == {"BTC": 0.5, "USDT": 1000.25}  # zero ETH dropped

        acct_calls = [c for c in session.calls
                      if c["path"].endswith("/api/v3/account")]
        assert acct_calls, "no account call recorded"
        call = acct_calls[0]
        assert call["headers"].get("X-MBX-APIKEY") == KEY
        q = parse_qs(call["query"])
        assert q["recvWindow"] == ["5000"]
        # timestamp must be server-adjusted: FakeSession's clock is far
        # in the past, so the offset must bring us within a minute of it
        ts = int(q["timestamp"][0])
        assert abs(ts - session.server_time) < 60_000

    def test_time_synced_once_and_cached(self):
        client, session = make_client()
        client.get_balances()
        client.get_balances()
        time_calls = [c for c in session.calls
                      if c["path"].endswith("/api/v3/time")]
        assert len(time_calls) == 1

    def test_resync_and_single_retry_on_1021(self):
        session = FakeSession()
        session.order_responses = [
            FakeResponse(400, {"code": -1021,
                               "msg": "Timestamp outside recvWindow"}),
            FakeResponse(200, {"status": "NEW", "clientOrderId": "ok1"}),
        ]
        client, _ = make_client(session)
        r = client.place_limit("BTCUSDT", "BUY", 100.0, 0.001,
                               client_order_id="ok1",
                               price_step=0.01, qty_step=0.001)
        assert r["status"] == "NEW"
        # initial sync + one resync after -1021
        time_calls = [c for c in session.calls
                      if c["path"].endswith("/api/v3/time")]
        assert len(time_calls) == 2

    def test_persistent_1021_raises(self):
        session = FakeSession()
        session.order_responses = [
            FakeResponse(400, {"code": -1021, "msg": "still drifting"}),
            FakeResponse(400, {"code": -1021, "msg": "still drifting"}),
        ]
        client, _ = make_client(session)
        with pytest.raises(RuntimeError, match="-1021"):
            client.place_limit("BTCUSDT", "BUY", 100.0, 0.001,
                               client_order_id="x",
                               price_step=0.01, qty_step=0.001)

    def test_http_error_includes_binance_code_and_msg(self):
        session = FakeSession()
        session.order_responses = [
            FakeResponse(400, {"code": -2010,
                               "msg": "Account has insufficient balance"}),
        ]
        client, _ = make_client(session)
        with pytest.raises(RuntimeError) as ei:
            client.place_limit("BTCUSDT", "BUY", 100.0, 0.001,
                               client_order_id="x",
                               price_step=0.01, qty_step=0.001)
        assert "-2010" in str(ei.value)
        assert "insufficient balance" in str(ei.value)

    def test_limit_order_params_formatted(self):
        client, session = make_client()
        client.place_limit("BTCUSDT", "SELL", 30000.1, 0.00051,
                           client_order_id="gbdeadbe-7",
                           price_step=0.1, qty_step=1e-5)
        order_call = [c for c in session.calls
                      if c["path"].endswith("/api/v3/order")][0]
        q = parse_qs(order_call["query"])
        assert q["type"] == ["LIMIT"]
        assert q["timeInForce"] == ["GTC"]
        assert q["price"] == ["30000.1"]
        assert q["quantity"] == ["0.00051"]
        assert q["newClientOrderId"] == ["gbdeadbe-7"]

    def test_market_quote_params(self):
        client, session = make_client()
        client.place_market_quote("BTCUSDT", "BUY", 123.45,
                                  client_order_id="gbdeadbe-m")
        q = parse_qs([c for c in session.calls
                      if c["path"].endswith("/api/v3/order")][0]["query"])
        assert q["type"] == ["MARKET"]
        assert q["quoteOrderQty"] == ["123.45000000"]
        assert "price" not in q


# ---------------------------------------------------------------------------
# safety guarantees
# ---------------------------------------------------------------------------

class TestSafety:
    def test_repr_never_contains_secret(self):
        client, _ = make_client()
        r = repr(client)
        assert SECRET not in r
        assert KEY not in r          # only the masked preview
        assert mask_key(KEY) in r

    def test_no_withdrawal_or_transfer_methods(self):
        # deliberate: this client must never be able to move funds off
        # the exchange (see the class docstring in exchange.py)
        client, _ = make_client()
        for name in dir(client):
            low = name.lower()
            assert "withdraw" not in low
            assert "transfer" not in low

    def test_default_base_is_mainnet_and_testnet_available(self):
        assert BINANCE_BASE.startswith("https://api.binance.com")
        assert TESTNET_BASE == "https://testnet.binance.vision"
