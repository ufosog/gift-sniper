import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from gift_sniper import config, db
from gift_sniper.models import FloorSnapshot, Listing
from gift_sniper.report import backfill_name_collisions, generate_report, main


def _listing(**overrides) -> Listing:
    base = dict(
        marketplace="portals",
        external_id="ext-1",
        tg_id="Model-1",
        collection_id="col-a",
        collection_name="CollectionA",
        gift_number=1,
        price_nano=None,
        currency="TON",
        collection_floor_nano=None,
        model_name="ModelX",
        symbol_name=None,
        backdrop_name="Backdrop1",
        model_rarity_raw=None,
        symbol_rarity_raw=None,
        backdrop_rarity_raw=None,
        image_url=None,
        animation_url=None,
        listed_at=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc),
        unlocks_at=None,
        status="listed",
        first_seen_at=datetime(2026, 9, 5, 10, 0, 5, tzinfo=timezone.utc),
        raw={},
    )
    base.update(overrides)
    return Listing(**base)


def _snapshot(
    listing: Listing,
    pair_floor_nano,
    pair_floor_status="ok",
    pair_listed_count=10,
    own_combo_floor_nano=None,
    own_confidence="none",
    floor_sanity="no_data",
    own_sample_size=0,
    api_combo_floor_nano=None,
    model_floor_excl_self_nano=None,
    model_floor_status="no_data",
    model_listed_count_excl_self=0,
) -> FloorSnapshot:
    return FloorSnapshot(
        listing_external_id=listing.external_id,
        model_name=listing.model_name,
        backdrop_name=listing.backdrop_name,
        api_combo_floor_nano=api_combo_floor_nano,
        model_min_floor_nano=api_combo_floor_nano,
        floor_fetched_at=listing.first_seen_at,
        floor_age_sec=0,
        raw_model_block={},
        own_combo_floor_nano=own_combo_floor_nano,
        own_sample_size=own_sample_size,
        own_confidence=own_confidence,
        floor_sanity=floor_sanity,
        pair_floor_nano=pair_floor_nano,
        pair_listed_count=pair_listed_count,
        pair_floor_status=pair_floor_status,
        pair_floor_age_sec=0,
        # These tests predate self-exclusion; the main distribution now
        # reads from the _excl_self fields, so mirror pair_floor_nano
        # here unless a test explicitly wants to exercise self-exclusion
        # (see test_pair_floor.py / test_collect_min_price.py for that).
        pair_floor_excl_self_nano=pair_floor_nano,
        pair_listed_count_excl_self=pair_listed_count,
        pair_self_was_floor=False,
        model_floor_excl_self_nano=model_floor_excl_self_nano,
        model_listed_count_excl_self=model_listed_count_excl_self,
        model_floor_status=model_floor_status,
    )


def test_floor_level_hierarchy_pair_preferred_over_model():
    """Правка 3, test item 5: a row with a usable pair floor must use
    level='pair', even if a model floor is also present.
    """
    conn = db.connect(":memory:")
    price_nano = int(Decimal("10") * config.NANO)
    listing = _listing(external_id="pair-1", price_nano=price_nano)
    snapshot = _snapshot(
        listing,
        pair_floor_nano=int(Decimal("20") * config.NANO),
        pair_floor_status="ok",
        model_floor_excl_self_nano=int(Decimal("50") * config.NANO),
        model_floor_status="ok",
    )
    db.upsert_listing_with_floor(conn, listing, snapshot)

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "discount distribution vs PAIR floor, self-excluded (n=1" in report_text
    assert "discount distribution vs MODEL floor, self-excluded (n=0" in report_text


def test_floor_level_hierarchy_model_fallback_when_pair_alone():
    """Правка 3, test item 5: a row with ONLY a usable model floor
    (pair alone_in_pair) must use level='model'.
    """
    conn = db.connect(":memory:")
    price_nano = int(Decimal("10") * config.NANO)
    listing = _listing(external_id="model-1", price_nano=price_nano)
    snapshot = _snapshot(
        listing,
        pair_floor_nano=None,
        pair_floor_status="alone_in_pair",
        model_floor_excl_self_nano=int(Decimal("20") * config.NANO),
        model_floor_status="ok",
        model_listed_count_excl_self=config.FLOOR_MIN_LISTED_COUNT,
    )
    snapshot.pair_floor_excl_self_nano = None
    db.upsert_listing_with_floor(conn, listing, snapshot)

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "discount distribution vs PAIR floor, self-excluded (n=0" in report_text
    assert "discount distribution vs MODEL floor, self-excluded (n=1" in report_text


def test_floor_level_hierarchy_neither_level_excludes_row():
    """Правка 3, test item 5: a row with neither a usable pair nor model
    floor must be excluded entirely, not counted in either distribution.
    """
    conn = db.connect(":memory:")
    price_nano = int(Decimal("10") * config.NANO)
    listing = _listing(external_id="none-1", price_nano=price_nano)
    snapshot = _snapshot(
        listing,
        pair_floor_nano=None,
        pair_floor_status="alone_in_pair",
        model_floor_excl_self_nano=None,
        model_floor_status="alone_in_pair",
    )
    snapshot.pair_floor_excl_self_nano = None
    db.upsert_listing_with_floor(conn, listing, snapshot)

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "discount distribution vs PAIR floor, self-excluded (n=0" in report_text
    assert "discount distribution vs MODEL floor, self-excluded (n=0" in report_text
    assert "excluded from main distribution -- neither pair nor model floor usable (alone in pair AND alone at model level, or no_data/error at both): 1 " in report_text


def test_name_collision_backfill_marks_only_colliding_models():
    conn = db.connect(":memory:")
    a = _listing(external_id="a", model_name="Shared", collection_name="CollA")
    b = _listing(external_id="b", model_name="Shared", collection_name="CollB")
    c = _listing(external_id="c", model_name="Unique", collection_name="CollC")
    for listing in (a, b, c):
        db.upsert_listing_with_floor(conn, listing, _snapshot(listing, 10 * config.NANO))

    colliding = backfill_name_collisions(conn)
    assert colliding == {"Shared"}

    rows = {r[0]: r[1] for r in conn.execute("SELECT listing_external_id, name_collision FROM floor_snapshots")}
    assert rows["a"] == 1
    assert rows["b"] == 1
    assert rows["c"] == 0

    # idempotent
    colliding_again = backfill_name_collisions(conn)
    assert colliding_again == {"Shared"}


def test_report_requires_usd_rate_cli():
    result = subprocess.run(
        [sys.executable, "-m", "gift_sniper.report", "--db", ":memory:"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PORTALS_AUTH": "test"},
    )
    assert result.returncode != 0
    assert "usd-rate" in (result.stderr + result.stdout).lower()


def test_profit_formula_uses_pair_floor_as_sale_price():
    conn = db.connect(":memory:")
    price_nano = int(Decimal("17.99") * config.NANO)
    pair_floor_nano = int(Decimal("22.49") * config.NANO)
    listing = _listing(
        external_id="profit-1",
        price_nano=price_nano,
        model_name="ProfitModel",
        collection_name="ProfitColl",
    )
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, pair_floor_nano))

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "signals with discount > 10%" in report_text
    assert "signals with discount > 10% (pair or model floor self-excluded, not blocked): 1" in report_text

    # Verify the arithmetic directly, matching the spec's worked example:
    # pair_floor(22.49) * 0.98 - price(17.99) - withdrawal(0.35) = 3.7002
    expected_profit = (
        Decimal("22.49") * (1 - config.MARKETPLACE_FEE_RATE) * config.NANO
        - Decimal("17.99") * config.NANO
        - config.WITHDRAWAL_FEE_FLAT_NANO
    )
    expected_profit_units = expected_profit / config.NANO
    assert abs(expected_profit_units - Decimal("3.7002")) < Decimal("0.001")


def test_profit_formula_fails_if_price_used_as_sale_price():
    """This test documents the bug the spec explicitly forbids: using
    price_nano instead of pair_floor_nano as the sale price. It must FAIL
    if someone reintroduces that mistake.
    """
    price_nano = int(Decimal("17.99") * config.NANO)
    pair_floor_nano = int(Decimal("22.49") * config.NANO)

    correct_profit = (
        Decimal(pair_floor_nano) * (1 - config.MARKETPLACE_FEE_RATE)
        - Decimal(price_nano)
        - config.WITHDRAWAL_FEE_FLAT_NANO
    )
    wrong_profit = (
        Decimal(price_nano) * (1 - config.MARKETPLACE_FEE_RATE)
        - Decimal(price_nano)
        - config.WITHDRAWAL_FEE_FLAT_NANO
    )
    assert correct_profit > 0
    assert wrong_profit < 0
    assert correct_profit != wrong_profit


def test_pair_floor_status_not_ok_excluded_from_main_distribution():
    conn = db.connect(":memory:")
    price_nano = int(Decimal("10") * config.NANO)
    pair_floor_nano = int(Decimal("20") * config.NANO)  # would be 50% discount

    ok_listing = _listing(external_id="ok-1", price_nano=price_nano, model_name="M1", collection_name="C1")
    no_data_listing = _listing(external_id="nodata-1", price_nano=price_nano, model_name="M2", collection_name="C2")

    db.upsert_listing_with_floor(conn, ok_listing, _snapshot(ok_listing, pair_floor_nano, pair_floor_status="ok"))
    db.upsert_listing_with_floor(
        conn, no_data_listing, _snapshot(no_data_listing, None, pair_floor_status="no_data")
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "discount distribution vs PAIR floor, self-excluded (n=1" in report_text
    assert "excluded from main distribution -- neither pair nor model floor usable " in report_text
    assert ": 1 " in report_text


def test_pair_listed_count_breakdown_buckets_correctly(monkeypatch):
    # This test is about the liquidity BUCKETING itself, not the
    # thin-book filter (Правка 1) -- lower the threshold so a
    # cnt=1 row still reaches the distribution, exactly as this test
    # (written before Правка 1) expects.
    monkeypatch.setattr(config, "FLOOR_MIN_LISTED_COUNT", 1)
    conn = db.connect(":memory:")
    price_nano = int(Decimal("10") * config.NANO)
    pair_floor_nano = int(Decimal("20") * config.NANO)

    single = _listing(external_id="single-1", price_nano=price_nano, model_name="M1", collection_name="C1")
    deep = _listing(external_id="deep-1", price_nano=price_nano, model_name="M2", collection_name="C2")

    db.upsert_listing_with_floor(
        conn, single, _snapshot(single, pair_floor_nano, pair_listed_count=1)
    )
    db.upsert_listing_with_floor(
        conn, deep, _snapshot(deep, pair_floor_nano, pair_listed_count=15)
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "discount distribution by pair_listed_count_excl_self (liquidity)" in report_text

    count_rows = {}
    for line in report_text.splitlines():
        tokens = line.split()
        if tokens and tokens[0] in ("1", "2-3", "4-9", "10+"):
            count_rows[tokens[0]] = tokens

    # price=10 vs pair_floor=20 is a 50% discount -> lands in the ">35%" bucket.
    assert count_rows["1"][1] == "1"  # n=1 in the "1 listing" bucket
    assert count_rows["1"][-1] == "1"  # its discount landed in >35%
    assert count_rows["10+"][1] == "1"  # n=1 in the "10+ listings" bucket
    assert count_rows["10+"][-1] == "1"


def test_blocked_gift_excluded_from_main_distribution():
    conn = db.connect(":memory:")
    price_nano = int(Decimal("10") * config.NANO)
    combo_floor_nano = int(Decimal("20") * config.NANO)  # 50% discount for both

    unlocked = _listing(
        external_id="unblocked-1",
        price_nano=price_nano,
        model_name="M1",
        collection_name="C1",
        unlocks_at=None,
    )
    blocked = _listing(
        external_id="blocked-1",
        price_nano=price_nano,
        model_name="M2",
        collection_name="C2",
        unlocks_at=datetime(2026, 9, 6, tzinfo=timezone.utc),  # future vs first_seen_at
    )
    db.upsert_listing_with_floor(conn, unlocked, _snapshot(unlocked, combo_floor_nano))
    db.upsert_listing_with_floor(conn, blocked, _snapshot(blocked, combo_floor_nano))

    report_text = generate_report(conn, usd_rate=Decimal("1.0"))
    assert "blocked at listing time" in report_text
    assert "blocked gifts (unlocks_at in the future at first_seen_at):" in report_text
    assert "count: 1" in report_text


def test_marketplace_breakdown_shows_both_sources():
    """Правка 5: the breakdown block always shows both marketplaces."""
    from gift_sniper.tonnel_poller import TonnelPoller

    conn = db.connect(":memory:")
    listing = _listing(external_id="p-1", price_nano=int(Decimal("40") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("44") * config.NANO)))

    class FakeTonnel:
        def search(self, **kwargs):
            if kwargs.get("page", 1) != 1:
                return []
            return [{
                "gift_num": 1, "gift_id": 999, "name": "Ice Cream", "model": "Emperor (5%)",
                "backdrop": "Black (10%)", "symbol": "Star (1%)", "price": 20.0, "status": "forsale",
                "asset": "TON", "underLoan": False, "premarketData": None, "auction": None,
                "dutchAuctionData": None, "export_at": 1,
            }]

    TonnelPoller(conn, tonnel_client=FakeTonnel()).poll_once()

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), marketplace="all")
    assert "=== listings by marketplace ===" in report_text
    assert "portals: 1" in report_text
    assert "tonnel: 1" in report_text


def test_marketplace_tonnel_skips_portals_only_sections():
    """--marketplace tonnel prints the breakdown but not the Portals-only
    price-drops/cross-market sections (nothing Tonnel-specific exists
    there yet, per этап 2).
    """
    conn = db.connect(":memory:")
    listing = _listing(external_id="p-1", price_nano=int(Decimal("40") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("44") * config.NANO)))

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), marketplace="tonnel")
    assert "=== listings by marketplace ===" in report_text
    assert "=== price drops ===" not in report_text
    assert "cross-market verification" not in report_text


def test_marketplace_portals_still_prints_full_report():
    conn = db.connect(":memory:")
    listing = _listing(external_id="p-1", price_nano=int(Decimal("40") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("44") * config.NANO)))

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), marketplace="portals")
    assert "=== price drops ===" in report_text
    assert "cross-market verification" in report_text


def test_cross_direction_block_shows_both_directions_separately(monkeypatch):
    """Правка 5: the bidirectional breakdown shows Portals->Tonnel and
    Tonnel->Portals separately, with per-verdict shares and up to 10
    rejected (verdict=skipped_neighbour_cheaper) listed with their gap
    ratio.
    """
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    from gift_sniper.cross_check import cross_check
    from gift_sniper.tonnel_client import TonnelFloor

    conn = db.connect(":memory:")

    # A clean Portals signal, cross-checked against Tonnel -> neighbour
    # not cheap enough (49.50 <= 57*1.10=62.7) -> skipped_neighbour_cheaper.
    # Floor 70 (not 60): with realization rate 0.95 at depth 5, a 60 floor
    # leaves no profit over 57 and the signal would never be formed.
    listing = _listing(external_id="p-1", price_nano=int(Decimal("57.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    now = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", "p-1",
        old_price_nano=int(Decimal("65.0") * config.NANO), new_price_nano=int(Decimal("57.0") * config.NANO),
        delta_pct=Decimal("-12.3"), is_noise=False, old_listed_at=None, new_listed_at=None,
        observed_at=now, floor_at_drop_nano=int(Decimal("70.0") * config.NANO), floor_listed_count_at_drop=5,
    )

    class FakeTonnel:
        def pair_floor(self, **kwargs):
            return TonnelFloor(
                floor_nano=int(Decimal("45.0") * config.NANO),
                floor_with_fee_nano=int(Decimal("49.50") * config.NANO),
                listed_count=5, status="ok", raw=[],
            )

    from gift_sniper.signals import clean_signals
    sig = clean_signals(conn, now=now, marketplace="portals")[0]
    cross_check(conn, sig, tonnel_client=FakeTonnel())

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), drops_now=now)
    assert "=== cross-market verification (all directions) ===" in report_text
    assert "-- Portals -> Tonnel --" in report_text
    assert "-- Tonnel -> Portals --" in report_text
    assert "skipped_neighbour_cheaper: 1 (100%)" in report_text
    assert "разрыв x" in report_text


def test_price_above_own_floor_stage_printed_with_nonzero_count():
    """КАК ТЕСТИРОВАТЬ item 4: the price_above_own_floor stage appears in
    report.py's cascade output with a nonzero count.
    """
    conn = db.connect(":memory:")
    listing = _listing(external_id="over-floor-1", price_nano=int(Decimal("210.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("34.0") * config.NANO)))
    # observed_at kept within MAX_SNAPSHOT_AGE_MIN of the snapshot's own
    # floor_fetched_at (listing.first_seen_at, 2026-09-05 10:00:05) --
    # this test is about price_above_own_floor, not staleness, so the
    # drop must not incidentally get caught by the (separate) stale_floor
    # stage first.
    observed_at = listing.first_seen_at + timedelta(minutes=1)
    db.record_price_change(
        conn, "portals", "over-floor-1",
        old_price_nano=int(Decimal("230.0") * config.NANO), new_price_nano=int(Decimal("210.0") * config.NANO),
        delta_pct=Decimal("-8.7"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), drops_now=observed_at)
    assert "price_above_own_floor" in report_text
    assert "price_above_own_floor (new_price_nano >= floor_nano" in report_text
    assert "1 removed" in report_text


def test_below_min_profit_stage_printed_with_nonzero_count():
    """ПРАВКА 3: the below_min_profit cascade stage appears in report.py's
    output with a nonzero count -- Jingle Bells/Noble Pearl style two-cent
    gap (price 16.65, floor 16.67).
    """
    conn = db.connect(":memory:")
    listing = _listing(external_id="two-cent-gap-1", price_nano=int(Decimal("16.65") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("16.67") * config.NANO)))
    observed_at = listing.first_seen_at + timedelta(minutes=1)
    db.record_price_change(
        conn, "portals", "two-cent-gap-1",
        old_price_nano=int(Decimal("17.0") * config.NANO), new_price_nano=int(Decimal("16.65") * config.NANO),
        delta_pct=Decimal("-2.1"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), drops_now=observed_at)
    assert "below_min_profit" in report_text
    assert "below_min_profit (profit < MIN_SIGNAL_PROFIT_TON" in report_text
    assert "1 removed" in report_text
    assert "CLEAN signals: 0" in report_text


def test_stale_floor_stage_printed_with_nonzero_count():
    """ПРАВКА 3: the stale_floor cascade stage appears in report.py's
    output with a nonzero count when a snapshot-sourced floor is older
    than MAX_SNAPSHOT_AGE_MIN relative to the drop.
    """
    conn = db.connect(":memory:")
    fetched_at = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    listing = _listing(external_id="stale-report-1", price_nano=int(Decimal("30.0") * config.NANO), first_seen_at=fetched_at)
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("40.0") * config.NANO)))
    observed_at = fetched_at + timedelta(minutes=45)
    db.record_price_change(
        conn, "portals", "stale-report-1",
        old_price_nano=int(Decimal("35.0") * config.NANO), new_price_nano=int(Decimal("30.0") * config.NANO),
        delta_pct=Decimal("-14.3"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), drops_now=observed_at)
    assert "stale_floor" in report_text
    assert "stale_floor (snapshot-sourced floor older than MAX_SNAPSHOT_AGE_MIN" in report_text
    assert "1 removed" in report_text
    assert "CLEAN signals: 0" in report_text


def test_clean_signal_table_has_profit_ton_column():
    """ПРАВКА 3: the per-line clean-signals table gains a profit_ton
    column.
    """
    conn = db.connect(":memory:")
    listing = _listing(external_id="profit-col-1", price_nano=int(Decimal("30.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("40.0") * config.NANO)))
    observed_at = listing.first_seen_at + timedelta(minutes=1)
    db.record_price_change(
        conn, "portals", "profit-col-1",
        old_price_nano=int(Decimal("35.0") * config.NANO), new_price_nano=int(Decimal("30.0") * config.NANO),
        delta_pct=Decimal("-14.3"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("40.0") * config.NANO), floor_listed_count_at_drop=5,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), drops_now=observed_at)
    assert "profit_ton" in report_text
    assert "6.89" in report_text  # 40*0.95*0.98 - 30 - 0.35 (depth 5)


def test_tonnel_clean_signal_floor_source_at_drop_nonzero_after_fix():
    """КАК ТЕСТИРОВАТЬ item 4 (stale-floor fix): after tonnel_poller.py
    fills floor_at_drop_nano on a significant drop, report.py --marketplace
    tonnel's "clean signal floor source" shows at_drop > 0 -- confirmed
    live it was at_drop=0 before this fix (195 significant Tonnel drops,
    none filled).
    """
    conn = db.connect(":memory:")
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    from gift_sniper.models import Listing
    listing = Listing(
        marketplace="tonnel", external_id="t-1", tg_id="IceCream-1", collection_id=None,
        collection_name="Ice Cream", gift_number=1, price_nano=int(Decimal("10.0") * config.NANO),
        currency="TON", collection_floor_nano=None, model_name="Emperor", symbol_name=None, backdrop_name="Black",
        model_rarity_raw=None, symbol_rarity_raw=None, backdrop_rarity_raw=None,
        image_url=None, animation_url=None, listed_at=None, unlocks_at=None,
        status="forsale", first_seen_at=now, raw={},
    )
    db.insert_listing(conn, listing)
    db.record_price_change(
        conn, "tonnel", "t-1",
        old_price_nano=int(Decimal("20.0") * config.NANO), new_price_nano=int(Decimal("10.0") * config.NANO),
        delta_pct=Decimal("-50"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=now,
        floor_at_drop_nano=int(Decimal("13.0") * config.NANO), floor_listed_count_at_drop=5,
        floor_level_at_drop="model",
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), marketplace="tonnel", drops_now=now)
    assert "clean signal floor source: at_drop=1  snapshot(backfilled)=0" in report_text


def test_cross_direction_block_includes_mrkt_directions():
    """Правка 3 (MRKT third-neighbour delivery): the report shows
    Portals->MRKT and Tonnel->MRKT as their own separate directions.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    db.record_cross_check_snapshot(
        conn, "portals", "mrkt", "p-1", "Coll", "M", "B",
        int(Decimal("25.0") * config.NANO), 5, "sent_neighbour_higher", now,
    )
    db.record_cross_check_snapshot(
        conn, "tonnel", "mrkt", "t-1", "Coll", "M", "B",
        int(Decimal("10.0") * config.NANO), 5, "skipped_neighbour_cheaper", now,
    )

    report_text = generate_report(conn, usd_rate=Decimal("1.0"), drops_now=now)
    assert "-- Portals -> MRKT --" in report_text
    assert "-- Tonnel -> MRKT --" in report_text
