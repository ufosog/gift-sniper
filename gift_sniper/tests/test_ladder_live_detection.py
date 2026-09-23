"""КАК ТЕСТИРОВАТЬ (min-1.7-signals-per-lot delivery) -- is_ladder is
now computed LIVE at selection time (signals._ladder_listings_live),
never depending on a separate prior backfill_ladder() pass. See
run_cascade's docstring and README.md's newest entry.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.models import FloorSnapshot, Listing
from gift_sniper.report import generate_report
from gift_sniper.signals import clean_signals


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


def _snapshot(listing, pair_floor_nano) -> FloorSnapshot:
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
        pair_floor_status="ok",
        pair_floor_excl_self_nano=pair_floor_nano,
        pair_listed_count_excl_self=10,
    )


def _record_drop(conn, marketplace, ext_id, old, new, delta_pct, observed_at, is_noise=False, floor_at_drop="200"):
    db.record_price_change(
        conn, marketplace, ext_id,
        old_price_nano=int(Decimal(old) * config.NANO),
        new_price_nano=int(Decimal(new) * config.NANO),
        delta_pct=Decimal(str(delta_pct)),
        is_noise=is_noise,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal(floor_at_drop) * config.NANO),
        floor_listed_count_at_drop=5,
        floor_fetched_at=observed_at,
        floor_level_at_drop="pair",
    )


def _seed_ladder(conn, marketplace, ext_id, n_drops, step_minutes=30, is_noise=False, floor="200"):
    listing = _listing(ext_id, int(Decimal("110.0") * config.NANO), marketplace=marketplace)
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal(floor) * config.NANO)))
    price = Decimal("110.0")
    t = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    last_t = t
    for _ in range(n_drops):
        old = price
        price = price * Decimal("0.95")
        _record_drop(conn, marketplace, ext_id, str(old), str(price), "-5.0", t, is_noise=is_noise, floor_at_drop=floor)
        last_t = t
        t = t + timedelta(minutes=step_minutes)
    return last_t


# --- ПРАВКА 3: fails on the old implementation (which relied on a stale ---
# --- precomputed is_ladder column never refreshed at selection time) ---

def test_ladder_without_backfill_ladder_call_produces_no_signals():
    """Правка 3's required test: seed a 5-drop ladder, never call
    backfill_ladder() as a CALLER (nothing here calls it directly, unlike
    the old report.py-only code path), call clean_signals(), and confirm
    none of the ladder's rows became signals. Must fail against a version
    of the code that reads a stale is_ladder column instead of computing
    it live -- see _ladder_listings_live. (run_cascade does still write
    price_history.is_ladder itself, internally, on every call -- purely
    to keep that column available for anything else that reads it
    directly; filtering above never reads it back, only the freshly
    computed set from _ladder_listings_live -- see run_cascade.)
    """
    conn = db.connect(":memory:")
    last_t = _seed_ladder(conn, "portals", "ladder-1", 5)

    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=last_t)
    assert sigs == []


# --- item 1: 19 consecutive drops, backfill_ladder never called -> no signals ---

def test_item1_19_drops_no_backfill_call_zero_signals():
    conn = db.connect(":memory:")
    last_t = _seed_ladder(conn, "portals", "ladder-19", 19)
    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=last_t)
    assert sigs == []


# --- item 2: 2 significant drops in 24h -> both pass (threshold 3 not reached) ---

def test_item2_two_significant_drops_pass():
    conn = db.connect(":memory:")
    last_t = _seed_ladder(conn, "portals", "two-drops", 2)
    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=last_t)
    assert len(sigs) == 2


# --- item 3: 3 drops, 2 of them noise -> the 1 significant drop passes ---

def test_item3_only_significant_drops_are_counted():
    conn = db.connect(":memory:")
    listing = _listing("mixed-noise", int(Decimal("110.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("200.0") * config.NANO)))
    t = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    # 2 noise drops (is_noise=True) + 1 real significant drop -- total 3
    # rows, but only 1 counts toward LADDER_MIN_DROPS=3, well below it.
    _record_drop(conn, "portals", "mixed-noise", "110.0", "109.9", "-0.09", t, is_noise=True)
    t2 = t + timedelta(minutes=10)
    _record_drop(conn, "portals", "mixed-noise", "109.9", "109.8", "-0.09", t2, is_noise=True)
    t3 = t2 + timedelta(minutes=10)
    _record_drop(conn, "portals", "mixed-noise", "109.8", "95.0", "-13.5", t3, is_noise=False)

    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=t3)
    assert len(sigs) == 1


# --- item 4: drops older than LADDER_WINDOW_HOURS (relative to the ---
# --- CANDIDATE row's own observed_at, not to `now`) don't count ---

def test_item4_drops_outside_window_are_not_counted():
    """A drop more than LADDER_WINDOW_HOURS away from the other 3 close-
    together drops must NOT be pulled into their count -- it's an
    isolated event, not part of the same burst. Evaluating this long
    after the fact (`now` far in the future) must give the SAME answer
    as evaluating it right away -- the window is anchored to each row's
    own observed_at, never to `now` (see _ladder_listings_live's
    docstring: anchoring to `now` was the exact bug that let 15/20 real
    Clover Pin/Maple Leaf drops leak through when evaluated days later).
    """
    conn = db.connect(":memory:")
    listing = _listing("old-drops", int(Decimal("110.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("200.0") * config.NANO)))
    t0 = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    _record_drop(conn, "portals", "old-drops", "110.0", "104.5", "-5.0", t0)
    _record_drop(conn, "portals", "old-drops", "104.5", "99.3", "-5.0", t0 + timedelta(hours=1))
    _record_drop(conn, "portals", "old-drops", "99.3", "94.3", "-5.0", t0 + timedelta(hours=2))
    # A 4th drop far outside LADDER_WINDOW_HOURS of the other 3 -- an
    # isolated event on the same lot, not part of that burst.
    t_isolated = t0 + timedelta(hours=2 + config.LADDER_WINDOW_HOURS * 2)
    _record_drop(conn, "portals", "old-drops", "94.3", "89.6", "-5.0", t_isolated)

    for now in (t_isolated, t_isolated + timedelta(days=30)):
        sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=now)
        assert len(sigs) == 1, f"now={now}"
        assert sigs[0].observed_at.replace(tzinfo=timezone.utc) == t_isolated


# --- item 5: a Portals ladder does not affect Tonnel counting for the same external_id ---

def test_item5_ladder_on_one_marketplace_does_not_affect_the_other():
    conn = db.connect(":memory:")
    ext_id = "shared-id"
    last_t = _seed_ladder(conn, "portals", ext_id, 5)
    # Same external_id, single drop on Tonnel -- must not be treated as
    # part of Portals' ladder.
    listing_tonnel = _listing(ext_id, int(Decimal("110.0") * config.NANO), marketplace="tonnel")
    db.upsert_listing_with_floor(conn, listing_tonnel, _snapshot(listing_tonnel, int(Decimal("200.0") * config.NANO)))
    _record_drop(conn, "tonnel", ext_id, "110.0", "104.5", "-5.0", last_t)

    portals_sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=last_t, marketplace="portals")
    tonnel_sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=last_t, marketplace="tonnel")
    assert portals_sigs == []
    assert len(tonnel_sigs) == 1


# --- item 6: report.py still prints the is_ladder stage with a correct count ---

def test_item6_report_still_prints_is_ladder_stage_with_correct_count():
    conn = db.connect(":memory:")
    last_t = _seed_ladder(conn, "portals", "report-ladder", 5)
    report_text = generate_report(conn, usd_rate=Decimal("1.0"), drops_now=last_t)
    assert "is_ladder" in report_text
    assert "ladder-down listings (is_ladder rows): 5 rows across 1 listings" in report_text


# --- Regression tests on the EXACT real listing that exposed the ---
# --- SECOND, real bug: Clover Pin / Maple Leaf, ---
# --- 01a08b5d-27e2-7a62-ae78-3095d9ddf091, 20 real drops of ~5% every ---
# --- ~30 minutes, all dated the same day. The first fix delivery's ---
# --- window was anchored at `now` (the call's evaluation time), same ---
# --- as backfill_ladder -- correct for a live poller cycle (now close ---
# --- to the drops) but wrong for anything evaluated well after the ---
# --- fact: with `now` days later, window_start = now - 24h fell AFTER ---
# --- every one of these 20 drops, so none were ever in range and the ---
# --- whole lot silently stopped being recognized. Proven live by ---
# --- widening `since` (0 -> 1 -> 12 -> 15 -> 15 as since went from ---
# --- -1d to -7d) -- since only post-filters an already-decided set of ---
# --- clean rows, so a changing count proves the is_ladder decision ---
# --- itself, not the since filter, was wrong. Fixed by anchoring the ---
# --- window at each CANDIDATE ROW's own observed_at instead of `now` ---
# --- (see _ladder_listings_live's docstring). ---

_REAL_LADDER_STEPS = [
    ("12:59:27", "37.95", "36.05"),
    ("13:20:42", "36.05", "34.24"),
    ("13:51:15", "34.24", "32.52"),
    ("14:21:34", "32.52", "30.89"),
    ("14:52:02", "30.89", "29.34"),
    ("15:22:24", "29.34", "27.87"),
    ("15:52:59", "27.87", "26.47"),
    ("16:23:00", "26.47", "25.14"),
    ("16:53:24", "25.14", "23.88"),
    ("17:23:35", "23.88", "22.68"),
    ("17:53:39", "22.68", "21.54"),
    ("18:24:04", "21.54", "20.46"),
]


def test_real_clover_pin_maple_leaf_ladder_produces_zero_signals():
    conn = db.connect(":memory:")
    ext_id = "01a08b5d-27e2-7a62-ae78-3095d9ddf091"
    listing = _listing(ext_id, int(Decimal("37.95") * config.NANO))
    # Floor just above the starting price -- realistic for this lot;
    # a floor far above (e.g. 200) would get caught by the UNRELATED
    # price_above_own_floor/implausible stages instead of testing
    # is_ladder in isolation.
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("40.0") * config.NANO)))
    last_t = None
    for t, old, new in _REAL_LADDER_STEPS:
        h, m, s = (int(x) for x in t.split(":"))
        ts = datetime(2026, 9, 10, h, m, s, tzinfo=timezone.utc)
        db.record_price_change(
            conn, "portals", ext_id,
            old_price_nano=int(Decimal(old) * config.NANO), new_price_nano=int(Decimal(new) * config.NANO),
            delta_pct=Decimal("-5.0"), is_noise=False, old_listed_at=None, new_listed_at=None,
            observed_at=ts, floor_at_drop_nano=int(Decimal("40.0") * config.NANO), floor_listed_count_at_drop=5,
        )
        last_t = ts

    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=last_t)
    assert sigs == []


def test_ladder_detection_holds_at_production_scale_with_many_candidates():
    """Rules out a large concurrent candidate set (many distinct
    listing_external_ids reaching the is_ladder stage at once, as on a
    real multi-day Portals DB) corrupting the IN(...) grouped-count
    query for one specific lot -- 1500 unrelated single-drop listings
    seeded alongside the real ladder above; the ladder must still be
    fully suppressed.
    """
    conn = db.connect(":memory:")
    t0 = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(1500):
        bg_id = f"bg-{i}"
        listing = _listing(bg_id, int(Decimal("100.0") * config.NANO))
        db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("150.0") * config.NANO)))
        db.record_price_change(
            conn, "portals", bg_id,
            old_price_nano=int(Decimal("100.0") * config.NANO), new_price_nano=int(Decimal("90.0") * config.NANO),
            delta_pct=Decimal("-10.0"), is_noise=False, old_listed_at=None, new_listed_at=None,
            observed_at=t0 + timedelta(minutes=i % 1000), floor_at_drop_nano=int(Decimal("150.0") * config.NANO),
            floor_listed_count_at_drop=5,
        )

    ext_id = "01a08b5d-27e2-7a62-ae78-3095d9ddf091"
    listing = _listing(ext_id, int(Decimal("37.95") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("40.0") * config.NANO)))
    last_t = None
    for t, old, new in _REAL_LADDER_STEPS:
        h, m, s = (int(x) for x in t.split(":"))
        ts = datetime(2026, 9, 10, h, m, s, tzinfo=timezone.utc)
        db.record_price_change(
            conn, "portals", ext_id,
            old_price_nano=int(Decimal(old) * config.NANO), new_price_nano=int(Decimal(new) * config.NANO),
            delta_pct=Decimal("-5.0"), is_noise=False, old_listed_at=None, new_listed_at=None,
            observed_at=ts, floor_at_drop_nano=int(Decimal("40.0") * config.NANO), floor_listed_count_at_drop=5,
        )
        last_t = ts

    sigs = clean_signals(conn, usd_rate=Decimal("1.0"), now=last_t)
    target_sigs = [s for s in sigs if s.listing_external_id == ext_id]
    assert target_sigs == []


def _seed_real_ladder_days_ago(conn, days_ago: int):
    """The exact reported fixture: 20 drops, step 5%, 30 min apart, all
    dated `days_ago` days before `now` -- must fail on the `now`-anchored
    window (the actual second bug): evaluated with `now` far from the
    drops, window_start = now - LADDER_WINDOW_HOURS falls AFTER all 20
    of them, so none are ever counted and the whole lot leaks through.
    """
    ext_id = "01a08b5d-27e2-7a62-ae78-3095d9ddf091"
    listing = _listing(ext_id, int(Decimal("37.95") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("40.0") * config.NANO)))
    base_day = datetime(2026, 9, 17, tzinfo=timezone.utc) - timedelta(days=days_ago)
    for t, old, new in _REAL_LADDER_STEPS:
        h, m, s = (int(x) for x in t.split(":"))
        ts = base_day.replace(hour=h, minute=m, second=s)
        db.record_price_change(
            conn, "portals", ext_id,
            old_price_nano=int(Decimal(old) * config.NANO), new_price_nano=int(Decimal(new) * config.NANO),
            delta_pct=Decimal("-5.0"), is_noise=False, old_listed_at=None, new_listed_at=None,
            observed_at=ts, floor_at_drop_nano=int(Decimal("40.0") * config.NANO), floor_listed_count_at_drop=5,
        )
    return ext_id


def test_real_ladder_4_days_old_since_5_days_produces_zero_signals():
    """Exact required regression test: the real 20-drop ladder, dated 4
    days ago, evaluated with since=now-5 days. Must give 0 signals for
    this lot -- fails on the `now`-anchored window (would give 15).
    """
    conn = db.connect(":memory:")
    ext_id = _seed_real_ladder_days_ago(conn, days_ago=4)
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    sigs = clean_signals(conn, since=now - timedelta(days=5), usd_rate=Decimal("1.0"), now=now)
    assert [s for s in sigs if s.listing_external_id == ext_id] == []


def test_real_ladder_4_days_old_since_1_day_also_zero():
    """Same fixture, narrower since=now-1 day -- the ladder falls outside
    the candidate window too, so still 0, for either reason (correctly
    filtered by since, or correctly recognized as a ladder either way).
    """
    conn = db.connect(":memory:")
    ext_id = _seed_real_ladder_days_ago(conn, days_ago=4)
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    sigs = clean_signals(conn, since=now - timedelta(days=1), usd_rate=Decimal("1.0"), now=now)
    assert [s for s in sigs if s.listing_external_id == ext_id] == []


def test_real_ladder_since_sweep_always_zero():
    """The full since-sweep from the live report that proved the bug (0,
    1, 12, 15, 15 as since widened from -1d to -7d on the OLD code) --
    must be 0 at every point now, since the ladder decision no longer
    depends on `since` or `now` at all, only on the lot's own drop
    history.
    """
    conn = db.connect(":memory:")
    ext_id = _seed_real_ladder_days_ago(conn, days_ago=4)
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    for days in (1, 2, 3, 4, 7):
        sigs = clean_signals(conn, since=now - timedelta(days=days), usd_rate=Decimal("1.0"), now=now)
        assert [s for s in sigs if s.listing_external_id == ext_id] == [], f"since=now-{days}d"


def test_backfill_ladder_throttled_per_file_not_in_memory(tmp_path, monkeypatch):
    from gift_sniper import db, signals
    calls = []
    monkeypatch.setattr(signals, "backfill_ladder", lambda conn, now, marketplace: calls.append(marketplace))
    monkeypatch.setattr(signals, "_last_backfill", {})
    mem = db.connect(":memory:")
    signals._backfill_ladder_throttled(mem, None, "portals")
    signals._backfill_ladder_throttled(mem, None, "portals")
    assert calls == ["portals", "portals"]  # in-memory: every call
    filed = db.connect(str(tmp_path / "g.db"))
    signals._backfill_ladder_throttled(filed, None, "portals")
    signals._backfill_ladder_throttled(filed, None, "portals")
    signals._backfill_ladder_throttled(filed, None, "tonnel")
    assert calls == ["portals", "portals", "portals", "tonnel"]  # file: once per interval per marketplace
