from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.pair_floor import PairFloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def test_record_lifecycle_status_listed_keeps_disappeared_at_null():
    """КАК ТЕСТИРОВАТЬ item 1: a mock returning status="listed" ->
    disappeared_at stays NULL, last_checked_at is updated.
    """
    conn = db.connect(":memory:")
    seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.touch_listing_lifecycle(conn, "portals", "still-listed-1", "col-a", "Model", "Backdrop", 1000, seen_at)

    now = datetime.now(timezone.utc)
    db.record_lifecycle_status(conn, "portals", "still-listed-1", "listed", now)

    row = conn.execute(
        "SELECT disappeared_at, last_checked_at FROM listing_lifecycle WHERE listing_external_id = 'still-listed-1'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == now.isoformat()


def test_record_lifecycle_status_withdrawn_sets_disappeared_at_and_final_status():
    """КАК ТЕСТИРОВАТЬ item 2: a mock returning status="withdrawn" ->
    disappeared_at filled, final_status="withdrawn".
    """
    conn = db.connect(":memory:")
    seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.touch_listing_lifecycle(conn, "portals", "withdrawn-1", "col-a", "Model", "Backdrop", 1000, seen_at)

    now = datetime.now(timezone.utc)
    db.record_lifecycle_status(conn, "portals", "withdrawn-1", "withdrawn", now)

    row = conn.execute(
        "SELECT disappeared_at, final_status FROM listing_lifecycle WHERE listing_external_id = 'withdrawn-1'"
    ).fetchone()
    assert row[0] == now.isoformat()
    assert row[1] == "withdrawn"


def test_record_lifecycle_status_unlisted_also_counts_as_disappeared():
    conn = db.connect(":memory:")
    seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.touch_listing_lifecycle(conn, "portals", "unlisted-1", "col-a", "Model", "Backdrop", 1000, seen_at)

    now = datetime.now(timezone.utc)
    db.record_lifecycle_status(conn, "portals", "unlisted-1", "unlisted", now)

    row = conn.execute(
        "SELECT disappeared_at, final_status FROM listing_lifecycle WHERE listing_external_id = 'unlisted-1'"
    ).fetchone()
    assert row[0] == now.isoformat()
    assert row[1] == "unlisted"


def test_record_lifecycle_status_unknown_value_does_not_mark_disappeared():
    """Any status value other than the known set must be treated as
    "unknown", not as evidence of disappearance -- per spec.
    """
    conn = db.connect(":memory:")
    seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.touch_listing_lifecycle(conn, "portals", "weird-status-1", "col-a", "Model", "Backdrop", 1000, seen_at)

    now = datetime.now(timezone.utc)
    db.record_lifecycle_status(conn, "portals", "weird-status-1", "some_future_status", now)

    row = conn.execute(
        "SELECT disappeared_at, last_checked_at FROM listing_lifecycle WHERE listing_external_id = 'weird-status-1'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == now.isoformat()


def test_record_lifecycle_check_missing_does_not_mark_disappeared():
    """КАК ТЕСТИРОВАТЬ item 3: the listing is absent from the API
    response entirely -> disappeared_at NOT filled (only last_checked_at
    bumps, so it doesn't monopolize the check queue).
    """
    conn = db.connect(":memory:")
    seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.touch_listing_lifecycle(conn, "portals", "missing-1", "col-a", "Model", "Backdrop", 1000, seen_at)

    now = datetime.now(timezone.utc)
    db.record_lifecycle_check_missing(conn, "portals", "missing-1", now)

    row = conn.execute(
        "SELECT disappeared_at, last_checked_at FROM listing_lifecycle WHERE listing_external_id = 'missing-1'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == now.isoformat()


def test_reappearance_clears_disappeared_at_and_increments_count():
    """КАК ТЕСТИРОВАТЬ item 4: a listing with disappeared_at set that is
    seen again (via the FAST PATH feed scan, touch_listing_lifecycle --
    NOT the background API check, which never re-queries already-
    disappeared rows) -> disappeared_at cleared, reappeared_count=1.
    """
    conn = db.connect(":memory:")
    seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.touch_listing_lifecycle(conn, "portals", "relist-1", "col-a", "Model", "Backdrop", 1000, seen_at)
    db.record_lifecycle_status(conn, "portals", "relist-1", "withdrawn", datetime.now(timezone.utc))

    row = conn.execute(
        "SELECT disappeared_at, reappeared_count FROM listing_lifecycle WHERE listing_external_id = 'relist-1'"
    ).fetchone()
    assert row[0] is not None
    assert row[1] == 0

    db.touch_listing_lifecycle(conn, "portals", "relist-1", "col-a", "Model", "Backdrop", 950, datetime.now(timezone.utc))

    row = conn.execute(
        "SELECT disappeared_at, reappeared_count FROM listing_lifecycle WHERE listing_external_id = 'relist-1'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == 1


def test_get_lifecycle_check_batch_respects_limit_and_oldest_first():
    """КАК ТЕСТИРОВАТЬ item 5: the batch takes at most LIFECYCLE_BATCH_SIZE
    rows and picks the least-recently-checked ones (never-checked, i.e.
    last_checked_at IS NULL, first).
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    # Already checked, most recent first -- should be picked LAST.
    for i in range(3):
        db.touch_listing_lifecycle(conn, "portals", f"checked-{i}", "col-a", "M", "B", 1000, now)
        db.record_lifecycle_status(conn, "portals", f"checked-{i}", "listed", now - timedelta(minutes=i))
    # Never checked -- should be picked FIRST (NULL sorts first).
    for i in range(3):
        db.touch_listing_lifecycle(conn, "portals", f"unchecked-{i}", "col-a", "M", "B", 1000, now)

    batch = db.get_lifecycle_check_batch(conn, "portals", limit=4)
    assert len(batch) == 4
    ids = {row["listing_external_id"] for row in batch}
    # All 3 never-checked rows must be included (they sort first);
    # exactly 1 of the already-checked rows fills out the remaining slot.
    assert {f"unchecked-{i}" for i in range(3)}.issubset(ids)


def test_reappeared_listing_does_not_count_toward_pair_liquidity():
    """A listing that disappeared and then reappeared must NOT be counted
    as "gone" in pair_liquidity_stats -- it was delisted/relisted, not
    (possibly) sold.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    seen_at = now - timedelta(hours=1)
    db.touch_listing_lifecycle(conn, "portals", "relist-2", "col-a", "M", "B", 1000, seen_at)
    db.record_lifecycle_status(conn, "portals", "relist-2", "withdrawn", now)
    db.touch_listing_lifecycle(conn, "portals", "relist-2", "col-a", "M", "B", 950, now)  # reappears

    gone_count, median = db.pair_liquidity_stats(conn, "col-a", "M", "B", since=now - timedelta(hours=1))
    assert gone_count == 0
    assert median is None


def test_pair_liquidity_stats_with_five_gone_listings():
    """КАК ТЕСТИРОВАТЬ item 4: a pair with 5 disappeared listings ->
    pair_gone_count == 5, median computed.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    for i in range(5):
        first_seen = now - timedelta(hours=10 + i)  # varying ages -> a real median
        db.touch_listing_lifecycle(conn, "portals", f"gone-pair-{i}", "col-a", "Model", "Backdrop", 1000, first_seen)
        conn.execute(
            "UPDATE listing_lifecycle SET disappeared_at = ? WHERE listing_external_id = ?",
            ((first_seen + timedelta(hours=2 + i)).isoformat(), f"gone-pair-{i}"),
        )

    gone_count, median = db.pair_liquidity_stats(conn, "col-a", "Model", "Backdrop", since=now - timedelta(hours=168))
    assert gone_count == 5
    assert median is not None
    assert median > 0


def test_pair_liquidity_stats_below_min_sample_reports_zero_not_fabricated():
    """КАК ТЕСТИРОВАТЬ item 5: a pair with only 2 disappeared listings --
    db.pair_liquidity_stats() itself still reports the true count (2);
    the "insufficient data" decision (not showing a number) is made by
    the caller (signals.py / notifier.py) applying MIN_LIQUIDITY_SAMPLE,
    tested separately in test_signals.py.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    for i in range(2):
        first_seen = now - timedelta(hours=10 + i)
        db.touch_listing_lifecycle(conn, "portals", f"thin-pair-{i}", "col-a", "Model", "Backdrop", 1000, first_seen)
        conn.execute(
            "UPDATE listing_lifecycle SET disappeared_at = ? WHERE listing_external_id = ?",
            ((first_seen + timedelta(hours=3)).isoformat(), f"thin-pair-{i}"),
        )

    gone_count, median = db.pair_liquidity_stats(conn, "col-a", "Model", "Backdrop", since=now - timedelta(hours=168))
    assert gone_count == 2
    assert median is not None


def _build_lifecycle_poller(conn, by_ids_responses):
    client = FakePortalsClient(pages=[[], []], by_ids_responses=by_ids_responses)
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    pair_floor_cache = PairFloorCache(client, ttl_sec=300)
    poller = Poller(conn, client, auth, floor_cache, pair_floor_cache)
    return poller, client


def test_poller_lifecycle_check_batch_end_to_end():
    """End-to-end: withdrawn -> disappeared, listed -> untouched,
    missing from response -> untouched, and lifecycle_newly_gone counts
    only the confirmed-withdrawn one.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    for ext_id in ("w-1", "l-1", "m-1"):
        db.touch_listing_lifecycle(conn, "portals", ext_id, "col-a", "Model", "Backdrop", 1000, now)

    by_ids_responses = {
        "w-1": {"id": "w-1", "status": "withdrawn", "price": None},
        "l-1": {"id": "l-1", "status": "listed", "price": "40.0"},
        # "m-1" deliberately absent -- simulates a missing lot.
    }
    poller, client = _build_lifecycle_poller(conn, by_ids_responses)
    poller._run_lifecycle_check_batch()

    assert poller.stats["lifecycle_newly_gone"] == 1
    assert poller.stats["lifecycle_not_returned"] == 1  # "m-1"
    rows = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT listing_external_id, disappeared_at, final_status FROM listing_lifecycle")
    }
    assert rows["w-1"][0] is not None and rows["w-1"][1] == "withdrawn"
    assert rows["l-1"][0] is None
    assert rows["m-1"][0] is None  # missing from response -- never marked disappeared


def test_poller_lifecycle_check_batch_size_respects_config(monkeypatch):
    monkeypatch.setattr(config, "LIFECYCLE_BATCH_SIZE", 2)
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    for i in range(5):
        db.touch_listing_lifecycle(conn, "portals", f"item-{i}", "col-a", "Model", "Backdrop", 1000, now)

    poller, client = _build_lifecycle_poller(conn, by_ids_responses={})
    poller._run_lifecycle_check_batch()

    assert len(client.by_ids_calls[0]) == 2


def test_poller_lifecycle_check_passes_explicit_limit_equal_to_batch_size():
    """Правка 2: the batch call must pass limit= equal to the number of
    ids requested, not rely on the server's default page size.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    for i in range(5):
        db.touch_listing_lifecycle(conn, "portals", f"lim-{i}", "col-a", "Model", "Backdrop", 1000, now)

    poller, client = _build_lifecycle_poller(conn, by_ids_responses={})
    poller._run_lifecycle_check_batch()

    assert client.by_ids_limits == [5]


def test_poller_lifecycle_check_not_returned_counter_only_counts_missing():
    """Правка 3: lifecycle_not_returned counts exactly the ids the API
    silently dropped -- not the ids that came back "listed" or
    "withdrawn".
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    for ext_id in ("present-1", "present-2", "dropped-1", "dropped-2"):
        db.touch_listing_lifecycle(conn, "portals", ext_id, "col-a", "Model", "Backdrop", 1000, now)

    by_ids_responses = {
        "present-1": {"id": "present-1", "status": "listed", "price": "10.0"},
        "present-2": {"id": "present-2", "status": "withdrawn", "price": None},
        # "dropped-1"/"dropped-2" deliberately absent.
    }
    poller, client = _build_lifecycle_poller(conn, by_ids_responses)
    poller._run_lifecycle_check_batch()

    assert poller.stats["lifecycle_not_returned"] == 2
