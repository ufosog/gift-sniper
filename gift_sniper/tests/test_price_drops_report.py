from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.models import FloorSnapshot, Listing
from gift_sniper.report import generate_report


def _listing(ext_id, price_nano) -> Listing:
    now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    return Listing(
        marketplace="portals",
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


def _snapshot(listing, pair_floor_nano, pair_floor_status="ok") -> FloorSnapshot:
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
        pair_listed_count=10,
        pair_floor_status=pair_floor_status,
        pair_floor_excl_self_nano=pair_floor_nano,
        pair_listed_count_excl_self=10,
    )


def test_price_drops_block_reports_clean_signal_with_floor_at_drop():
    conn = db.connect(":memory:")

    # Floor 80 (not 70): depth 3 -> realization rate 0.90, a 70 floor no
    # longer leaves any profit over 65.93.
    listing = _listing("drop-1", int(Decimal("65.93") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("80.0") * config.NANO)))

    db.record_price_change(
        conn,
        "portals",
        "drop-1",
        old_price_nano=int(Decimal("67.91") * config.NANO),
        new_price_nano=int(Decimal("65.93") * config.NANO),
        delta_pct=Decimal("-2.92"),
        is_noise=False,
        old_listed_at=None,
        new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("80.0") * config.NANO),
        floor_listed_count_at_drop=3,
        floor_fetched_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        is_anomaly=False,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "=== price drops ===" in report_text
    assert "total price changes recorded: 1" in report_text
    assert "drops: 1  raises: 0" in report_text
    assert "CLEAN signals: 1" in report_text
    # Line-item output must exist (the whole point per spec -- aggregates hid the artifacts):
    assert "CollA" in report_text


def test_price_drops_block_excludes_noise_from_clean_signals():
    conn = db.connect(":memory:")
    listing = _listing("noise-1", int(Decimal("24.95") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("30.0") * config.NANO)))

    db.record_price_change(
        conn, "portals", "noise-1",
        old_price_nano=int(Decimal("24.99") * config.NANO),
        new_price_nano=int(Decimal("24.95") * config.NANO),
        delta_pct=Decimal("-0.16"),
        is_noise=True,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        # noise drops never get a floor re-fetch -- floor_at_drop_nano stays None
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "total price changes recorded: 1" in report_text
    assert "below PRICE_DROP_MIN_PCT (1.0%, bot noise): 1 removed, 0 remain" in report_text
    assert "CLEAN signals: 0" in report_text


def test_price_drops_block_excludes_anomaly_from_clean_signals():
    conn = db.connect(":memory:")
    listing = _listing("anomaly-1", int(Decimal("29") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("999") * config.NANO)))

    db.record_price_change(
        conn, "portals", "anomaly-1",
        old_price_nano=int(Decimal("999") * config.NANO),
        new_price_nano=int(Decimal("99") * config.NANO),
        delta_pct=Decimal("-90.09"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        is_anomaly=True,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "anomalous drops (is_anomaly): 1" in report_text
    assert "CLEAN signals: 0" in report_text


def test_price_drops_block_detects_ladder_and_excludes_from_clean_signals():
    conn = db.connect(":memory:")
    listing = _listing("ladder-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    # LADDER_MIN_DROPS default is 3 -- three steps within LADDER_WINDOW_HOURS.
    steps = [("60.0", "55.0"), ("55.0", "50.0"), ("50.0", "40.0")]
    for i, (old, new) in enumerate(steps):
        db.record_price_change(
            conn, "portals", "ladder-1",
            old_price_nano=int(Decimal(old) * config.NANO),
            new_price_nano=int(Decimal(new) * config.NANO),
            delta_pct=Decimal("-8.3"),
            is_noise=False,
            old_listed_at=None, new_listed_at=None,
            observed_at=datetime(2026, 9, 6, 10, i, tzinfo=timezone.utc),
            floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
            floor_listed_count_at_drop=3,
        )

    # Fixed reference point, NOT datetime.now() -- backfill_ladder's window
    # is relative to `now`, so a hardcoded fixture date must be paired
    # with an equally fixed `now`, not wall-clock time (which drifts the
    # fixture out of LADDER_WINDOW_HOURS the next day).
    report_text = generate_report(
        conn, usd_rate=Decimal("1.0"), drops_now=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert "ladder-down listings (is_ladder rows): 3 rows across 1 listings" in report_text
    assert "CLEAN signals: 0" in report_text  # all 3 steps excluded as ladder
    assert "ladder-1" in report_text  # ladder detail block names the listing


def test_price_drops_block_two_drops_below_min_are_not_a_ladder():
    conn = db.connect(":memory:")
    listing = _listing("two-drops-1", int(Decimal("50.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    for i, (old, new) in enumerate([("60.0", "55.0"), ("55.0", "50.0")]):
        db.record_price_change(
            conn, "portals", "two-drops-1",
            old_price_nano=int(Decimal(old) * config.NANO),
            new_price_nano=int(Decimal(new) * config.NANO),
            delta_pct=Decimal("-8.3"),
            is_noise=False,
            old_listed_at=None, new_listed_at=None,
            observed_at=datetime(2026, 9, 6, 10, i, tzinfo=timezone.utc),
            floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
            floor_listed_count_at_drop=3,
        )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "ladder-down listings (is_ladder rows): 0 rows across 0 listings" in report_text
    assert "CLEAN signals: 2" in report_text


def test_backfill_ladder_is_idempotent():
    conn = db.connect(":memory:")
    listing = _listing("idem-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    for i, (old, new) in enumerate([("60.0", "55.0"), ("55.0", "50.0"), ("50.0", "40.0")]):
        db.record_price_change(
            conn, "portals", "idem-1",
            old_price_nano=int(Decimal(old) * config.NANO),
            new_price_nano=int(Decimal(new) * config.NANO),
            delta_pct=Decimal("-8.3"),
            is_noise=False,
            old_listed_at=None, new_listed_at=None,
            observed_at=datetime(2026, 9, 6, 10, i, tzinfo=timezone.utc),
        )

    first = generate_report(conn, usd_rate=Decimal("1.0"))
    second = generate_report(conn, usd_rate=Decimal("1.0"))
    # Strip the generated_at line (always differs) before comparing.
    strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("generated_at"))
    assert strip(first) == strip(second)


def _add_drop(conn, ext_id, i, old, new, pct, is_noise, floor_at_drop=None):
    db.record_price_change(
        conn, "portals", ext_id,
        old_price_nano=int(Decimal(old) * config.NANO),
        new_price_nano=int(Decimal(new) * config.NANO),
        delta_pct=Decimal(str(pct)),
        is_noise=is_noise,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, i, tzinfo=timezone.utc),
        floor_at_drop_nano=(int(Decimal(str(floor_at_drop)) * config.NANO) if floor_at_drop else None),
        floor_listed_count_at_drop=3 if floor_at_drop else 0,
    )


def test_only_noise_drops_do_not_form_a_ladder():
    """4 drops of 0.05% each -- all below PRICE_DROP_MIN_PCT=1.0, so
    is_noise=True for all of them. None are "significant", so this must
    NOT be flagged is_ladder even though there are 4 (>= LADDER_MIN_DROPS)
    of them.
    """
    conn = db.connect(":memory:")
    listing = _listing("noise-ladder-1", int(Decimal("50.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    prices = [Decimal("50.025"), Decimal("50.0"), Decimal("49.975"), Decimal("49.95"), Decimal("49.925")]
    for i in range(4):
        _add_drop(conn, "noise-ladder-1", i, prices[i], prices[i + 1], "-0.05", is_noise=True)

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "ladder-down listings (is_ladder rows): 0 rows across 0 listings" in report_text


def test_three_significant_drops_form_a_ladder():
    conn = db.connect(":memory:")
    listing = _listing("real-ladder-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    steps = [("60.0", "57.0"), ("57.0", "54.15"), ("54.15", "51.4")]
    for i, (old, new) in enumerate(steps):
        _add_drop(conn, "real-ladder-1", i, old, new, "-5.0", is_noise=False, floor_at_drop=70.0)

    report_text = generate_report(
        conn, usd_rate=Decimal("1.0"), drops_now=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    )
    assert "ladder-down listings (is_ladder rows): 3 rows across 1 listings" in report_text


def test_mixed_noise_and_significant_drops_counts_only_significant_ones():
    """10 noise-level ticks (0.05%) plus exactly 3 significant drops
    (4% each) on the same listing: must be flagged is_ladder (3
    significant drops meets LADDER_MIN_DROPS=3), and the noise ticks must
    not inflate the count further.
    """
    conn = db.connect(":memory:")
    listing = _listing("mixed-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    price = Decimal("100.0")
    for i in range(10):
        new_price = price * Decimal("0.9995")
        _add_drop(conn, "mixed-1", i, price, new_price, "-0.05", is_noise=True)
        price = new_price

    for j in range(3):
        new_price = price * Decimal("0.96")
        _add_drop(conn, "mixed-1", 10 + j, price, new_price, "-4.0", is_noise=False, floor_at_drop=70.0)
        price = new_price

    report_text = generate_report(
        conn, usd_rate=Decimal("1.0"), drops_now=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    )
    # backfill_ladder() flags ALL 13 of the listing's price_history rows
    # is_ladder=1 (it's a property of the lot). But the cascade's
    # "ladder rows" count is drawn from what's still in the REMAINING
    # pool at that stage -- and the 10 noise rows were already removed
    # one stage earlier, so only the 3 significant ones show up here.
    assert "ladder-down listings (is_ladder rows): 3 rows across 1 listings" in report_text
    all_flagged = conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE listing_external_id='mixed-1' AND is_ladder=1"
    ).fetchone()[0]
    assert all_flagged == 13


def test_clean_signal_falls_back_to_snapshot_floor_when_floor_at_drop_is_null():
    """A row with no floor_at_drop_nano (e.g. written before this fix, or
    the drop was noise) but a populated pair_floor_excl_self_nano on
    floor_snapshots (e.g. from a later ANALYTICS PATH run, or backfill.py)
    must still be usable, with floor_source="snapshot".
    """
    conn = db.connect(":memory:")
    listing = _listing("fallback-1", int(Decimal("65.93") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("80.0") * config.NANO)))

    db.record_price_change(
        conn, "portals", "fallback-1",
        old_price_nano=int(Decimal("67.91") * config.NANO),
        new_price_nano=int(Decimal("65.93") * config.NANO),
        delta_pct=Decimal("-2.92"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        # floor_at_drop_nano deliberately omitted -- stays NULL
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "CLEAN signals: 1" in report_text
    assert "clean signal floor source: at_drop=0  snapshot(backfilled)=1" in report_text
    assert "snapshot" in report_text  # the per-line 'src' column


def test_thin_book_signal_excluded_when_cnt_below_threshold():
    """КАК ТЕСТИРОВАТЬ item 1: cnt=1 with FLOOR_MIN_LISTED_COUNT=3
    (the default) -> excluded, counted in the thin-book stage.
    """
    conn = db.connect(":memory:")
    listing = _listing("thin-1", int(Decimal("23.91") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("230.0") * config.NANO)))

    db.record_price_change(
        conn, "portals", "thin-1",
        old_price_nano=int(Decimal("24.60") * config.NANO),
        new_price_nano=int(Decimal("23.91") * config.NANO),
        delta_pct=Decimal("-2.8"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("230.0") * config.NANO),
        floor_listed_count_at_drop=1,  # only one OTHER listing in the book
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "CLEAN signals: 0" in report_text
    assert "thin-book (listed_count_excl_self < FLOOR_MIN_LISTED_COUNT=3): 1 removed (cnt=1: 1, cnt=2: 0)" in report_text


def test_signal_with_cnt_5_survives_thin_book_filter():
    """КАК ТЕСТИРОВАТЬ item 2: cnt=5 -> remains a clean signal."""
    conn = db.connect(":memory:")
    listing = _listing("thick-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("50.0") * config.NANO)))

    db.record_price_change(
        conn, "portals", "thick-1",
        old_price_nano=int(Decimal("45.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-11.1"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("50.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "CLEAN signals: 1" in report_text


def test_implausible_ratio_excludes_signal():
    """КАК ТЕСТИРОВАТЬ item 3: floor=230, price=23.91 (ratio 9.6) with the
    default FLOOR_MAX_RATIO_TO_PRICE=4.0 -> excluded as is_implausible,
    even though cnt clears the thin-book threshold.
    """
    conn = db.connect(":memory:")
    listing = _listing("implausible-1", int(Decimal("23.91") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("230.0") * config.NANO)))

    db.record_price_change(
        conn, "portals", "implausible-1",
        old_price_nano=int(Decimal("24.60") * config.NANO),
        new_price_nano=int(Decimal("23.91") * config.NANO),
        delta_pct=Decimal("-2.8"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("230.0") * config.NANO),
        floor_listed_count_at_drop=5,  # clears the thin-book threshold
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "CLEAN signals: 0" in report_text
    assert "is_implausible (floor/price > FLOOR_MAX_RATIO_TO_PRICE=4.0): 1 removed" in report_text
    assert "implausible floor/price examples" in report_text


def test_plausible_ratio_survives():
    """КАК ТЕСТИРОВАТЬ item 4: floor=44, price=20.61 (ratio ~2.1) ->
    remains a clean signal.
    """
    conn = db.connect(":memory:")
    listing = _listing("light-sword-1", int(Decimal("20.61") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("44.0") * config.NANO)))

    db.record_price_change(
        conn, "portals", "light-sword-1",
        old_price_nano=int(Decimal("22.0") * config.NANO),
        new_price_nano=int(Decimal("20.61") * config.NANO),
        delta_pct=Decimal("-6.3"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("44.0") * config.NANO),
        floor_listed_count_at_drop=3,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "CLEAN signals: 1" in report_text
    assert "is_implausible" in report_text  # stage line present
    assert "is_implausible (floor/price > FLOOR_MAX_RATIO_TO_PRICE=4.0): 0 removed" in report_text


def test_bulk_update_same_delta_pct_same_second_excludes_all_three():
    """КАК ТЕСТИРОВАТЬ item 5, first half: three DIFFERENT listings in the
    same collection, identical delta_pct, all within SAME_SECOND_WINDOW
    (5s default) of each other -> all three is_bulk_update, excluded.
    """
    conn = db.connect(":memory:")
    t = datetime(2026, 9, 6, 19, 14, 32, tzinfo=timezone.utc)
    for i, ext_id in enumerate(["papakha-1", "papakha-2", "papakha-3"]):
        listing = _listing(ext_id, int(Decimal("23.91") * config.NANO))
        db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("60.0") * config.NANO)))
        db.record_price_change(
            conn, "portals", ext_id,
            old_price_nano=int(Decimal("24.60") * config.NANO),
            new_price_nano=int(Decimal("23.91") * config.NANO),
            delta_pct=Decimal("-2.8"),
            is_noise=False,
            old_listed_at=None, new_listed_at=None,
            observed_at=t,
            floor_at_drop_nano=int(Decimal("60.0") * config.NANO),
            floor_listed_count_at_drop=5,
        )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "CLEAN signals: 0" in report_text
    assert (
        "is_bulk_update (>= 2 different listings, same collection, same delta_pct, "
        "within SAME_SECOND_WINDOW=5s): 3 removed" in report_text
    )


def test_bulk_update_different_delta_pct_same_time_both_survive():
    """КАК ТЕСТИРОВАТЬ item 5, second half: two listings, same collection,
    same time, but DIFFERENT delta_pct -> not a bulk update, both remain.
    """
    conn = db.connect(":memory:")
    t = datetime(2026, 9, 6, 19, 14, 32, tzinfo=timezone.utc)

    listing_a = _listing("distinct-a", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing_a, _snapshot(listing_a, int(Decimal("50.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", "distinct-a",
        old_price_nano=int(Decimal("50.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-20.0"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=t,
        floor_at_drop_nano=int(Decimal("50.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    listing_b = _listing("distinct-b", int(Decimal("41.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing_b, _snapshot(listing_b, int(Decimal("50.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", "distinct-b",
        old_price_nano=int(Decimal("50.0") * config.NANO),
        new_price_nano=int(Decimal("41.0") * config.NANO),
        delta_pct=Decimal("-18.0"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=t,
        floor_at_drop_nano=int(Decimal("50.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "CLEAN signals: 2" in report_text
    assert (
        "is_bulk_update (>= 2 different listings, same collection, same delta_pct, "
        "within SAME_SECOND_WINDOW=5s): 0 removed" in report_text
    )


def test_report_filter_stages_idempotent_across_repeated_runs():
    """КАК ТЕСТИРОВАТЬ item 6: repeated report.py runs (which re-run the
    idempotent backfill passes) must produce identical output.
    """
    conn = db.connect(":memory:")
    listing = _listing("idem-thin-1", int(Decimal("23.91") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("230.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", "idem-thin-1",
        old_price_nano=int(Decimal("24.60") * config.NANO),
        new_price_nano=int(Decimal("23.91") * config.NANO),
        delta_pct=Decimal("-2.8"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("230.0") * config.NANO),
        floor_listed_count_at_drop=1,
    )

    first = generate_report(conn, usd_rate=Decimal("1.0"))
    second = generate_report(conn, usd_rate=Decimal("1.0"))
    strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("generated_at"))
    assert strip(first) == strip(second)


def test_report_still_prints_all_diagnostic_fields_after_minimal_notification_format_delivery():
    """КАК ТЕСТИРОВАТЬ item 3 (minimal-notification-format delivery):
    report.py's diagnostics are UNCHANGED -- only notifier.py's Telegram
    message got simplified. Every field the notification dropped
    (discount %, old price, listed_count/"в стакане", liquidity, profit,
    the model-floor caveat) must still be present in the report, since
    report.py is where this data is needed for diagnosis.
    """
    conn = db.connect(":memory:")
    listing = _listing("full-report-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("44.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", "full-report-1",
        old_price_nano=int(Decimal("45.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-11.1"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc),
        floor_at_drop_nano=int(Decimal("44.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    # The per-line clean-signals table's column headers (old/new/floor/
    # ratio/src/level/cnt) are still there.
    assert "old" in report_text and "new" in report_text and "floor" in report_text
    assert "ratio" in report_text and "src" in report_text and "level" in report_text and "cnt" in report_text
    # The profit-count summary line is still printed.
    assert "clean signal profit" in report_text
    # The floor-level breakdown is still printed.
    assert "clean signal floor level: pair=" in report_text
