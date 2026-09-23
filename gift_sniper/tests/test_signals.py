import dataclasses
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.report import generate_report
from gift_sniper.signals import clean_signals
from .test_price_drops_report import _add_drop, _listing, _snapshot


def _seed_mixed_signals(conn):
    """A DB with a mix of clean, noise, anomaly, thin-book, implausible,
    and bulk-update rows -- exercising every stage of the cascade at
    once, so a report.py/clean_signals() divergence at any single stage
    would show up as a set mismatch below.
    """
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    # Floor 50 (not 44): at depth 5 the realization rate (0.95) needs
    # floor*0.95*0.98 - 40 - 0.35 >= MIN_SIGNAL_PROFIT_TON. Own backdrop,
    # so the other fixtures' wildly different floors in pair M/B don't make
    # clean-1's pair look unstable (unstable_floor stage).
    clean1 = _listing("clean-1", int(Decimal("40.0") * config.NANO))
    clean1.backdrop_name = "Clean"
    db.upsert_listing_with_floor(conn, clean1, _snapshot(clean1, int(Decimal("50.0") * config.NANO)))
    _add_drop(conn, "clean-1", 0, "45.0", "40.0", "-11.1", is_noise=False, floor_at_drop=50.0)

    noise1 = _listing("noise-1", int(Decimal("24.95") * config.NANO))
    db.upsert_listing_with_floor(conn, noise1, _snapshot(noise1, int(Decimal("30.0") * config.NANO)))
    _add_drop(conn, "noise-1", 1, "24.99", "24.95", "-0.16", is_noise=True)

    anomaly1 = _listing("anomaly-1", int(Decimal("99") * config.NANO))
    db.upsert_listing_with_floor(conn, anomaly1, _snapshot(anomaly1, int(Decimal("999") * config.NANO)))
    db.record_price_change(
        conn, "portals", "anomaly-1",
        old_price_nano=int(Decimal("999") * config.NANO),
        new_price_nano=int(Decimal("99") * config.NANO),
        delta_pct=Decimal("-90.09"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=now,
        is_anomaly=True,
    )

    thin1 = _listing("thin-1", int(Decimal("23.91") * config.NANO))
    db.upsert_listing_with_floor(conn, thin1, _snapshot(thin1, int(Decimal("25.0") * config.NANO)))
    _add_drop(conn, "thin-1", 2, "24.60", "23.91", "-2.8", is_noise=False, floor_at_drop=25.0)
    # thin-1's floor_listed_count_at_drop defaults to 3 in _add_drop (see
    # test_price_drops_report.py) -- override to 1 directly.
    conn.execute(
        "UPDATE price_history SET floor_listed_count_at_drop = 1 WHERE listing_external_id = 'thin-1'"
    )

    implausible1 = _listing("implausible-1", int(Decimal("23.91") * config.NANO))
    db.upsert_listing_with_floor(conn, implausible1, _snapshot(implausible1, int(Decimal("230.0") * config.NANO)))
    _add_drop(conn, "implausible-1", 3, "24.60", "23.91", "-2.9", is_noise=False, floor_at_drop=230.0)

    for i, ext_id in enumerate(["bulk-1", "bulk-2", "bulk-3"]):
        listing = _listing(ext_id, int(Decimal("23.91") * config.NANO))
        db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("60.0") * config.NANO)))
        db.record_price_change(
            conn, "portals", ext_id,
            old_price_nano=int(Decimal("24.60") * config.NANO),
            new_price_nano=int(Decimal("23.91") * config.NANO),
            delta_pct=Decimal("-2.8"),
            is_noise=False,
            old_listed_at=None, new_listed_at=None,
            observed_at=now,
            floor_at_drop_nano=int(Decimal("60.0") * config.NANO),
            floor_listed_count_at_drop=5,
        )

    return now


def test_clean_signals_matches_report_py_on_the_same_db():
    """КАК ТЕСТИРОВАТЬ item 1: clean_signals() must return the same set
    of listings as report.py's "CLEAN signals" count on the same DB.
    """
    conn = db.connect(":memory:")
    now = _seed_mixed_signals(conn)

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), drops_now=now)
    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=now)

    assert f"CLEAN signals: {len(sigs)}" in report_text
    assert {s.listing_external_id for s in sigs} == {"clean-1"}


def test_clean_signals_since_filters_by_observed_at():
    conn = db.connect(":memory:")
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    early = _listing("early-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, early, _snapshot(early, int(Decimal("50.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", "early-1",
        old_price_nano=int(Decimal("45.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-11.1"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("50.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    late = _listing("late-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, late, _snapshot(late, int(Decimal("50.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", "late-1",
        old_price_nano=int(Decimal("45.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-11.1"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 11, 30, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("50.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    all_sigs = clean_signals(conn, now=now)
    assert {s.listing_external_id for s in all_sigs} == {"early-1", "late-1"}

    since_sigs = clean_signals(conn, since=datetime(2026, 9, 6, 11, 0, tzinfo=timezone.utc), now=now)
    assert {s.listing_external_id for s in since_sigs} == {"late-1"}


def test_clean_signals_identical_across_two_independent_connections():
    """БАГ 2, КАК ТЕСТИРОВАТЬ item 5: clean_signals() must give the
    IDENTICAL result whether called from "the poller's" connection or a
    completely separate process/connection against the same on-disk DB --
    it's a pure function of DB state (plus the `since`/`now`/`usd_rate`
    arguments), never of which caller invoked it. Uses a real file (not
    :memory:) so two independent db.connect() calls actually share state,
    the way the poller process and an external diagnostic script would.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        seed_conn = db.connect(path)
        now = _seed_mixed_signals(seed_conn)
        seed_conn.close()

        poller_conn = db.connect(path)
        other_process_conn = db.connect(path)

        poller_sigs = clean_signals(poller_conn, usd_rate=Decimal("3.15"), now=now)
        other_sigs = clean_signals(other_process_conn, usd_rate=Decimal("3.15"), now=now)

        assert [dataclasses.asdict(s) for s in poller_sigs] == [dataclasses.asdict(s) for s in other_sigs]
        assert len(poller_sigs) > 0  # sanity: the comparison isn't vacuous
    finally:
        try:
            poller_conn.close()
            other_process_conn.close()
        except NameError:
            pass
        os.remove(path)


def _mark_gone(conn, ext_id, collection_id, model_name, backdrop_name, first_seen, disappeared):
    db.touch_listing_lifecycle(conn, "portals", ext_id, collection_id, model_name, backdrop_name, 1000, first_seen)
    conn.execute(
        "UPDATE listing_lifecycle SET disappeared_at = ? WHERE listing_external_id = ?",
        (disappeared.isoformat(), ext_id),
    )


def test_clean_signal_carries_liquidity_when_enough_samples():
    """КАК ТЕСТИРОВАТЬ item 4: a signal for a (collection, model,
    backdrop) pair with >= MIN_LIQUIDITY_SAMPLE gone listings gets real
    pair_gone_count / pair_median_time_to_gone_hours values.
    """
    conn = db.connect(":memory:")
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    listing = _listing("liquid-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("50.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", "liquid-1",
        old_price_nano=int(Decimal("45.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-11.1"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=now,
        floor_at_drop_nano=int(Decimal("50.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    # _listing() fixture always uses collection_id="col-a", model_name="M",
    # backdrop_name="B" -- 3 gone listings for that exact pair.
    for i in range(3):
        first_seen = now - timedelta(hours=20 + i)
        _mark_gone(conn, f"gone-{i}", "col-a", "M", "B", first_seen, first_seen + timedelta(hours=5))

    sigs = clean_signals(conn, now=now)
    assert len(sigs) == 1
    assert sigs[0].pair_gone_count == 3
    assert sigs[0].pair_median_time_to_gone_hours is not None


def test_clean_signal_liquidity_is_none_below_min_sample():
    """КАК ТЕСТИРОВАТЬ item 5: only 2 gone listings for the pair ->
    pair_gone_count/pair_median_time_to_gone_hours stay None (the
    "insufficient data" case, not a fabricated number).
    """
    conn = db.connect(":memory:")
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    listing = _listing("liquid-2", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("50.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", "liquid-2",
        old_price_nano=int(Decimal("45.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-11.1"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=now,
        floor_at_drop_nano=int(Decimal("50.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    for i in range(2):
        first_seen = now - timedelta(hours=20 + i)
        _mark_gone(conn, f"gone2-{i}", "col-a", "M", "B", first_seen, first_seen + timedelta(hours=5))

    sigs = clean_signals(conn, now=now)
    assert len(sigs) == 1
    assert sigs[0].pair_gone_count is None
    assert sigs[0].pair_median_time_to_gone_hours is None


def test_clean_signal_set_unchanged_by_minimal_notification_format_delivery():
    """КАК ТЕСТИРОВАТЬ item 4 (minimal-notification-format delivery): the
    cascade (thin-book, is_ladder, is_anomaly, is_bulk_update,
    is_implausible, floor no_data, ratio) is UNCHANGED by that delivery,
    which only touched notifier.py's display. Re-runs the full mixed
    fixture (every stage exercised) and pins the exact same surviving
    set as before -- if a future edit to signals.py ever narrows or
    widens this set, this test is the tripwire.
    """
    conn = db.connect(":memory:")
    now = _seed_mixed_signals(conn)

    sigs = clean_signals(conn, now=now)
    assert {s.listing_external_id for s in sigs} == {"clean-1"}
    # And every field the cascade computes is still populated (nothing
    # was removed from Signal itself -- only notifier.py stopped
    # displaying some of it).
    s = sigs[0]
    assert s.floor_level in ("pair", "model")
    assert s.ratio is not None
    assert s.discount is not None
    assert s.listed_count is not None


# --- Tonnel signals (this delivery) ----------------------------------------


def _seed_tonnel_signal(conn, ext_id="t-1", price="20.0", floor="30.0", old_price="40.0", listed_count=5):
    from gift_sniper.models import Listing

    now = datetime.now(timezone.utc)
    listing = Listing(
        marketplace="tonnel", external_id=ext_id, tg_id=f"IceCream-{ext_id}", collection_id=None,
        collection_name="Ice Cream", gift_number=1, price_nano=int(Decimal(price) * config.NANO),
        currency="TON", collection_floor_nano=None, model_name="Emperor", symbol_name=None, backdrop_name="Black",
        model_rarity_raw=None, symbol_rarity_raw=None, backdrop_rarity_raw=None,
        image_url=None, animation_url=None, listed_at=None, unlocks_at=None,
        status="forsale", first_seen_at=now, raw={},
    )
    db.insert_listing(conn, listing)
    db.record_price_change(
        conn, "tonnel", ext_id,
        old_price_nano=int(Decimal(old_price) * config.NANO), new_price_nano=int(Decimal(price) * config.NANO),
        delta_pct=Decimal("-50"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=now,
    )
    db.upsert_tonnel_model_floor_snapshot(conn, ext_id, "Emperor", "Black", int(Decimal(floor) * config.NANO), listed_count, "ok", now)
    return now


def test_clean_signals_marketplace_isolation():
    """КАК ТЕСТИРОВАТЬ item 3: clean_signals(marketplace='tonnel') never
    returns a Portals row and vice versa.
    """
    conn = db.connect(":memory:")
    now = _seed_mixed_signals(conn)  # portals fixture, various stages
    _seed_tonnel_signal(conn, "t-1")

    portals_sigs = clean_signals(conn, now=now, marketplace="portals")
    tonnel_sigs = clean_signals(conn, now=now, marketplace="tonnel")

    assert all(s.marketplace == "portals" for s in portals_sigs)
    assert all(s.marketplace == "tonnel" for s in tonnel_sigs)
    assert {s.listing_external_id for s in tonnel_sigs} == {"t-1"}
    assert "t-1" not in {s.listing_external_id for s in portals_sigs}


def test_tonnel_signal_uses_tonnel_profit_formula_not_portals():
    """КАК ТЕСТИРОВАТЬ item 4: profit = floor - price*1.1 (Tonnel's
    confirmed 10% BUYER fee, no seller-side deduction) -- NOT Portals'
    floor*(1-MARKETPLACE_FEE_RATE) - price - WITHDRAWAL_FEE_FLAT_NANO.
    """
    conn = db.connect(":memory:")
    now = _seed_tonnel_signal(conn, "t-1", price="20.0", floor="30.0")

    sigs = clean_signals(conn, now=now, marketplace="tonnel")
    assert len(sigs) == 1
    s = sigs[0]

    # listed_count 5 -> realization rate 0.95 applied to the floor.
    expected_profit_nano = int(
        Decimal("30.0") * config.REALIZATION_RATE_DEPTH_4_9 * config.NANO - Decimal("20.0") * Decimal("1.1") * config.NANO
    )
    assert s.profit_nano == expected_profit_nano
    # Confirm this is NOT the Portals formula (which would subtract
    # MARKETPLACE_FEE_RATE and WITHDRAWAL_FEE_FLAT_NANO instead).
    portals_style_profit = int(
        Decimal("30.0") * config.NANO * (1 - config.MARKETPLACE_FEE_RATE) - Decimal("20.0") * config.NANO
        - config.WITHDRAWAL_FEE_FLAT_NANO
    )
    assert s.profit_nano != portals_style_profit


def test_tonnel_signal_level_is_always_model():
    """Правка 3: Tonnel signals are always floor_level="model" -- "pair"
    is never used for Tonnel (there is no pair floor to use)."""
    conn = db.connect(":memory:")
    now = _seed_tonnel_signal(conn, "t-1", price="20.0", floor="30.0")
    sigs = clean_signals(conn, now=now, marketplace="tonnel")
    assert len(sigs) == 1
    assert sigs[0].floor_level == "model"


def test_tonnel_price_above_model_floor_forms_no_signal():
    """КАК ТЕСТИРОВАТЬ item 3: price above the model floor -> no signal."""
    conn = db.connect(":memory:")
    now = _seed_tonnel_signal(conn, "t-1", price="35.0", floor="30.0", old_price="40.0")
    sigs = clean_signals(conn, now=now, marketplace="tonnel")
    assert sigs == []


def test_tonnel_lonely_high_floor_excluded_by_max_ratio(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 4: floor 510 at price 110 (ratio 4.64) is
    excluded via TONNEL_FLOOR_MAX_RATIO_TO_PRICE (default 4.0) -- protects
    against a single overpriced lonely listing skewing the floor, the
    Durov's Glasses/Vampire Gaze case from spec.
    """
    conn = db.connect(":memory:")
    now = _seed_tonnel_signal(conn, "t-1", price="110.0", floor="510.0", old_price="600.0")
    sigs = clean_signals(conn, now=now, marketplace="tonnel")
    assert sigs == []


# --- price_above_own_floor (ДОПОЛНЕНИЕ: applies to ALL marketplaces/levels) --


def test_price_above_own_floor_rejected_on_portals_model_level():
    """КАК ТЕСТИРОВАТЬ item 1: price 210, floor 34 (Bling Binky/Regent,
    a real measured case) -> rejected at price_above_own_floor, never
    reaches clean.
    """
    from gift_sniper.signals import run_cascade

    conn = db.connect(":memory:")
    listing = _listing("bling-binky-1", int(Decimal("210.0") * config.NANO))
    snapshot = _snapshot(listing, int(Decimal("999.0") * config.NANO))
    snapshot.model_floor_excl_self_nano = int(Decimal("34.0") * config.NANO)
    snapshot.model_listed_count_excl_self = 5
    snapshot.model_floor_status = "ok"
    snapshot.pair_floor_status = "alone_in_pair"
    db.upsert_listing_with_floor(conn, listing, snapshot)
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    _add_drop(conn, "bling-binky-1", 0, "230.0", "210.0", "-8.7", is_noise=False)

    cascade = run_cascade(conn, now=now)
    assert len(cascade.price_above_own_floor) == 1
    assert cascade.price_above_own_floor[0]["listing_external_id"] == "bling-binky-1"
    assert cascade.clean == []

    sigs = clean_signals(conn, now=now)
    assert sigs == []


def test_price_below_own_floor_passes_on_portals():
    """КАК ТЕСТИРОВАТЬ item 2: price 17.80, floor 29.00 -> passes."""
    conn = db.connect(":memory:")
    listing = _listing("passes-1", int(Decimal("17.80") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("29.00") * config.NANO)))
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    _add_drop(conn, "passes-1", 0, "20.0", "17.80", "-11.0", is_noise=False, floor_at_drop="29.00")

    sigs = clean_signals(conn, now=now)
    assert len(sigs) == 1
    assert sigs[0].listing_external_id == "passes-1"


def test_price_equal_to_own_floor_rejected():
    """КАК ТЕСТИРОВАТЬ item 3: price == floor -> rejected (no benefit)."""
    from gift_sniper.signals import run_cascade

    conn = db.connect(":memory:")
    listing = _listing("equal-1", int(Decimal("29.00") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("29.00") * config.NANO)))
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    _add_drop(conn, "equal-1", 0, "35.0", "29.00", "-17.1", is_noise=False, floor_at_drop="29.00")

    cascade = run_cascade(conn, now=now)
    assert len(cascade.price_above_own_floor) == 1
    assert cascade.clean == []


def test_price_above_own_floor_never_duplicated_per_marketplace():
    """Per spec: one shared stage, not one per marketplace -- confirmed
    by exercising the SAME code path (run_cascade) for both, with no
    marketplace-specific branch left in the cascade for this check.
    """
    import inspect
    from gift_sniper import signals as signals_module

    source = inspect.getsource(signals_module.run_cascade)
    # The stage itself must not be conditioned on `marketplace ==`.
    price_above_idx = source.index("price_above_own_floor")
    # No "if marketplace ==" between no_floor and thin_book (where this
    # stage now lives) -- the old Tonnel-only conditional is gone.
    stage_region = source[price_above_idx - 200:price_above_idx + 400]
    assert 'marketplace == "tonnel"' not in stage_region
