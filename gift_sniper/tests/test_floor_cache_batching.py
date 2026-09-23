from gift_sniper import db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.poller import Poller
from .conftest import load_fixture
from .fakes import FakePortalsClient


def test_two_listings_same_model_one_floor_request_in_analytics_pass():
    """Floor batching now happens in the ANALYTICS PATH (run_floor_worker),
    not inline during poll_once (FAST PATH) -- see test_fast_path_no_floor_
    requests.py for the test that poll_once makes zero floor requests.
    """
    a = load_fixture("listing_full.json")
    b = dict(a)
    b["id"] = "a1b2c3d4-9999"  # same model "Inception", different listing
    batch = [a, b]

    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[batch, []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    poller = Poller(conn, client, auth, floor_cache)

    new_listings = poller.poll_once()
    assert len(new_listings) == 2
    assert client.floors_calls == []  # nothing during FAST PATH

    processed = poller.run_floor_worker()
    assert processed == 2
    assert len(client.floors_calls) == 1
    assert client.floors_calls[0] == ["Inception"]
