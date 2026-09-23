import os

import pytest

from gift_sniper import step0


def test_empty_token_exits_nonzero_and_makes_no_network_calls(monkeypatch):
    monkeypatch.delenv("PORTALS_AUTH", raising=False)

    called = {"count": 0}

    def client_factory():
        called["count"] += 1
        raise AssertionError("client_factory must not be called when token is empty")

    rc = step0.main(client_factory=client_factory)

    assert rc != 0
    assert called["count"] == 0


def test_with_token_runs_probes_via_injected_client(monkeypatch, capsys):
    monkeypatch.setenv("PORTALS_AUTH", "fake-token-for-test")

    class FakeClient:
        def search(self, limit, offset, sort=None):
            return {"results": [], "total_count": 0}

        def model_backgrounds_floors(self, model_names):
            return {"model_backgrounds": {}}

        def market_config(self):
            return {"usd_course": "1.42"}

    rc = step0.main(client_factory=FakeClient)

    assert rc == 0
    out = capsys.readouterr().out
    assert "fake-token-for-test" not in out
    assert "MARKET CONFIG" in out
