from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.poller import Poller
from .conftest import load_fixture
from .fakes import FakePortalsClient


def test_same_batch_twice_yields_no_dupes(monkeypatch):
    # This test is about dedup, not about COLLECT_MIN_PRICE -- one of the
    # fixtures (listing_no_symbol.json, price 9.5) is below the current
    # COLLECT_MIN_PRICE default (15), which is a separate concern.
    monkeypatch.setattr(config, "COLLECT_MIN_PRICE_NANO", 0)
    batch = [load_fixture("listing_full.json"), load_fixture("listing_no_symbol.json")]
    conn = db.connect(":memory:")
    # offset=0 -> batch, offset=50 -> empty (end of pagination for this iteration)
    client = FakePortalsClient(pages=[batch, []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    poller = Poller(conn, client, auth, floor_cache)

    first = poller.poll_once()
    assert len(first) == 2

    second = poller.poll_once()
    assert second == []

    rows = conn.execute("SELECT COUNT(*) FROM listings").fetchone()
    assert rows[0] == 2
