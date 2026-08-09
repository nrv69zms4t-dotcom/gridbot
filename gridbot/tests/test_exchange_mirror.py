"""Public-data mirror fallback: 451/403 -> data-api.binance.vision.

NO network — the HTTP layer is a scripted fake session where the
production base is regionally blocked and the official market-data
mirror answers normally.  Verifies: transparent one-retry fallback,
the sticky public base (no double latency afterwards), no fallback for
testnet/custom bases, and that SIGNED requests never touch the mirror.
"""

from __future__ import annotations

import json

import pytest

from gridbot.exchange import (BINANCE_BASE, DATA_API_BASE, TESTNET_BASE,
                              BinancePublicClient, BinanceSignedClient)


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


PUBLIC_PAYLOADS = {
    "/api/v3/ping": {},
    "/api/v3/time": {"serverTime": 1_700_000_000_000},
    "/api/v3/ticker/bookTicker": {"symbol": "BTCUSDT",
                                  "bidPrice": "100.0", "askPrice": "102.0"},
    "/api/v3/klines": [[1_700_000_000_000, "1", "2", "0.5", "1.5", "10",
                        1_700_000_059_999, "0", 0, "0", "0", "0"]],
}


class BlockedProdSession:
    """Any non-mirror host answers ``blocked_status``; mirror answers 200."""

    def __init__(self, blocked_status: int = 451):
        self.blocked_status = blocked_status
        self.calls: list[tuple[str, str]] = []   # (method, full url)

    def request(self, method, url, params=None, headers=None, timeout=None):
        self.calls.append((method, url))
        if url.startswith(DATA_API_BASE):
            path = url[len(DATA_API_BASE):].partition("?")[0]
            return FakeResponse(200, PUBLIC_PAYLOADS.get(path, {}))
        return FakeResponse(self.blocked_status, {"msg": "blocked"})

    def mirror_urls(self) -> list[str]:
        return [u for _, u in self.calls if u.startswith(DATA_API_BASE)]

    def prod_urls(self) -> list[str]:
        return [u for _, u in self.calls if not u.startswith(DATA_API_BASE)]


# ---------------------------------------------------------------------------
# public GETs fall back and remember the working base
# ---------------------------------------------------------------------------

class TestPublicMirrorFallback:
    def test_451_falls_back_once_then_sticky(self):
        s = BlockedProdSession(451)
        c = BinancePublicClient(session=s)
        t = c.get_book_ticker("BTCUSDT")
        assert t.bid == 100.0 and t.ask == 102.0
        # first call: one blocked prod attempt + one mirror retry
        assert len(s.prod_urls()) == 1
        assert len(s.mirror_urls()) == 1
        # subsequent public calls go STRAIGHT to the mirror (sticky base)
        c.get_klines("BTCUSDT", "1m", 0, 10 ** 15)
        c.get_server_time()
        assert len(s.prod_urls()) == 1           # unchanged
        assert len(s.mirror_urls()) == 3

    def test_403_falls_back_too(self):
        s = BlockedProdSession(403)
        c = BinancePublicClient(session=s)
        assert c.get_server_time() == 1_700_000_000_000
        assert len(s.mirror_urls()) == 1

    def test_testnet_base_never_falls_back(self):
        s = BlockedProdSession(451)
        c = BinancePublicClient(base_url=TESTNET_BASE, session=s)
        with pytest.raises(RuntimeError, match="451"):
            c.get_book_ticker("BTCUSDT")
        assert s.mirror_urls() == []

    def test_custom_base_never_falls_back(self):
        s = BlockedProdSession(451)
        c = BinancePublicClient(base_url="https://example.invalid",
                                session=s)
        with pytest.raises(RuntimeError, match="451"):
            c.get_server_time()
        assert s.mirror_urls() == []

    def test_mirror_also_blocked_raises_and_not_sticky(self):
        class AllBlocked:
            def __init__(self):
                self.calls: list[str] = []

            def request(self, method, url, params=None, headers=None,
                        timeout=None):
                self.calls.append(url)
                return FakeResponse(451, {})

        s = AllBlocked()
        c = BinancePublicClient(session=s)
        with pytest.raises(RuntimeError, match="451"):
            c.get_server_time()
        # base did NOT stick to the (dead) mirror: the next call probes
        # production first again — 2 requests per attempt, both times
        with pytest.raises(RuntimeError, match="451"):
            c.get_server_time()
        prod = [u for u in s.calls if u.startswith(BINANCE_BASE)]
        mirror = [u for u in s.calls if u.startswith(DATA_API_BASE)]
        assert len(prod) == 2 and len(mirror) == 2


# ---------------------------------------------------------------------------
# signed requests never touch the mirror (it has no signed endpoints)
# ---------------------------------------------------------------------------

class TestSignedNeverFallsBack:
    def test_signed_451_raises_without_mirror_retry(self):
        s = BlockedProdSession(451)
        client = BinanceSignedClient("KEY", "SECRET", session=s)
        with pytest.raises(RuntimeError, match="451"):
            client.place_limit("BTCUSDT", "BUY", 100.0, 0.001,
                               price_step=0.01, qty_step=0.001)
        # the signed order request hit production only — never the mirror
        signed = [u for _, u in s.calls if "signature=" in u]
        assert signed, "no signed request recorded"
        assert all(u.startswith(BINANCE_BASE) for u in signed)
        assert all("/api/v3/order" not in u for u in s.mirror_urls())

    def test_signed_client_public_reads_do_use_mirror(self):
        # chart/status data must keep working even when trading does not
        s = BlockedProdSession(451)
        client = BinanceSignedClient("KEY", "SECRET", session=s)
        t = client.get_book_ticker("BTCUSDT")
        assert t.mid == 101.0
        assert len(s.mirror_urls()) == 1
