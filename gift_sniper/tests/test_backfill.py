from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.backfill import run_backfill
from gift_sniper.models import FloorSnapshot, Listing


def _listing(ext_id) -> Listing:
    now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    return Listing(
        marketplace="portals", external_id=ext_id, tg_id=f"{ext_id}-tg",
        collection_id="col-a", collection_name="CollA", gift_number=1,
        price_nano=int(Decimal("20") * config.NANO), currency="TON",
        collection_floor_nano=None, model_name="M", symbol_name=None,
        backdrop_name="B", model_rarity_raw=None, symbol_rarity_raw=None,
        backdrop_rarity_raw=None, image_url=None, animation_url=None,
        listed_at=now, unlocks_at=None, status="listed", first_seen_at=now, raw={},
    )


def _snapshot_with_raw(listing, raw_model_block) -> FloorSnapshot:
    return FloorSnapshot(
        listing_external_id=listing.external_id, model_name=listing.model_name,
        backdrop_name=listing.backdrop_name, api_combo_floor_nano=None,
        model_min_floor_nano=None, floor_fetched_at=listing.first_seen_at,
        floor_age_sec=0, raw_model_block=raw_model_block, pair_floor_status="ok",
    )


def test_current_real_world_shape_is_always_left_null_with_reason():
    """raw_model_block, as actually populated by floors.py, is a
    backdrop-keyed dict ({"Copper": "5.0"}) -- it has no per-listing data
    and self-exclusion cannot be reconstructed from it. Confirmed and
    accepted with the user: leave NULL, count why.
    """
    conn = db.connect(":memory:")
    listing = _listing("ext-1")
    db.upsert_listing_with_floor(conn, listing, _snapshot_with_raw(listing, {"Copper": "5.0", "Onyx": "22.49"}))

    stats = run_backfill(conn)

    assert stats["processed"] == 1
    assert stats["recomputed"] == 0
    assert stats["left_null_backdrop_keyed_not_per_listing"] == 1

    row = conn.execute(
        "SELECT pair_floor_excl_self_nano FROM floor_snapshots WHERE listing_external_id='ext-1'"
    ).fetchone()
    assert row[0] is None


def test_empty_raw_model_block_is_left_null_with_separate_reason():
    conn = db.connect(":memory:")
    listing = _listing("ext-2")
    db.upsert_listing_with_floor(conn, listing, _snapshot_with_raw(listing, {}))

    stats = run_backfill(conn)

    assert stats["processed"] == 1
    assert stats["recomputed"] == 0
    assert stats["left_null_empty_raw_model_block"] == 1


def test_hypothetical_per_listing_shape_is_recomputed_correctly():
    """Defensive/future-proofing path: IF raw_model_block ever held a
    list of per-listing entries (matching pair_floor.py's actual
    search_pair_floor response shape), backfill.py must reconstruct
    exactly what pair_floor.py's exclude_external_id logic would have
    produced live.
    """
    conn = db.connect(":memory:")
    listing = _listing("cheap")
    per_listing_raw = [
        {"id": "cheap", "status": "listed", "price": "150.0"},  # the excluded listing itself
        {"id": "mid", "status": "listed", "price": "195.0"},
        {"id": "high", "status": "listed", "price": "220.0"},
        {"id": "gone", "status": "unlisted", "price": None},
    ]
    db.upsert_listing_with_floor(conn, listing, _snapshot_with_raw(listing, per_listing_raw))

    stats = run_backfill(conn)

    assert stats["recomputed"] == 1
    row = conn.execute(
        "SELECT pair_floor_excl_self_nano, pair_listed_count_excl_self, pair_self_was_floor "
        "FROM floor_snapshots WHERE listing_external_id='cheap'"
    ).fetchone()
    assert row[0] == int(Decimal("195.0") * config.NANO)
    assert row[1] == 2
    assert row[2] == 1  # self_was_floor -- "cheap" (150.0) was the pre-exclusion minimum


def test_hypothetical_shape_where_excluded_listing_is_the_only_one_stays_null():
    conn = db.connect(":memory:")
    listing = _listing("solo")
    db.upsert_listing_with_floor(
        conn, listing, _snapshot_with_raw(listing, [{"id": "solo", "status": "listed", "price": "195.0"}])
    )

    stats = run_backfill(conn)

    assert stats["recomputed"] == 0
    row = conn.execute(
        "SELECT pair_floor_excl_self_nano FROM floor_snapshots WHERE listing_external_id='solo'"
    ).fetchone()
    assert row[0] is None


def test_backfill_is_idempotent():
    conn = db.connect(":memory:")
    listing = _listing("ext-3")
    db.upsert_listing_with_floor(conn, listing, _snapshot_with_raw(listing, {"Copper": "5.0"}))

    stats1 = run_backfill(conn)
    stats2 = run_backfill(conn)
    assert stats1 == stats2
