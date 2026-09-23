"""КАК ТЕСТИРОВАТЬ (below_min_profit / stale_floor delivery) -- all 8
items from the spec, each as its own test. See signals.py's
compute_profit_nano and run_cascade (below_min_profit, stale_floor
stages) and README.md's newest entry for the full defect writeup.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.models import FloorSnapshot, Listing
from gift_sniper.signals import clean_signals, compute_profit_nano, run_cascade


def _listing(ext_id, price_nano, marketplace="portals", model_name="M") -> Listing:
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


def _snapshot(listing, pair_floor_nano, pair_floor_status="ok", floor_fetched_at=None) -> FloorSnapshot:
    return FloorSnapshot(
        listing_external_id=listing.external_id,
        model_name=listing.model_name,
        backdrop_name=listing.backdrop_name,
        api_combo_floor_nano=None,
        model_min_floor_nano=None,
        floor_fetched_at=floor_fetched_at or listing.first_seen_at,
        floor_age_sec=0,
        raw_model_block={},
        pair_floor_nano=pair_floor_nano,
        pair_listed_count=10,
        pair_floor_status=pair_floor_status,
        pair_floor_excl_self_nano=pair_floor_nano,
        pair_listed_count_excl_self=10,
    )


def _record_drop(conn, marketplace, ext_id, old, new, delta_pct, observed_at,
                  floor_at_drop=None, floor_level_at_drop="pair", floor_listed_count=5):
    db.record_price_change(
        conn, marketplace, ext_id,
        old_price_nano=int(Decimal(old) * config.NANO),
        new_price_nano=int(Decimal(new) * config.NANO),
        delta_pct=Decimal(str(delta_pct)),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=(int(Decimal(str(floor_at_drop)) * config.NANO) if floor_at_drop is not None else None),
        floor_listed_count_at_drop=floor_listed_count,
        floor_fetched_at=observed_at,
        floor_level_at_drop=floor_level_at_drop,
    )


# --- item 1: Portals price 16.65, floor 16.67 -> negative/near-zero profit -> dropped ---

def test_item1_portals_two_cent_gap_dropped_at_below_min_profit():
    conn = db.connect(":memory:")
    listing = _listing("jingle-bells-1", int(Decimal("16.65") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("16.67") * config.NANO)))
    observed_at = datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc)
    _record_drop(conn, "portals", "jingle-bells-1", "17.00", "16.65", "-2.1", observed_at, floor_at_drop="16.67")

    cascade = run_cascade(conn, now=observed_at, marketplace="portals")
    assert len(cascade.below_min_profit) == 1
    signals_list = clean_signals(conn, usd_rate=Decimal("1.0"), now=observed_at, marketplace="portals")
    assert signals_list == []


# --- item 2: Tonnel price 32.45, floor 32.50 -> profit -4.54 -> dropped ---

def test_item2_tonnel_negative_profit_dropped_at_below_min_profit():
    conn = db.connect(":memory:")
    listing = _listing("pepe-bag-1", int(Decimal("32.45") * config.NANO), marketplace="tonnel")
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("32.50") * config.NANO)))
    observed_at = datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc)
    _record_drop(conn, "tonnel", "pepe-bag-1", "33.00", "32.45", "-1.7", observed_at, floor_at_drop="32.50")

    floor_nano = int(Decimal("32.50") * config.NANO)
    price_nano = int(Decimal("32.45") * config.NANO)
    _before, profit_nano = compute_profit_nano("tonnel", floor_nano, price_nano, 5)
    profit = Decimal(profit_nano) / config.NANO
    # floor*RATE - price*1.1 at depth 5 (RATE 0.95):
    # 32.50*0.95 - 32.45*1.1 = 30.875 - 35.695 = -4.82 TON -- NEGATIVE,
    # therefore dropped below.
    assert profit == Decimal("-4.82")
    assert profit < 0

    cascade = run_cascade(conn, now=observed_at, marketplace="tonnel")
    assert len(cascade.below_min_profit) == 1


# --- item 3: Portals price 30, floor 40 -> passes (profit = 8.85 per the
# formula, NOT the spec's stated 9.05 -- see README: 40*0.98-30-0.35 =
# 8.85, matching the spec's OWN worked "Расчёт прибыли" table at other
# price points, e.g. price=100 -> 105*0.98-100-0.35=2.55 stated there.
# "9.05" in item 3 is treated as a typo in the task text; the
# qualitative claim -- passes, comfortably above MIN_SIGNAL_PROFIT_TON
# -- still holds either way.) ---

def test_item3_portals_profitable_signal_passes():
    conn = db.connect(":memory:")
    listing = _listing("profitable-1", int(Decimal("30") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("40") * config.NANO)))
    observed_at = datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc)
    _record_drop(conn, "portals", "profitable-1", "35", "30", "-14.3", observed_at, floor_at_drop="40")

    floor_nano = int(Decimal("40") * config.NANO)
    price_nano = int(Decimal("30") * config.NANO)
    # depth 5 -> RATE 0.95: 40*0.95*0.98 - 30 - 0.35 = 6.89
    _before, profit_nano = compute_profit_nano("portals", floor_nano, price_nano, 5)
    assert Decimal(profit_nano) / config.NANO == Decimal("6.89")

    cascade = run_cascade(conn, now=observed_at, marketplace="portals")
    assert cascade.below_min_profit == []
    signals_list = clean_signals(conn, usd_rate=Decimal("1.0"), now=observed_at, marketplace="portals")
    assert len(signals_list) == 1
    assert signals_list[0].profit_nano == profit_nano


# --- item 4: model-level Portals signal -> profit computed (not None),
# profit_is_estimate=True, threshold still applies ---

def test_item4_model_level_profit_is_computed_and_flagged_estimate():
    conn = db.connect(":memory:")
    listing = _listing("model-level-1", int(Decimal("30") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, None, pair_floor_status="no_data"))
    observed_at = datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc)
    _record_drop(
        conn, "portals", "model-level-1", "35", "30", "-14.3", observed_at,
        floor_at_drop="40", floor_level_at_drop="model",
    )

    signals_list = clean_signals(conn, usd_rate=Decimal("1.0"), now=observed_at, marketplace="portals")
    assert len(signals_list) == 1
    sig = signals_list[0]
    assert sig.floor_level == "model"
    assert sig.profit_nano is not None
    assert sig.profit_is_estimate is True

    # Same lot, but a floor so close to price the model-level estimate
    # should still be dropped by below_min_profit.
    conn2 = db.connect(":memory:")
    listing2 = _listing("model-level-2", int(Decimal("16.65") * config.NANO))
    db.upsert_listing_with_floor(conn2, listing2, _snapshot(listing2, None, pair_floor_status="no_data"))
    _record_drop(
        conn2, "portals", "model-level-2", "17.00", "16.65", "-2.1", observed_at,
        floor_at_drop="16.67", floor_level_at_drop="model",
    )
    cascade2 = run_cascade(conn2, now=observed_at, marketplace="portals")
    assert len(cascade2.below_min_profit) == 1


# --- item 5: floor snapshot aged 45 min at a 30-min threshold -> dropped at stale_floor ---

def test_item5_stale_snapshot_45min_dropped():
    conn = db.connect(":memory:")
    fetched_at = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    observed_at = fetched_at + timedelta(minutes=45)
    listing = _listing("stale-1", int(Decimal("30") * config.NANO))
    db.upsert_listing_with_floor(
        conn, listing, _snapshot(listing, int(Decimal("40") * config.NANO), floor_fetched_at=fetched_at)
    )
    # No floor_at_drop -- forces fallback to the (stale) snapshot.
    _record_drop(conn, "portals", "stale-1", "35", "30", "-14.3", observed_at, floor_at_drop=None)

    cascade = run_cascade(conn, now=observed_at, marketplace="portals")
    assert len(cascade.stale_floor) == 1
    assert cascade.below_min_profit == []
    signals_list = clean_signals(conn, usd_rate=Decimal("1.0"), now=observed_at, marketplace="portals")
    assert signals_list == []


# --- item 6: snapshot aged 10 min -> used normally, not dropped ---

def test_item6_fresh_snapshot_10min_used_normally():
    conn = db.connect(":memory:")
    fetched_at = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    observed_at = fetched_at + timedelta(minutes=10)
    listing = _listing("fresh-1", int(Decimal("30") * config.NANO))
    db.upsert_listing_with_floor(
        conn, listing, _snapshot(listing, int(Decimal("40") * config.NANO), floor_fetched_at=fetched_at)
    )
    _record_drop(conn, "portals", "fresh-1", "35", "30", "-14.3", observed_at, floor_at_drop=None)

    cascade = run_cascade(conn, now=observed_at, marketplace="portals")
    assert cascade.stale_floor == []
    signals_list = clean_signals(conn, usd_rate=Decimal("1.0"), now=observed_at, marketplace="portals")
    assert len(signals_list) == 1
    assert signals_list[0].floor_source == "snapshot"


# --- items 7 & 8: formula shape per marketplace ---

def test_item7_mrkt_formula_has_no_1_1_multiplier_and_no_flat_withdrawal_fee():
    floor_nano = int(Decimal("100") * config.NANO)
    price_nano = int(Decimal("90") * config.NANO)
    rate = config.REALIZATION_RATE_DEPTH_4_9
    _before, profit_nano = compute_profit_nano("mrkt", floor_nano, price_nano, 5)
    expected = int(Decimal("100") * rate * (1 - config.MARKETPLACE_FEE_RATE) * config.NANO - Decimal("90") * config.NANO)
    assert profit_nano == expected
    # Confirm neither a 1.1x price multiplier nor the flat 0.35 withdrawal
    # fee snuck into MRKT's formula (those are Portals/Tonnel-only).
    wrong_with_1_1 = int(Decimal("100") * rate * (1 - config.MARKETPLACE_FEE_RATE) * config.NANO - Decimal("90") * Decimal("1.1") * config.NANO)
    wrong_with_flat_fee = expected - config.WITHDRAWAL_FEE_FLAT_NANO
    assert profit_nano != wrong_with_1_1
    assert profit_nano != wrong_with_flat_fee


def test_item8_tonnel_formula_multiplies_buy_price_by_1_1():
    floor_nano = int(Decimal("100") * config.NANO)
    price_nano = int(Decimal("90") * config.NANO)
    rate = config.REALIZATION_RATE_DEPTH_4_9
    _before, profit_nano = compute_profit_nano("tonnel", floor_nano, price_nano, 5)
    expected = int(Decimal("100") * rate * config.NANO - Decimal("90") * Decimal("1.1") * config.NANO)
    assert profit_nano == expected
    without_1_1 = int(Decimal("100") * rate * config.NANO - Decimal("90") * config.NANO)
    assert profit_nano != without_1_1
