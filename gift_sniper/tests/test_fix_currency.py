from datetime import datetime, timezone

from gift_sniper import config, db
from gift_sniper.fix_currency import main
from gift_sniper.models import Listing


def _listing(ext_id, currency) -> Listing:
    now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    return Listing(
        marketplace="portals",
        external_id=ext_id,
        tg_id=f"{ext_id}-tg",
        collection_id="col-a",
        collection_name="CollA",
        gift_number=1,
        price_nano=1000,
        currency=currency,
        collection_floor_nano=None,
        model_name="M",
        symbol_name=None,
        backdrop_name="B",
        model_rarity_raw=None,
        symbol_rarity_raw=None,
        backdrop_rarity_raw=None,
        image_url=None,
        animation_url=None,
        listed_at=now,
        unlocks_at=None,
        status="listed",
        first_seen_at=now,
        raw={},
    )


def _snapshot(listing):
    from gift_sniper.models import FloorSnapshot
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


def test_fix_listings_currency_updates_only_differing_rows():
    """КАК ТЕСТИРОВАТЬ item 4: only differing rows are updated."""
    conn = db.connect(":memory:")
    ton_listing = _listing("ton-1", "TON")
    gram_listing = _listing("gram-1", "GRAM")
    db.upsert_listing_with_floor(conn, ton_listing, _snapshot(ton_listing))
    db.upsert_listing_with_floor(conn, gram_listing, _snapshot(gram_listing))

    updated = db.fix_listings_currency(conn, "GRAM")
    assert updated == 1

    currencies = {
        r[0]: r[1] for r in conn.execute("SELECT external_id, currency FROM listings")
    }
    assert currencies == {"ton-1": "GRAM", "gram-1": "GRAM"}


def test_fix_listings_currency_is_idempotent():
    """КАК ТЕСТИРОВАТЬ item 4: a repeated run updates 0 rows."""
    conn = db.connect(":memory:")
    ton_listing = _listing("ton-2", "TON")
    db.upsert_listing_with_floor(conn, ton_listing, _snapshot(ton_listing))

    first = db.fix_listings_currency(conn, "GRAM")
    second = db.fix_listings_currency(conn, "GRAM")
    assert first == 1
    assert second == 0


def test_fix_currency_cli_runs_end_to_end(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    ton_listing = _listing("cli-ton-1", "TON")
    db.upsert_listing_with_floor(conn, ton_listing, _snapshot(ton_listing))
    conn.close()

    exit_code = main(["--db", db_path])
    assert exit_code == 0

    conn2 = db.connect(db_path)
    row = conn2.execute("SELECT currency FROM listings WHERE external_id = 'cli-ton-1'").fetchone()
    assert row[0] == config.CURRENCY_DEFAULT
