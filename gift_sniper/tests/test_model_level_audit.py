import json
from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.model_level_audit import (
    _backdrop_spread_for_model,
    _part1_direct_comparison,
    _spread_bucket,
    generate_audit,
)
from gift_sniper.models import FloorSnapshot, Listing


def _listing(ext_id, price_nano, model_name="M", collection_name="Coll") -> Listing:
    now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    return Listing(
        marketplace="portals",
        external_id=ext_id,
        tg_id=f"{ext_id}-tg",
        collection_id="col-a",
        collection_name=collection_name,
        gift_number=1,
        price_nano=price_nano,
        currency="TON",
        collection_floor_nano=None,
        model_name=model_name,
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


def _snapshot(
    listing,
    pair_floor_excl_self_nano=None,
    pair_floor_status="no_data",
    pair_listed_count_excl_self=0,
    model_floor_excl_self_nano=None,
    model_floor_status="no_data",
    model_listed_count_excl_self=0,
    raw_model_block=None,
    floor_fetched_at=None,
) -> FloorSnapshot:
    return FloorSnapshot(
        listing_external_id=listing.external_id,
        model_name=listing.model_name,
        backdrop_name=listing.backdrop_name,
        api_combo_floor_nano=None,
        model_min_floor_nano=None,
        floor_fetched_at=floor_fetched_at or listing.first_seen_at,
        floor_age_sec=0,
        raw_model_block=raw_model_block or {},
        pair_floor_nano=pair_floor_excl_self_nano,
        pair_listed_count=pair_listed_count_excl_self,
        pair_floor_status=pair_floor_status,
        pair_floor_excl_self_nano=pair_floor_excl_self_nano,
        pair_listed_count_excl_self=pair_listed_count_excl_self,
        model_floor_excl_self_nano=model_floor_excl_self_nano,
        model_listed_count_excl_self=model_listed_count_excl_self,
        model_floor_status=model_floor_status,
    )


def test_ratio_2_0_falls_into_over_1_5_bucket():
    """Test item 1: pair_floor=50, model_floor=100 -> ratio 2.0, counted
    in the ratio > 1.5 bucket.
    """
    conn = db.connect(":memory:")
    listing = _listing("audit-1", int(Decimal("10") * config.NANO))
    db.upsert_listing_with_floor(
        conn,
        listing,
        _snapshot(
            listing,
            pair_floor_excl_self_nano=int(Decimal("50") * config.NANO),
            pair_floor_status="ok",
            pair_listed_count_excl_self=5,
            model_floor_excl_self_nano=int(Decimal("100") * config.NANO),
            model_floor_status="ok",
            model_listed_count_excl_self=5,
        ),
    )

    text, median = _part1_direct_comparison(conn)
    assert "rows with BOTH pair and model floor filled: 1" in text
    assert median == 2.0
    assert "ratio > 1.5 (model floor overstates by 50%+): 1 (100.0%)" in text


def _record_drop(conn, ext_id, old, new, delta_pct, observed_at, floor_at_drop=None, floor_level_at_drop=None, floor_listed_count=0):
    db.record_price_change(
        conn, "portals", ext_id,
        old_price_nano=int(Decimal(old) * config.NANO),
        new_price_nano=int(Decimal(new) * config.NANO),
        delta_pct=Decimal(str(delta_pct)),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=(int(Decimal(str(floor_at_drop)) * config.NANO) if floor_at_drop else None),
        floor_listed_count_at_drop=floor_listed_count,
        floor_level_at_drop=floor_level_at_drop,
        is_anomaly=False,
    )


def test_model_signal_disappears_when_pair_discount_is_below_threshold():
    """Test item 2: model-level clean signal with 30% discount by model
    and 5% discount by pair -> classified as 'would disappear' (pair
    discount does not clear the > 10% threshold).
    """
    conn = db.connect(":memory:")
    observed_at = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    listing = _listing("disappear-1", int(Decimal("70") * config.NANO))
    db.upsert_listing_with_floor(
        conn,
        listing,
        _snapshot(
            listing,
            pair_floor_excl_self_nano=int(Decimal("73.68") * config.NANO),  # (1-70/73.68) ~= 5%
            pair_floor_status="ok",
            pair_listed_count_excl_self=5,
            floor_fetched_at=observed_at,
        ),
    )
    # 30% discount vs model floor of 100.
    _record_drop(
        conn, "disappear-1", "80", "70", "-12.5", observed_at,
        floor_at_drop=100, floor_level_at_drop="model", floor_listed_count=5,
    )

    report = generate_audit(conn)
    assert "would DISAPPEAR (discount vs. pair floor <= 10%): 1" in report
    assert "would REMAIN a signal (discount vs. pair floor still > 10%): 0" in report


def test_model_signal_remains_when_pair_discount_clears_threshold():
    """Test item 3: 30% discount by model, 25% by pair -> 'would remain'."""
    conn = db.connect(":memory:")
    observed_at = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    listing = _listing("remains-1", int(Decimal("70") * config.NANO))
    db.upsert_listing_with_floor(
        conn,
        listing,
        _snapshot(
            listing,
            pair_floor_excl_self_nano=int(Decimal("93.33") * config.NANO),  # (1-70/93.33) ~= 25%
            pair_floor_status="ok",
            pair_listed_count_excl_self=5,
            floor_fetched_at=observed_at,
        ),
    )
    _record_drop(
        conn, "remains-1", "80", "70", "-12.5", observed_at,
        floor_at_drop=100, floor_level_at_drop="model", floor_listed_count=5,
    )

    report = generate_audit(conn)
    assert "would REMAIN a signal (discount vs. pair floor still > 10%): 1" in report
    assert "would DISAPPEAR (discount vs. pair floor <= 10%): 0" in report


def test_backdrop_spread_max_over_min():
    """Test item 4: raw_model_block {A: 4.0, B: 20.0} -> max/min = 5.0,
    which lands in the 3-10 bucket.
    """
    spread = _backdrop_spread_for_model({"A": "4.0", "B": "20.0"})
    assert spread is not None
    max_over_min, _cv = spread
    assert max_over_min == Decimal("5.0")
    assert _spread_bucket(max_over_min) == "3-10"


def test_empty_raw_model_block_is_skipped_not_a_crash():
    """Test item 5: an empty raw_model_block is skipped, not fatal."""
    assert _backdrop_spread_for_model({}) is None

    conn = db.connect(":memory:")
    listing = _listing("empty-block-1", int(Decimal("10") * config.NANO))
    db.upsert_listing_with_floor(
        conn,
        listing,
        _snapshot(listing, raw_model_block={}),
    )
    # Must not raise.
    report = generate_audit(conn)
    assert "=== Part 3" in report


def test_audit_runs_cleanly_on_empty_db():
    conn = db.connect(":memory:")
    report = generate_audit(conn)
    assert "=== Part 1" in report
    assert "=== Part 2" in report
    assert "=== Part 3" in report
    assert "=== Part 4" in report
