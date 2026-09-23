from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.models import Listing
from gift_sniper.tonnel_bundle_cleanup import main


def _tonnel_listing(external_id: str, price: str = "10.0") -> Listing:
    return Listing(
        marketplace="tonnel", external_id=external_id, tg_id=None, collection_id=None,
        collection_name="Coll", gift_number=1, price_nano=int(Decimal(price) * config.NANO),
        currency="TON", collection_floor_nano=None, model_name="M", symbol_name=None,
        backdrop_name="B", model_rarity_raw=None, symbol_rarity_raw=None, backdrop_rarity_raw=None,
        image_url=None, animation_url=None, listed_at=None, unlocks_at=None,
        status="forsale", first_seen_at=datetime.now(timezone.utc), raw={},
    )


def test_delete_tonnel_bundle_listings_removes_negative_gift_id_rows():
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    db.insert_listing(conn, _tonnel_listing("-555"))
    db.insert_listing(conn, _tonnel_listing("777"))
    db.touch_listing_lifecycle(conn, "tonnel", "-555", None, "M", "B", 10_000_000_000, now)
    db.touch_listing_lifecycle(conn, "tonnel", "777", None, "M", "B", 10_000_000_000, now)
    db.record_price_change(
        conn, "tonnel", "-555", old_price_nano=10_000_000_000, new_price_nano=9_000_000_000,
        delta_pct=Decimal("-10"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=now,
    )

    deleted = db.delete_tonnel_bundle_listings(conn)

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE external_id = '-555'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE external_id = '777'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE listing_external_id = '-555'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM listing_lifecycle WHERE listing_external_id = '-555'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM listing_lifecycle WHERE listing_external_id = '777'"
    ).fetchone()[0] == 1


def test_delete_tonnel_bundle_listings_is_idempotent():
    conn = db.connect(":memory:")
    db.insert_listing(conn, _tonnel_listing("-555"))

    first = db.delete_tonnel_bundle_listings(conn)
    second = db.delete_tonnel_bundle_listings(conn)

    assert first == 1
    assert second == 0


def test_delete_tonnel_bundle_listings_never_touches_portals_rows():
    """A negative-looking Portals external_id (UUIDs never look like
    this, but defensively) must not be affected -- the DELETE is scoped
    to marketplace='tonnel' explicitly.
    """
    conn = db.connect(":memory:")
    portals_listing = Listing(
        marketplace="portals", external_id="-not-actually-a-bundle", tg_id="X-1", collection_id="col-a",
        collection_name="Coll", gift_number=1, price_nano=1_000_000_000, currency="TON",
        collection_floor_nano=None, model_name="M", symbol_name=None, backdrop_name="B",
        model_rarity_raw=None, symbol_rarity_raw=None, backdrop_rarity_raw=None,
        image_url=None, animation_url=None, listed_at=None, unlocks_at=None,
        status="listed", first_seen_at=datetime.now(timezone.utc), raw={},
    )
    from gift_sniper.models import FloorSnapshot
    snapshot = FloorSnapshot(
        listing_external_id="-not-actually-a-bundle", model_name="M", backdrop_name="B",
        api_combo_floor_nano=None, model_min_floor_nano=None, floor_fetched_at=datetime.now(timezone.utc),
        floor_age_sec=0, raw_model_block={}, pair_floor_status="pending",
    )
    db.upsert_listing_with_floor(conn, portals_listing, snapshot)

    deleted = db.delete_tonnel_bundle_listings(conn)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE marketplace='portals'").fetchone()[0] == 1


def test_cli_runs_end_to_end(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    db.insert_listing(conn, _tonnel_listing("-555"))
    conn.close()

    rc = main(["--db", db_path])
    assert rc == 0

    conn2 = db.connect(db_path)
    assert conn2.execute("SELECT COUNT(*) FROM listings WHERE external_id = '-555'").fetchone()[0] == 0
