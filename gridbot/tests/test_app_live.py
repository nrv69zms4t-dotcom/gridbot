"""App backend live methods + DPAPI secrets store — NO network.

Injection points: ``signed_client_factory`` returns a FakeSignedClient,
``client`` is the offline StaticTickerClient, ``data_dir`` is tmp_path.
The secrets store uses REAL Windows DPAPI (this app is Windows-only).
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from gridbot.app import secrets_store
from gridbot.app.api import Api
from gridbot.exchange import BookTicker
from gridbot.paper import StaticTickerClient
from gridbot.tests.fake_signed import FakeSignedClient

CFG = {
    "symbol": "TESTUSDT",
    "lower": 100.0,
    "upper": 110.0,
    "levels": 11,
    "spacing": "arithmetic",
    "budget": 1100.0,
    "fee": 0.001,
    "poll": 1.0,
}

windows_only = pytest.mark.skipif(sys.platform != "win32",
                                  reason="DPAPI is Windows-only")


def make_api(tmp_path, fake=None) -> tuple[Api, FakeSignedClient]:
    fake = fake or FakeSignedClient()
    client = StaticTickerClient(
        [BookTicker("TESTUSDT", bid=104.99, ask=105.01)])
    api = Api(client=client, data_dir=tmp_path,
              signed_client_factory=lambda network, cancel_event: fake)
    return api, fake


def save_keys(api: Api) -> dict:
    return api.save_api_keys("MYTESTKEY1234567890", "my-test-secret-value")


def wait_until(cond, timeout=8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# secrets store (real DPAPI round trip)
# ---------------------------------------------------------------------------

@windows_only
class TestSecretsStore:
    def test_roundtrip(self, tmp_path):
        secrets_store.save_credentials(tmp_path, "KeyА-123",
                                       "секрет/secret+=")
        path = tmp_path / secrets_store.CRED_FILENAME
        assert path.exists()
        # ciphertext must not contain the plaintext
        blob = path.read_bytes()
        assert b"secret+=" not in blob
        assert secrets_store.load_credentials(tmp_path) == \
               ("KeyА-123", "секрет/secret+=")

    def test_load_missing_returns_none(self, tmp_path):
        assert secrets_store.load_credentials(tmp_path) is None

    def test_clear(self, tmp_path):
        secrets_store.save_credentials(tmp_path, "k1234567890", "s")
        assert secrets_store.clear_credentials(tmp_path) is True
        assert secrets_store.load_credentials(tmp_path) is None
        assert secrets_store.clear_credentials(tmp_path) is False

    def test_corrupt_file_raises_russian(self, tmp_path):
        (tmp_path / secrets_store.CRED_FILENAME).write_bytes(b"garbage")
        with pytest.raises(RuntimeError, match="расшифровать"):
            secrets_store.load_credentials(tmp_path)

    def test_key_preview_masks(self):
        assert secrets_store.key_preview("ABCDEFGHIJKLMNOP") == "ABCD…MNOP"
        assert secrets_store.key_preview("tiny") == "t…"


# ---------------------------------------------------------------------------
# api key management (never returns the secret)
# ---------------------------------------------------------------------------

@windows_only
class TestApiKeys:
    def test_status_save_clear_flow(self, tmp_path):
        api, _ = make_api(tmp_path)
        assert api.get_api_status() == {"ok": True, "saved": False,
                                        "key_preview": None}
        r = save_keys(api)
        assert r["ok"] is True
        assert r["key_preview"] == "MYTE…7890"
        # the secret must never appear in any api response
        assert "my-test-secret-value" not in json.dumps(r)
        st = api.get_api_status()
        assert st["saved"] is True and st["key_preview"] == "MYTE…7890"
        assert "my-test-secret-value" not in json.dumps(st)
        assert api.clear_api_keys()["ok"] is True
        assert api.get_api_status()["saved"] is False

    def test_empty_keys_rejected(self, tmp_path):
        api, _ = make_api(tmp_path)
        r = api.save_api_keys("", "")
        assert r["ok"] is False and "ключ" in r["error"].lower()

    def test_test_connection(self, tmp_path):
        api, fake = make_api(tmp_path)
        # without keys -> polite Russian error
        r = api.test_connection("testnet")
        assert r["ok"] is False and "ключ" in r["error"].lower()
        save_keys(api)
        r = api.test_connection("testnet")
        assert r["ok"] is True
        assert r["network"] == "testnet"
        assert r["balances"].get("USDT") == pytest.approx(100000.0)
        r2 = api.test_connection("почта")
        assert r2["ok"] is False


# ---------------------------------------------------------------------------
# start_live validation
# ---------------------------------------------------------------------------

@windows_only
class TestStartLiveValidation:
    def test_requires_saved_keys(self, tmp_path):
        api, _ = make_api(tmp_path)
        r = api.start_live(CFG, "testnet", "")
        assert r["ok"] is False
        assert "ключ" in r["error"].lower()

    def test_mainnet_requires_symbol_confirmation(self, tmp_path):
        api, _ = make_api(tmp_path)
        save_keys(api)
        for bad in ("", "BTCUSDT", "testusd"):
            r = api.start_live(CFG, "mainnet", bad)
            assert r["ok"] is False
            assert "Подтверждение не совпадает" in r["error"], bad
        # nothing was started
        assert api.get_status()["running"] is False

    def test_unknown_network_rejected(self, tmp_path):
        api, _ = make_api(tmp_path)
        save_keys(api)
        r = api.start_live(CFG, "prod", "TESTUSDT")
        assert r["ok"] is False and "сеть" in r["error"].lower()

    def test_config_validation_still_applies(self, tmp_path):
        api, _ = make_api(tmp_path)
        save_keys(api)
        r = api.start_live(dict(CFG, lower=110.0, upper=100.0),
                           "testnet", "")
        assert r["ok"] is False and "меньше верхней" in r["error"]


# ---------------------------------------------------------------------------
# live lifecycle through the app
# ---------------------------------------------------------------------------

@windows_only
class TestLiveLifecycle:
    def test_testnet_start_status_fill_stop(self, tmp_path):
        api, fake = make_api(tmp_path)
        save_keys(api)
        r = api.start_live(CFG, "testnet", "")
        assert r["ok"] is True and r["network"] == "testnet"

        assert wait_until(lambda: len(fake.open) == 10)
        assert wait_until(lambda: api.get_status()["price"] is not None)
        st = api.get_status()
        assert st["running"] is True
        assert st["mode"] == "live"
        assert st["network"] == "testnet"
        assert st["balances"] is not None
        assert st["balances"]["quote_asset"] == "USDT"
        assert st["balances"]["base_asset"] == "TEST"
        json.dumps(st)  # must stay JSON-serializable

        # paper is mutually exclusive with live
        rp = api.start_paper(CFG)
        assert rp["ok"] is False and "live" in rp["error"].lower()
        # double live start refused too
        assert api.start_live(CFG, "testnet", "")["ok"] is False

        # a real fill propagates into the engine
        coid = fake.find_open("BUY", 104.0)
        fake.fill_order(coid)
        assert wait_until(lambda: api.get_status()["n_trades"] >= 1)

        stop = api.stop_live(cancel_orders=True)
        assert stop["ok"] is True
        assert fake.open == {}          # everything cancelled on stop
        st2 = api.get_status()
        assert st2["running"] is False and st2["mode"] is None
        # stopping again -> polite error
        assert api.stop_live(True)["ok"] is False

    def test_stop_live_keep_orders(self, tmp_path):
        api, fake = make_api(tmp_path)
        save_keys(api)
        assert api.start_live(CFG, "testnet", "")["ok"] is True
        assert wait_until(lambda: len(fake.open) == 10)
        assert api.stop_live(cancel_orders=False)["ok"] is True
        assert len(fake.open) == 10     # left resting on the exchange

    def test_mainnet_with_correct_confirmation(self, tmp_path):
        api, fake = make_api(tmp_path)
        save_keys(api)
        r = api.start_live(CFG, "mainnet", "TESTUSDT")
        assert r["ok"] is True and r["network"] == "mainnet"
        assert wait_until(lambda: api.get_status()["mode"] == "live")
        assert api.get_status()["network"] == "mainnet"
        assert api.stop_live(True)["ok"] is True

    def test_live_refused_while_paper_running(self, tmp_path):
        api, _ = make_api(tmp_path)
        save_keys(api)
        assert api.start_paper(CFG)["ok"] is True
        try:
            assert wait_until(
                lambda: api.get_status()["price"] is not None)
            r = api.start_live(CFG, "testnet", "")
            assert r["ok"] is False
            assert "paper" in r["error"].lower()
        finally:
            api.stop_paper()

    def test_start_failure_surfaces_as_last_error(self, tmp_path):
        fake = FakeSignedClient(balances={"USDT": 5.0})  # too poor
        api, _ = make_api(tmp_path, fake=fake)
        save_keys(api)
        assert api.start_live(CFG, "testnet", "")["ok"] is True
        assert wait_until(
            lambda: api.get_status()["last_error"] is not None)
        err = api.get_status()["last_error"]
        assert "Недостаточно средств" in err
        assert fake.open == {}
