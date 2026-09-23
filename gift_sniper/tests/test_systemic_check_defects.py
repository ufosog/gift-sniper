"""КАК ТЕСТИРОВАТЬ (systemic-check delivery) items 1-3: no_floor_at_send
(ДЕФЕКТ 1) and TONNEL_NOTIFY_LEVELS/NOTIFY_LEVELS end-to-end (ДЕФЕКТ 2).
Items 4-10 (cooldown, cross-check) live in test_notifier.py /
test_cross_check.py, closer to their existing coverage. See README.md's
newest entry for the full defect writeup.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.models import FloorSnapshot, Listing
from gift_sniper.signals import clean_signals, run_cascade


def _listing(ext_id, price_nano, marketplace="portals") -> Listing:
    now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    return Listing(
        marketplace=marketplace,
        external_id=ext_id,
        tg_id=f"{ext_id}-tg",
        collection_id="col-a",
        collection_name="CollA",
        gift_number=1,
        price_nano=price_nano,
        currency="TON",
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


def _snapshot(listing, pair_floor_nano=None, pair_floor_status="no_data") -> FloorSnapshot:
    return FloorSnapshot(
        listing_external_id=listing.external_id,
        model_name=listing.model_name,
        backdrop_name=listing.backdrop_name,
        api_combo_floor_nano=None,
        model_min_floor_nano=None,
        floor_fetched_at=listing.first_seen_at,
        floor_age_sec=0,
        raw_model_block={},
        pair_floor_nano=pair_floor_nano,
        pair_listed_count=10 if pair_floor_nano else 0,
        pair_floor_status=pair_floor_status,
        pair_floor_excl_self_nano=pair_floor_nano,
        pair_listed_count_excl_self=10 if pair_floor_nano else 0,
    )


# --- ДЕФЕКТ 1, item 1: floor_at_drop_nano=NULL, no snapshot either -> not sent ---

def test_item1_null_floor_at_drop_no_snapshot_not_sent():
    conn = db.connect(":memory:")
    listing = _listing("no-floor-1", int(Decimal("30.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing))  # no pair floor at all
    observed_at = listing.first_seen_at + timedelta(minutes=1)
    db.record_price_change(
        conn, "portals", "no-floor-1",
        old_price_nano=int(Decimal("35.0") * config.NANO), new_price_nano=int(Decimal("30.0") * config.NANO),
        delta_pct=Decimal("-14.3"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
        # floor_at_drop_nano deliberately omitted -> None
    )

    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=observed_at)
    assert sigs == []
    cascade = run_cascade(conn, now=observed_at, marketplace="portals")
    assert cascade.clean == []


# --- ДЕФЕКТ 1, item 2: floor_listed_count_at_drop=0 -> not sent, caught by ---
# --- no_floor_at_send even when the configurable thin_book threshold is ---
# --- misconfigured to let it through (the whole point: "ни при каких ---
# --- настройках"). ---

def test_item2_zero_listed_count_at_drop_not_sent_even_with_threshold_at_zero(monkeypatch):
    monkeypatch.setattr(config, "FLOOR_MIN_LISTED_COUNT", 2)  # config.py's own floor (>= 2 enforced)
    conn = db.connect(":memory:")
    listing = _listing("zero-count-1", int(Decimal("30.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing))
    observed_at = listing.first_seen_at + timedelta(minutes=1)
    db.record_price_change(
        conn, "portals", "zero-count-1",
        old_price_nano=int(Decimal("35.0") * config.NANO), new_price_nano=int(Decimal("30.0") * config.NANO),
        delta_pct=Decimal("-14.3"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO), floor_listed_count_at_drop=0,
        floor_level_at_drop="pair",
    )

    cascade = run_cascade(conn, now=observed_at, marketplace="portals")
    assert cascade.clean == []
    assert len(cascade.no_floor_at_send) + len(cascade.thin_book) == 1

    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=observed_at)
    assert sigs == []


def test_no_floor_at_send_is_a_hardcoded_backstop_ignoring_tonnel_threshold(monkeypatch):
    """The unconfigurable case per spec ("ни при каких настройках"):
    TONNEL_FLOOR_MIN_LISTED_COUNT (no >= 2 guard in config.py, unlike
    the Portals one) misconfigured to 0 must NOT let a listed_count=0
    Tonnel signal through -- no_floor_at_send catches it regardless.
    """
    monkeypatch.setattr(config, "TONNEL_FLOOR_MIN_LISTED_COUNT", 0)
    conn = db.connect(":memory:")
    listing = _listing("tonnel-zero-1", int(Decimal("30.0") * config.NANO), marketplace="tonnel")
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing))
    observed_at = listing.first_seen_at + timedelta(minutes=1)
    db.record_price_change(
        conn, "tonnel", "tonnel-zero-1",
        old_price_nano=int(Decimal("35.0") * config.NANO), new_price_nano=int(Decimal("30.0") * config.NANO),
        delta_pct=Decimal("-14.3"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO), floor_listed_count_at_drop=0,
        floor_level_at_drop="model",
    )

    cascade = run_cascade(conn, now=observed_at, marketplace="tonnel")
    assert cascade.thin_book == []  # the misconfigured (0) threshold lets it through thin_book...
    assert len(cascade.no_floor_at_send) == 1  # ...but the hardcoded backstop still catches it
    assert cascade.clean == []


# --- ДЕФЕКТ 2, item 3: NOTIFY_LEVELS={'pair'}, level='model' -> not sent ---
# --- (covered end-to-end, real Poller, in test_notifier.py's ---
# --- test_notify_levels_end_to_end_blocks_model_level_portals_send). ---
# --- Cascade-level check here: the signal itself IS constructed (passes ---
# --- the cascade, unlike Дефект 1) -- filtering happens one layer up, in ---
# --- poller.py's _maybe_notify candidate list, exactly where it's supposed to. ---

def test_item3_model_level_signal_exists_in_clean_signals_gate_is_at_notify_layer():
    conn = db.connect(":memory:")
    listing = _listing("model-1", int(Decimal("30.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing))
    observed_at = listing.first_seen_at + timedelta(minutes=1)
    db.record_price_change(
        conn, "portals", "model-1",
        old_price_nano=int(Decimal("35.0") * config.NANO), new_price_nano=int(Decimal("30.0") * config.NANO),
        delta_pct=Decimal("-14.3"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO), floor_listed_count_at_drop=5,
        floor_level_at_drop="model",
    )

    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=observed_at)
    assert len(sigs) == 1
    assert sigs[0].floor_level == "model"
    # clean_signals() itself doesn't gate by NOTIFY_LEVELS -- that's
    # poller.py's job (see test_notifier.py's end-to-end regression test
    # with NOTIFY_LEVELS={"pair"}; the default is now "pair,model").
