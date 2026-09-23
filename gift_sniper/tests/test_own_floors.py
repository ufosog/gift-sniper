from datetime import datetime, timezone

from gift_sniper import config, db
from gift_sniper.models import FloorSnapshot, Listing
from gift_sniper.own_floors import own_combo_floor


def _listing(ext_id, collection_name, model_name, backdrop_name, price_units, seen_offset_sec=0):
    seen_at = datetime(2026, 9, 5, 10, 0, seen_offset_sec, tzinfo=timezone.utc)
    return Listing(
        marketplace="portals",
        external_id=ext_id,
        tg_id=f"{ext_id}-tg",
        collection_id=f"col-{collection_name}",
        collection_name=collection_name,
        gift_number=1,
        price_nano=int(price_units * config.NANO),
        currency="TON",
        collection_floor_nano=None,
        model_name=model_name,
        symbol_name=None,
        backdrop_name=backdrop_name,
        model_rarity_raw=None,
        symbol_rarity_raw=None,
        backdrop_rarity_raw=None,
        image_url=None,
        animation_url=None,
        listed_at=seen_at,
        unlocks_at=None,
        status="listed",
        first_seen_at=seen_at,
        raw={},
    )


def _snapshot(listing: Listing) -> FloorSnapshot:
    return FloorSnapshot(
        listing_external_id=listing.external_id,
        model_name=listing.model_name,
        backdrop_name=listing.backdrop_name,
        api_combo_floor_nano=None,
        model_min_floor_nano=None,
        floor_fetched_at=listing.first_seen_at,
        floor_age_sec=0,
        raw_model_block={},
    )


def _seed(conn, listings):
    for l in listings:
        db.upsert_listing_with_floor(conn, l, _snapshot(l))


def test_own_floor_does_not_mix_collections_with_same_model_and_backdrop():
    conn = db.connect(":memory:")
    listings = [
        _listing("a1", "CollectionA", "SharedModel", "Copper", price_units=10, seen_offset_sec=0),
        _listing("a2", "CollectionA", "SharedModel", "Copper", price_units=15, seen_offset_sec=1),
        _listing("b1", "CollectionB", "SharedModel", "Copper", price_units=100, seen_offset_sec=0),
        _listing("b2", "CollectionB", "SharedModel", "Copper", price_units=150, seen_offset_sec=1),
    ]
    _seed(conn, listings)

    as_of = datetime(2026, 9, 5, 10, 0, 5, tzinfo=timezone.utc)
    floor_a = own_combo_floor(conn, "CollectionA", "SharedModel", "Copper", as_of)
    floor_b = own_combo_floor(conn, "CollectionB", "SharedModel", "Copper", as_of)

    assert floor_a.floor_nano == int(10 * config.NANO)
    assert floor_b.floor_nano == int(100 * config.NANO)
    # This is the assertion that fails if collection_name is dropped from
    # the grouping key: without it, floor_a and floor_b would both come
    # back as min(10) mixed across both collections.
    assert floor_a.floor_nano != floor_b.floor_nano


def test_confidence_thresholds():
    conn = db.connect(":memory:")
    as_of = datetime(2026, 9, 5, 10, 0, 5, tzinfo=timezone.utc)

    assert own_combo_floor(conn, "X", "M", "B", as_of).confidence == "none"

    listings_2 = [_listing(f"n2-{i}", "X", "M", "B", 10 + i, i) for i in range(2)]
    _seed(conn, listings_2)
    assert own_combo_floor(conn, "X", "M", "B", as_of).confidence == "low"

    conn2 = db.connect(":memory:")
    listings_5 = [_listing(f"n5-{i}", "X", "M", "B", 10 + i, i) for i in range(5)]
    _seed(conn2, listings_5)
    assert own_combo_floor(conn2, "X", "M", "B", as_of).confidence == "medium"

    conn3 = db.connect(":memory:")
    listings_12 = [_listing(f"n12-{i}", "X", "M", "B", 10 + i, i) for i in range(12)]
    _seed(conn3, listings_12)
    as_of_later = datetime(2026, 9, 5, 10, 1, 0, tzinfo=timezone.utc)  # past all offsets (max 11s)
    assert own_combo_floor(conn3, "X", "M", "B", as_of_later).confidence == "high"
