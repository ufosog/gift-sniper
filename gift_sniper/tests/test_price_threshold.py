from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def _mk_item(ext_id: str, price: str, model: str) -> dict:
    return {
        "id": ext_id,
        "tg_id": f"{ext_id}-tg",
        "collection_id": "col-x",
        "name": "Collection X",
        "external_collection_number": 1,
        "price": price,
        "floor_price": "9.0",
        "photo_url": None,
        "animation_url": None,
        "listed_at": "2026-09-05T10:00:00Z",
        "unlocks_at": None,
        "status": "listed",
        "attributes": [
            {"type": "model", "value": model, "rarity_per_mille": 1.0},
            {"type": "backdrop", "value": "Copper", "rarity_per_mille": 1.0},
        ],
    }


def test_below_threshold_listing_written_but_floor_not_requested(monkeypatch):
    # This test is specifically about FLOOR_MIN_PRICE (skip the floor
    # lookup, but still write the listing) -- COLLECT_MIN_PRICE is a
    # separate, independent threshold (see test_collect_min_price.py for
    # that one) and must not interfere here.
    monkeypatch.setattr(config, "COLLECT_MIN_PRICE_NANO", 0)

    cheap = _mk_item("cheap-1", "5", "CheapModel")  # below FLOOR_MIN_PRICE
    pricey = _mk_item("pricey-1", "20", "PriceyModel")  # above threshold

    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[[cheap, pricey], []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    poller = Poller(conn, client, auth, floor_cache)

    result = poller.poll_once()
    assert len(result) == 2
    assert client.floors_calls == []  # FAST PATH makes no floor requests at all

    # Both listings are in the DB regardless of price, both "pending".
    count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert count == 2
    statuses = {
        r[0]: r[1]
        for r in conn.execute("SELECT listing_external_id, pair_floor_status FROM floor_snapshots")
    }
    assert statuses == {"cheap-1": "pending", "pricey-1": "pending"}

    poller.run_floor_worker()

    # Floor lookup requested only for the model above the threshold --
    # the cheap one was resolved to 'no_data' with zero network calls.
    assert client.floors_calls == [["PriceyModel"]]

    rows = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT listing_external_id, floor_skip_reason FROM floor_snapshots"
        )
    }
    assert rows["cheap-1"] == "below_price_threshold"
    assert rows["pricey-1"] is None
