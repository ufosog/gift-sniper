import time

from gift_sniper import db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def test_run_seconds_stops_itself_and_prints_summary(capsys, monkeypatch):
    # POLL_INTERVAL_SEC-driven sleep would make this test slow; patch
    # time.sleep used by Poller.run_forever's inter-cycle wait so the test
    # runs fast without weakening what's being verified (that run_seconds
    # actually terminates the loop and a summary gets printed).
    monkeypatch.setattr("gift_sniper.poller.time.sleep", lambda s: None)

    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[[], [], []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    poller = Poller(conn, client, auth, floor_cache)

    start = time.monotonic()
    poller.run_forever(run_seconds=0.05)
    elapsed = time.monotonic() - start

    assert elapsed < 5  # did not hang / run forever
    out = capsys.readouterr().out
    assert "Gift Sniper run summary" in out
    assert "new_listings: 0" in out
