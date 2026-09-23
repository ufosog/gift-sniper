from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gift_sniper import config, db
from gift_sniper.notifier import TelegramNotifier
from gift_sniper.tonnel_client import TonnelError
from gift_sniper.tonnel_poller import TonnelPoller


def _gift(gift_num, gift_id=None, price=10.0, status="forsale", underLoan=False,
          premarketData=None, auction=None, dutchAuctionData=None,
          name="Ice Cream", model="Emperor (5%)", backdrop="Black (10%)"):
    return {
        "gift_num": gift_num,
        "gift_id": gift_id if gift_id is not None else 1000 + gift_num,
        "name": name,
        "model": model,
        "backdrop": backdrop,
        "symbol": "Star (2%)",
        "price": price,
        "status": status,
        "asset": "TON",
        "underLoan": underLoan,
        "premarketData": premarketData,
        "auction": auction,
        "dutchAuctionData": dutchAuctionData,
        "export_at": 123456,
    }


class FakeTonnelClient:
    """Pages queued as {page_number: [items]}; search_minimal_by_gift_ids
    via a separate injectable response/error queue.

    `gift_id_errors`: a list of exceptions (or None for "succeed") consumed
    IN ORDER, one per search_minimal_by_gift_ids() call -- once exhausted,
    calls fall through to the default (look the gift_id up in `pages`).
    Lets a test express "first N calls fail, then it works" precisely.
    `gift_id_response`: if given, overrides the default lookup-in-pages
    behavior for every call that isn't consuming an error from the queue.
    """

    def __init__(self, pages=None, gift_id_response=None, gift_id_errors=None, model_floor=None,
                 pair_floor_raises=False):
        self._pages = pages or {}
        self._gift_id_response = gift_id_response
        self._gift_id_errors = list(gift_id_errors) if gift_id_errors is not None else []
        self._model_floor = model_floor
        # КАК ТЕСТИРОВАТЬ item 5: the Tonnel pair floor is never queried
        # at all anymore -- set True to make pair_floor() raise if a test
        # accidentally still calls it.
        self._pair_floor_raises = pair_floor_raises
        self.search_calls: list[dict] = []
        self.search_minimal_by_gift_ids_calls: list[list[int]] = []
        self.model_floor_calls: list[dict] = []
        self.pair_floor_calls: list[dict] = []

    def search(self, gift_name=None, model=None, backdrop=None, gift_num=None, min_price=None, limit=30, page=1, sort=None):
        self.search_calls.append({"page": page, "sort": sort, "gift_num": gift_num, "min_price": min_price})
        return self._pages.get(page, [])

    def search_minimal_by_gift_ids(self, gift_ids, limit):
        self.search_minimal_by_gift_ids_calls.append(list(gift_ids))
        if self._gift_id_errors:
            exc = self._gift_id_errors.pop(0)
            if exc is not None:
                raise exc
        if self._gift_id_response is not None:
            return self._gift_id_response
        found = []
        for items in self._pages.values():
            for item in items:
                if item["gift_id"] in gift_ids:
                    found.append(item)
        return found

    def model_floor(self, gift_name=None, model=None, exclude_gift_num=None):
        self.model_floor_calls.append(
            {"gift_name": gift_name, "model": model, "exclude_gift_num": exclude_gift_num}
        )
        if self._model_floor is not None:
            return self._model_floor
        from gift_sniper.tonnel_client import TonnelFloor
        return TonnelFloor(floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=[])

    def pair_floor(self, gift_name=None, model=None, backdrop=None, exclude_gift_num=None):
        self.pair_floor_calls.append(
            {"gift_name": gift_name, "model": model, "backdrop": backdrop, "exclude_gift_num": exclude_gift_num}
        )
        if self._pair_floor_raises:
            raise AssertionError("pair_floor() must never be called by TonnelPoller (Правка 1: retired)")
        from gift_sniper.tonnel_client import TonnelFloor
        return TonnelFloor(floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=[])


def _conn():
    return db.connect(":memory:")


# --- collection: new listings, filters ----------------------------------


def test_poll_once_collects_new_listings():
    """КАК ТЕСТИРОВАТЬ item 4-adjacent: a fresh gift_id is written."""
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, price=20.0)]})
    poller = TonnelPoller(conn, tonnel_client=client)

    result = poller.poll_once()

    assert len(result) == 1
    assert poller.stats["new_listings"] == 1
    row = conn.execute("SELECT marketplace, external_id, price_nano FROM listings").fetchone()
    assert row[0] == "tonnel"
    assert row[1] == str(1001)
    assert row[2] == int(Decimal("20.0") * config.NANO)


def test_poll_once_uses_freshness_sort():
    conn = _conn()
    client = FakeTonnelClient(pages={1: []})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    assert client.search_calls[0]["sort"] == {"message_post_time": -1}


def test_underloan_item_never_written():
    """КАК ТЕСТИРОВАТЬ item 2: underLoan=true -> not in listings."""
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, underLoan=True)]})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert poller.stats["items_not_purchasable"] == 1


def test_bundle_negative_gift_id_never_written():
    """КАК ТЕСТИРОВАТЬ item 4: gift_id < 0 (a bundle, per Tonnel's own
    documented convention -- price is for the whole set) never enters
    listings, and increments items_bundles_skipped.
    """
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, gift_id=-555, price=20.0)]})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert poller.stats["items_bundles_skipped"] == 1


def test_premarket_item_never_written():
    """КАК ТЕСТИРОВАТЬ item 3: non-empty premarketData -> not in listings."""
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, premarketData={"x": 1})]})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_auction_item_never_written():
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, auction={"end": 1})]})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_dedup_same_gift_id_twice_yields_one_row():
    """КАК ТЕСТИРОВАТЬ item 4: same gift_id twice -> one record."""
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, price=20.0)]})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    client2 = FakeTonnelClient(pages={1: [_gift(1, price=20.0)]})
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1


def test_below_min_price_never_written(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 3: even if a mock returns a lot below the
    threshold (as if the server-side filter somehow let it through), our
    own-side check still drops it and increments collect_filtered_count
    -- the defensive backstop stays.
    """
    monkeypatch.setattr(config, "TONNEL_COLLECT_MIN_PRICE_NANO", int(Decimal("15") * config.NANO))
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, price=5.0)]})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert poller.stats["collect_filtered_count"] == 1


def test_search_page_passes_min_price_to_tonnel(monkeypatch):
    """ДОПОЛНЕНИЕ, Правка 1: TONNEL_COLLECT_MIN_PRICE is sent to Tonnel
    on every feed request -- the whole point of moving the filter
    server-side.
    """
    monkeypatch.setattr(config, "TONNEL_COLLECT_MIN_PRICE", Decimal("15"))
    monkeypatch.setattr(config, "TONNEL_COLLECT_MIN_PRICE_NANO", int(Decimal("15") * config.NANO))
    conn = _conn()
    client = FakeTonnelClient(pages={1: []})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    assert client.search_calls[0]["min_price"] == Decimal("15")


def test_search_page_passes_no_min_price_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "TONNEL_COLLECT_MIN_PRICE_NANO", 0)
    conn = _conn()
    client = FakeTonnelClient(pages={1: []})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    assert client.search_calls[0]["min_price"] is None


# --- known items: price change tracking ----------------------------------


def test_price_change_on_known_item_writes_price_history():
    """КАК ТЕСТИРОВАТЬ item 5: known lot's price changes -> row in
    price_history with marketplace='tonnel'.
    """
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, price=20.0)]})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    client2 = FakeTonnelClient(pages={1: [_gift(1, price=15.0)]})
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    row = conn.execute(
        "SELECT marketplace, old_price_nano, new_price_nano FROM price_history WHERE listing_external_id = '1001'"
    ).fetchone()
    assert row == ("tonnel", int(Decimal("20.0") * config.NANO), int(Decimal("15.0") * config.NANO))
    assert poller2.stats["price_drops"] == 1


def test_no_op_price_writes_nothing():
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, price=20.0)]})
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    client2 = FakeTonnelClient(pages={1: [_gift(1, price=20.0)]})
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert count == 0


# --- Portals isolation (КАК ТЕСТИРОВАТЬ item 6) ---------------------------


def test_portals_queries_never_see_tonnel_rows():
    """КАК ТЕСТИРОВАТЬ item 6: existing Portals-scoped queries don't see
    Tonnel rows, on a mixed fixture.
    """
    from gift_sniper.own_floors import own_combo_floor
    from datetime import datetime, timezone

    conn = _conn()

    # A Tonnel listing sharing the EXACT same collection/model/backdrop
    # names as a Portals one would, per spec (names are confirmed to match).
    tonnel_client = FakeTonnelClient(
        pages={1: [_gift(1, price=1.0, name="Ice Cream", model="Emperor (5%)", backdrop="Black (10%)")]}
    )
    TonnelPoller(conn, tonnel_client=tonnel_client).poll_once()

    from .test_price_drops_report import _listing, _snapshot
    listing = _listing("portals-1", int(Decimal("40.0") * config.NANO))
    listing.model_name = "Emperor"
    listing.backdrop_name = "Black"
    listing.collection_name = "Ice Cream"
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("44.0") * config.NANO)))

    now = datetime.now(timezone.utc)
    own = own_combo_floor(conn, "Ice Cream", "Emperor", "Black", now)

    # If Tonnel's 1.0 TON leaked in, floor_nano would be far below 40.
    assert own.floor_nano == int(Decimal("40.0") * config.NANO)
    assert own.sample_size == 1


def test_marketplace_filter_on_listings_table():
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, price=20.0)]})
    TonnelPoller(conn, tonnel_client=client).poll_once()

    from .test_price_drops_report import _listing, _snapshot
    listing = _listing("portals-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("44.0") * config.NANO)))

    portals_count = conn.execute("SELECT COUNT(*) FROM listings WHERE marketplace = 'portals'").fetchone()[0]
    tonnel_count = conn.execute("SELECT COUNT(*) FROM listings WHERE marketplace = 'tonnel'").fetchone()[0]
    assert portals_count == 1
    assert tonnel_count == 1


# --- lifecycle: gift_id $in batch + per-batch fallback --------------------


def test_lifecycle_check_absent_gift_id_is_now_treated_as_disappeared():
    """ДОПОЛНЕНИЕ (lifecycle fix), КАК ТЕСТИРОВАТЬ item 2: a gift_id
    genuinely absent from the minimal-filter response -> disappeared_at
    filled, final_status="gone_unknown" (reason can't be determined,
    unlike Portals).
    """
    conn = _conn()
    client = FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}, gift_id_response=[])
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    poller._run_lifecycle_check_batch()

    assert client.search_minimal_by_gift_ids_calls == [[1001]]
    assert poller.stats["lifecycle_newly_gone"] == 1
    assert poller.stats["lifecycle_not_returned"] == 0  # a confirmed absence, not an uncertain one
    row = conn.execute(
        "SELECT disappeared_at, final_status FROM listing_lifecycle "
        "WHERE marketplace='tonnel' AND listing_external_id='1001'"
    ).fetchone()
    assert row[0] is not None
    assert row[1] == "gone_unknown"


def test_lifecycle_check_keeps_listing_when_gift_id_still_returned():
    """КАК ТЕСТИРОВАТЬ item 1: the mock returns the lot at the minimal
    filter -> disappeared_at stays NULL, last_checked_at updated.
    """
    conn = _conn()
    g = _gift(1, price=20.0)
    client = FakeTonnelClient(pages={1: [g]}, gift_id_response=[g])
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    before = conn.execute(
        "SELECT last_checked_at FROM listing_lifecycle WHERE marketplace='tonnel' AND listing_external_id='1001'"
    ).fetchone()[0]
    assert before is None  # never checked yet

    poller._run_lifecycle_check_batch()

    assert poller.stats["lifecycle_newly_gone"] == 0
    row = conn.execute(
        "SELECT disappeared_at, last_checked_at FROM listing_lifecycle "
        "WHERE marketplace='tonnel' AND listing_external_id='1001'"
    ).fetchone()
    assert row[0] is None
    assert row[1] is not None


def test_lifecycle_check_uses_minimal_filter_only():
    """КАК ТЕСТИРОВАТЬ item 3: the request sent for the lifecycle check
    does not contain buyer, refunded, export_at, or price.$exists.
    """
    conn = _conn()
    g = _gift(1, price=20.0)
    client = FakeTonnelClient(pages={1: [g]}, gift_id_response=[g])
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()
    poller._run_lifecycle_check_batch()
    assert client.search_minimal_by_gift_ids_calls == [[1001]]


def test_lifecycle_check_price_change_recorded_to_history():
    """КАК ТЕСТИРОВАТЬ item 5: the price returned during a lifecycle
    check differs from what's stored -> recorded to price_history, same
    as a feed sighting.
    """
    conn = _conn()
    g_seen = _gift(1, price=20.0)
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [g_seen]}))
    poller.poll_once()

    g_changed = _gift(1, price=15.0)
    client2 = FakeTonnelClient(pages={1: [g_seen]}, gift_id_response=[g_changed])
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2._run_lifecycle_check_batch()

    row = conn.execute(
        "SELECT marketplace, old_price_nano, new_price_nano FROM price_history WHERE listing_external_id='1001'"
    ).fetchone()
    assert tuple(row) == ("tonnel", int(Decimal("20.0") * config.NANO), int(Decimal("15.0") * config.NANO))
    assert poller2.stats["price_drops"] == 1


def test_lifecycle_check_retries_once_before_per_batch_fallback():
    """One retry of the SAME batch call before falling back -- confirmed
    format is {"gift_id": {"$in": [...]}}.
    """
    conn = _conn()
    g = _gift(1, price=20.0)
    client = FakeTonnelClient(pages={1: [g]}, gift_id_errors=[TonnelError("network blip"), None])
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    poller._run_lifecycle_check_batch()

    # Exactly 2 batch calls (initial + 1 retry), both with the full batch
    # -- the retry succeeded, so no per-item fallback was needed.
    assert client.search_minimal_by_gift_ids_calls == [[1001], [1001]]
    assert poller.stats["lifecycle_batch_fallback_count"] == 0


def test_lifecycle_check_falls_back_to_per_item_for_this_batch_only():
    """Both the initial call and its one retry fail -> falls back to
    one search_minimal_by_gift_ids([gift_id], limit=1) call per listing,
    for THIS batch only.
    """
    conn = _conn()
    g = _gift(1, price=20.0)
    client = FakeTonnelClient(
        pages={1: [g]},
        gift_id_errors=[TonnelError("bad filter"), TonnelError("bad filter again")],
    )
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    poller._run_lifecycle_check_batch()

    # 2 failed batch attempts + 1 successful per-item call.
    assert client.search_minimal_by_gift_ids_calls == [[1001], [1001], [1001]]
    assert poller.stats["lifecycle_batch_fallback_count"] == 1
    assert poller.stats["lifecycle_not_returned"] == 0  # found via the per-item fallback
    assert poller.stats["lifecycle_newly_gone"] == 0


def test_lifecycle_check_per_item_fallback_failure_is_uncertain_not_gone():
    """A gift_id whose per-item fallback query itself raises (not a
    confirmed absence) must land in lifecycle_not_returned, never
    lifecycle_newly_gone.
    """
    conn = _conn()
    g = _gift(1, price=20.0)
    client = FakeTonnelClient(
        pages={1: [g]},
        gift_id_errors=[TonnelError("bad filter"), TonnelError("bad filter again"), TonnelError("per-item also fails")],
    )
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    poller._run_lifecycle_check_batch()

    assert poller.stats["lifecycle_not_returned"] == 1
    assert poller.stats["lifecycle_newly_gone"] == 0
    row = conn.execute(
        "SELECT disappeared_at FROM listing_lifecycle WHERE marketplace='tonnel' AND listing_external_id='1001'"
    ).fetchone()
    assert row[0] is None


def test_lifecycle_check_next_batch_retries_batched_call():
    """Per spec: the fallback is per-BATCH, not permanent -- the next
    lifecycle pass must try the batched call again, not stay in
    per-item mode.
    """
    conn = _conn()
    client = FakeTonnelClient(
        pages={1: [_gift(1, price=20.0), _gift(2, price=21.0)]},
        gift_id_errors=[TonnelError("bad filter"), TonnelError("bad filter again")],
    )
    poller = TonnelPoller(conn, tonnel_client=client)
    poller.poll_once()

    poller._run_lifecycle_check_batch()
    calls_after_first = list(client.search_minimal_by_gift_ids_calls)
    assert poller.stats["lifecycle_batch_fallback_count"] == 1

    # Second lifecycle pass: the very next call must be a full-batch
    # attempt again (not a lone per-item call) -- no error queued this
    # time, so it should succeed on the first try.
    poller._run_lifecycle_check_batch()
    new_calls = client.search_minimal_by_gift_ids_calls[len(calls_after_first):]
    assert new_calls[0] == [1001, 1002]  # the batched call, tried again
    assert poller.stats["lifecycle_batch_fallback_count"] == 1  # unchanged -- second pass succeeded


# --- db_locked_count (Правка 4: two-writer SQLite contention) -------------


def test_db_locked_error_counted_and_does_not_crash_run_forever():
    """A 'database is locked' OperationalError from within a poll cycle
    must not take the whole process down -- counted in db_locked_count,
    logged, and the run loop continues to the next cycle.
    """
    import sqlite3

    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: []}), sleep_fn=lambda s: None)

    calls = {"n": 0}
    real_poll_once = poller.poll_once

    def flaky_poll_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_poll_once()

    poller.poll_once = flaky_poll_once
    poller.run_forever(run_seconds=0.01)

    assert poller.stats["db_locked_count"] == 1


def test_other_operational_errors_are_not_mistaken_for_locking():
    """Only 'database is locked' is caught/counted -- any other
    OperationalError must still propagate (not silently absorbed as if
    it were contention)."""
    import sqlite3
    import pytest

    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: []}), sleep_fn=lambda s: None)

    def broken_poll_once():
        raise sqlite3.OperationalError("no such table: bogus")

    poller.poll_once = broken_poll_once
    with pytest.raises(sqlite3.OperationalError):
        poller.run_forever(run_seconds=0.01)
    assert poller.stats["db_locked_count"] == 0


# --- 403/429 classification -----------------------------------------------


def test_403_and_429_counted_separately():
    class RaisingClient:
        def search(self, **kwargs):
            raise TonnelError("forbidden", status_code=403)

    poller = TonnelPoller(_conn(), tonnel_client=RaisingClient())
    poller.poll_once()
    assert poller.stats["tonnel_403_count"] == 1


def test_tonnel_5xx_counted_separately():
    """КАК ТЕСТИРОВАТЬ item 4: a 502 (confirmed live once, Cloudflare's
    HTML page) is counted separately from 403/429 -- distinguishes
    platform-side outages from our own client misbehaving.
    """
    class RaisingClient:
        def search(self, **kwargs):
            raise TonnelError("bad gateway", status_code=502)

    poller = TonnelPoller(_conn(), tonnel_client=RaisingClient())
    poller.poll_once()
    assert poller.stats["tonnel_5xx_count"] == 1
    assert poller.stats["tonnel_403_count"] == 0
    assert poller.stats["tonnel_429_count"] == 0


# --- at-drop pair floor snapshot (Правка 2) --------------------------------


def test_significant_drop_refreshes_floor_snapshot():
    """КАК ТЕСТИРОВАТЬ item 2 (model book deep enough, price below floor
    -> signal-ready snapshot) + item 5 (pair floor never queried)."""
    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}))
    poller.poll_once()

    from gift_sniper.tonnel_client import TonnelFloor
    model_floor = TonnelFloor(floor_nano=int(Decimal("13.0") * config.NANO), floor_with_fee_nano=None, listed_count=5, status="ok", raw=[])
    client2 = FakeTonnelClient(
        pages={1: [_gift(1, price=10.0)]}, model_floor=model_floor, pair_floor_raises=True,
    )  # 50% drop, above threshold, price(10) below model floor(13)
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    assert len(client2.model_floor_calls) == 1
    assert client2.model_floor_calls[0]["gift_name"] == "Ice Cream"
    assert client2.model_floor_calls[0]["model"] == "Emperor"
    assert client2.model_floor_calls[0]["exclude_gift_num"] == 1
    assert client2.pair_floor_calls == []  # item 5: pair floor never queried
    row = conn.execute(
        "SELECT marketplace, model_floor_excl_self_nano, model_listed_count_excl_self, model_floor_status "
        "FROM floor_snapshots WHERE listing_external_id='1001'"
    ).fetchone()
    assert tuple(row) == ("tonnel", int(Decimal("13.0") * config.NANO), 5, "ok")
    assert poller2.stats["price_drops_above_threshold"] == 1


def test_significant_drop_fills_floor_at_drop_in_price_history():
    """КАК ТЕСТИРОВАТЬ item 1 (stale-floor fix): a significant drop fills
    price_history's floor_at_drop_nano/floor_listed_count_at_drop/
    floor_fetched_at/floor_level_at_drop="model" -- NOT just
    floor_snapshots. Confirmed live this was the actual bug: 195
    significant Tonnel drops, floor_at_drop_nano filled on 0.
    """
    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}))
    poller.poll_once()

    from gift_sniper.tonnel_client import TonnelFloor
    model_floor = TonnelFloor(floor_nano=int(Decimal("13.0") * config.NANO), floor_with_fee_nano=None, listed_count=5, status="ok", raw=[])
    client2 = FakeTonnelClient(pages={1: [_gift(1, price=10.0)]}, model_floor=model_floor)
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    row = conn.execute(
        "SELECT floor_at_drop_nano, floor_listed_count_at_drop, floor_fetched_at, floor_level_at_drop "
        "FROM price_history WHERE marketplace='tonnel' AND listing_external_id='1001'"
    ).fetchone()
    assert row[0] == int(Decimal("13.0") * config.NANO)
    assert row[1] == 5
    assert row[2] is not None
    assert row[3] == "model"


def test_thin_model_book_leaves_floor_at_drop_null_in_price_history():
    """A "thin" book (below TONNEL_MODEL_MIN_LISTED_COUNT) is not a
    usable floor -- floor_at_drop_nano stays NULL in price_history too,
    same as floor_snapshots.model_floor_status staying "thin_model_book".
    """
    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}))
    poller.poll_once()

    from gift_sniper.tonnel_client import TonnelFloor
    model_floor = TonnelFloor(floor_nano=int(Decimal("13.0") * config.NANO), floor_with_fee_nano=None, listed_count=4, status="ok", raw=[])
    client2 = FakeTonnelClient(pages={1: [_gift(1, price=10.0)]}, model_floor=model_floor)
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    row = conn.execute(
        "SELECT floor_at_drop_nano, floor_level_at_drop FROM price_history "
        "WHERE marketplace='tonnel' AND listing_external_id='1001'"
    ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_noise_drop_never_queries_floor(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 2: below-threshold drop -> floor is NOT
    queried at all (a mock that raises AssertionError if called)."""
    monkeypatch.setattr(config, "TONNEL_PRICE_DROP_MIN_PCT", Decimal("5.0"))
    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}))
    poller.poll_once()

    class RaisingModelFloorClient(FakeTonnelClient):
        def model_floor(self, **kwargs):
            raise AssertionError("model_floor must not be called for a below-threshold drop")

    client2 = RaisingModelFloorClient(pages={1: [_gift(1, price=19.9)]})  # 0.5% drop -- below 5% threshold
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()  # must not raise

    row = conn.execute(
        "SELECT floor_at_drop_nano FROM price_history WHERE marketplace='tonnel' AND listing_external_id='1001'"
    ).fetchone()
    assert row[0] is None


def test_floor_query_error_still_records_drop_with_null_floor(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 3: the floor query fails -> the price drop is
    STILL recorded, floor_at_drop_nano stays NULL, error logged (not
    silently swallowed, not blocking the write).
    """
    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}))
    poller.poll_once()

    from gift_sniper.tonnel_client import TonnelError

    class RaisingModelFloorClient(FakeTonnelClient):
        def model_floor(self, **kwargs):
            raise TonnelError("simulated network failure")

    client2 = RaisingModelFloorClient(pages={1: [_gift(1, price=10.0)]})
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()  # must not raise

    row = conn.execute(
        "SELECT new_price_nano, floor_at_drop_nano FROM price_history "
        "WHERE marketplace='tonnel' AND listing_external_id='1001'"
    ).fetchone()
    assert row[0] == int(Decimal("10.0") * config.NANO)  # the drop WAS recorded
    assert row[1] is None


def test_thin_model_book_below_threshold_gets_thin_status():
    """КАК ТЕСТИРОВАТЬ item 1: 4 listings in the model book (below the
    default TONNEL_MODEL_MIN_LISTED_COUNT=5) -> status="thin_model_book",
    not "ok" -- signals.py's cascade must not treat this as usable.
    """
    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}))
    poller.poll_once()

    from gift_sniper.tonnel_client import TonnelFloor
    model_floor = TonnelFloor(floor_nano=int(Decimal("13.0") * config.NANO), floor_with_fee_nano=None, listed_count=4, status="ok", raw=[])
    client2 = FakeTonnelClient(pages={1: [_gift(1, price=10.0)]}, model_floor=model_floor)
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    row = conn.execute(
        "SELECT model_listed_count_excl_self, model_floor_status FROM floor_snapshots WHERE listing_external_id='1001'"
    ).fetchone()
    assert tuple(row) == (4, "thin_model_book")


def test_noise_drop_does_not_refresh_floor_snapshot(monkeypatch):
    monkeypatch.setattr(config, "TONNEL_PRICE_DROP_MIN_PCT", Decimal("5.0"))
    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}))
    poller.poll_once()

    client2 = FakeTonnelClient(pages={1: [_gift(1, price=19.9)]})  # 0.5% drop -- below 5% threshold
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    assert client2.model_floor_calls == []
    assert poller2.stats["price_drops_above_threshold"] == 0
    row = conn.execute("SELECT COUNT(*) FROM price_history WHERE is_noise = 1").fetchone()[0]
    assert row == 1


def test_bundle_and_underloan_excluded_from_model_floor_via_client(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 6, integration level: TonnelPoller delegates
    entirely to TonnelClient.model_floor() for filtering (unit-tested in
    test_tonnel_client.py) -- confirms the poller passes through whatever
    the client reports without re-filtering or re-interpreting it.
    """
    conn = _conn()
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(pages={1: [_gift(1, price=20.0)]}))
    poller.poll_once()

    from gift_sniper.tonnel_client import TonnelFloor
    # Client already excluded a bundle/underLoan lot -- listed_count
    # reflects only the 6 genuinely usable listings it found.
    model_floor = TonnelFloor(floor_nano=int(Decimal("13.0") * config.NANO), floor_with_fee_nano=None, listed_count=6, status="ok", raw=[])
    client2 = FakeTonnelClient(pages={1: [_gift(1, price=10.0)]}, model_floor=model_floor)
    poller2 = TonnelPoller(conn, tonnel_client=client2)
    poller2.poll_once()

    row = conn.execute(
        "SELECT model_listed_count_excl_self, model_floor_status FROM floor_snapshots WHERE listing_external_id='1001'"
    ).fetchone()
    assert tuple(row) == (6, "ok")


# --- notifications (Правка 4) ----------------------------------------------


class _FakeTelegramSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data))
        status, body = self._responses.pop(0)
        return _FakeTelegramResponse(status, body)


class _FakeTelegramResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _seed_tonnel_clean_signal(conn, external_id="2001", price="20.0", floor="30.0", gift_num=1):
    from gift_sniper.models import Listing

    now = datetime.now(timezone.utc)
    listing = Listing(
        marketplace="tonnel", external_id=external_id, tg_id=f"IceCream-{gift_num}", collection_id=None,
        collection_name="Ice Cream", gift_number=gift_num, price_nano=int(Decimal(price) * config.NANO),
        currency="TON", collection_floor_nano=None, model_name="Emperor", symbol_name=None, backdrop_name="Black",
        model_rarity_raw=None, symbol_rarity_raw=None, backdrop_rarity_raw=None,
        image_url=None, animation_url=None, listed_at=None, unlocks_at=None,
        status="forsale", first_seen_at=now, raw={},
    )
    db.insert_listing(conn, listing)
    observed_at = now
    db.record_price_change(
        conn, "tonnel", external_id,
        old_price_nano=int(Decimal("40.0") * config.NANO), new_price_nano=int(Decimal(price) * config.NANO),
        delta_pct=Decimal("-50"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
    )
    db.upsert_tonnel_model_floor_snapshot(
        conn, external_id, "Emperor", "Black", int(Decimal(floor) * config.NANO), 4, "ok", now,
    )
    return observed_at


def test_maybe_notify_sends_clean_tonnel_signal_and_marks_alerts_sent(monkeypatch):
    """КАК ТЕСТИРОВАТЬ items 2, 7."""
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = _conn()
    observed_at = _seed_tonnel_clean_signal(conn)

    # The freshness check goes through tonnel_client (search_minimal_by_gift_ids),
    # NOT the Telegram session -- only sendMessage goes to Telegram.
    session = _FakeTelegramSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    fake_client = FakeTonnelClient(
        gift_id_response=[{"gift_num": 1, "gift_id": 2001, "status": "forsale", "price": 20.0}]
    )
    poller = TonnelPoller(conn, tonnel_client=fake_client, notifier=notifier)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    row = conn.execute(
        "SELECT marketplace, status FROM alerts_sent WHERE listing_external_id='2001'"
    ).fetchone()
    assert tuple(row) == ("tonnel", "sent")
    sent_text = session.calls[-1][1]["text"]
    # ДЕФЕКТ 5: no portals_client/mrkt_client configured here -- no
    # neighbour ever queried, cross_verdict stays "not_checked", no
    # checkmark (Правка 2: still no marketplace name either way).
    assert sent_text.startswith("<b>ЛИСТИНГ</b>")


def test_tonnel_notify_levels_gate_is_real_not_just_documented(monkeypatch):
    """ДЕФЕКТ 2 (systemic-check delivery): TONNEL_NOTIFY_LEVELS must be
    an ENFORCED gate, not a comment saying it's fine to skip the check --
    set it to an empty set and confirm a real Tonnel model-level signal
    is suppressed, proving _maybe_notify actually reads it.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("0"))
    monkeypatch.setattr(config, "TONNEL_NOTIFY_LEVELS", set())

    conn = _conn()
    _seed_tonnel_clean_signal(conn)

    session = _FakeTelegramSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    fake_client = FakeTonnelClient(
        gift_id_response=[{"gift_num": 1, "gift_id": 2001, "status": "forsale", "price": 20.0}]
    )
    poller = TonnelPoller(conn, tonnel_client=fake_client, notifier=notifier)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 0


# --- two-way cross-check wiring (Правка 1) ---------------------------


class FakePortalsClientForCrossCheck:
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = []

    def search_pair_floor(self, collection_id, model_name, backdrop_name, limit=20, offset=0):
        self.calls.append({"collection_id": collection_id, "model_name": model_name, "backdrop_name": backdrop_name})
        if self._raises is not None:
            raise self._raises
        return self._response

    def search_model_floor(self, collection_id, model_name, limit=50, offset=0):
        return {"results": []}


def _seed_portals_anchor(conn, collection_name="Ice Cream", collection_id="col-portals-1"):
    from .test_price_drops_report import _listing, _snapshot
    listing = _listing("portals-anchor", int(Decimal("1.0") * config.NANO))
    listing.collection_name = collection_name
    listing.collection_id = collection_id
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("1.0") * config.NANO)))


def _portals_result(*prices):
    return {"results": [{"id": f"p-{i}", "status": "listed", "price": p} for i, p in enumerate(prices)]}


def test_tonnel_signal_sent_when_portals_neighbour_higher(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 6: gap above CROSS_MIN_GAP_PCT -> sent,
    verdict sent_neighbour_higher. 3 listings clears CROSS_MIN_NEIGHBOUR_COUNT.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("0"))
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    conn = _conn()
    _seed_portals_anchor(conn)
    _seed_tonnel_clean_signal(conn, price="17.25", floor="30.0")

    session = _FakeTelegramSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    fake_client = FakeTonnelClient(
        gift_id_response=[{"gift_num": 1, "gift_id": 2001, "status": "forsale", "price": 17.25}]
    )
    fake_portals = FakePortalsClientForCrossCheck(response=_portals_result("21.90", "22.00", "23.00"))
    poller = TonnelPoller(conn, tonnel_client=fake_client, notifier=notifier, portals_client=fake_portals)
    poller._maybe_notify()

    assert len(fake_portals.calls) == 1
    assert poller.stats["sent_neighbour_higher"] == 1
    assert poller.stats["signals_sent"] == 1
    sent_text = session.calls[-1][1]["text"]
    assert sent_text.startswith("<b>ЛИСТИНГ ✓</b>")


def test_tonnel_signal_skipped_when_portals_neighbour_cheaper(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 4: the Portals neighbour is cheaper -> not
    sent, verdict skipped_neighbour_cheaper.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("0"))
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    conn = _conn()
    _seed_portals_anchor(conn)
    # Floor 40 (not 30): realization rate 0.95 at depth 4 leaves no profit
    # over 25.70*1.1 with a 30 floor.
    _seed_tonnel_clean_signal(conn, price="25.70", floor="40.0")

    session = _FakeTelegramSession([])
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    fake_client = FakeTonnelClient(
        gift_id_response=[{"gift_num": 1, "gift_id": 2001, "status": "forsale", "price": 25.70}]
    )
    fake_portals = FakePortalsClientForCrossCheck(response=_portals_result("10.21", "10.50", "11.00"))
    poller = TonnelPoller(conn, tonnel_client=fake_client, notifier=notifier, portals_client=fake_portals)
    poller._maybe_notify()

    assert poller.stats["skipped_neighbour_cheaper"] == 1
    assert poller.stats["signals_sent"] == 0
    assert session.calls == []
    row = conn.execute("SELECT status FROM alerts_sent WHERE listing_external_id='2001'").fetchone()
    assert tuple(row) == ("skipped_cross_worse",)


def test_tonnel_signal_neighbour_thin_below_min_count_still_sends(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 8: only 1 Portals listing, below
    CROSS_MIN_NEIGHBOUR_COUNT=3 -> "neighbour_thin", still sent.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("0"))
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    conn = _conn()
    _seed_portals_anchor(conn)
    _seed_tonnel_clean_signal(conn, price="21.50", floor="30.0")

    session = _FakeTelegramSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    fake_client = FakeTonnelClient(
        gift_id_response=[{"gift_num": 1, "gift_id": 2001, "status": "forsale", "price": 21.50}]
    )
    fake_portals = FakePortalsClientForCrossCheck(response=_portals_result("21.50"))  # equal, but only 1 listing
    poller = TonnelPoller(conn, tonnel_client=fake_client, notifier=notifier, portals_client=fake_portals)
    poller._maybe_notify()

    assert poller.stats["neighbour_thin"] == 1
    assert poller.stats["signals_sent"] == 1


def test_cross_check_disabled_makes_zero_portals_calls(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("0"))
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", False)

    conn = _conn()
    _seed_portals_anchor(conn)
    _seed_tonnel_clean_signal(conn, price="17.25", floor="30.0")

    session = _FakeTelegramSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    fake_client = FakeTonnelClient(
        gift_id_response=[{"gift_num": 1, "gift_id": 2001, "status": "forsale", "price": 17.25}]
    )
    fake_portals = FakePortalsClientForCrossCheck(response=_portals_result("19.90"))
    poller = TonnelPoller(conn, tonnel_client=fake_client, notifier=notifier, portals_client=fake_portals)
    poller._maybe_notify()

    assert fake_portals.calls == []
    assert poller.stats["signals_sent"] == 1
    assert poller.stats["sent_neighbour_higher"] == 0


def test_maybe_notify_stale_lot_not_sent(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 5: sold between detection and send -> not sent."""
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = _conn()
    _seed_tonnel_clean_signal(conn)

    # FakeTonnelClient() with no gift_id_response and empty pages ->
    # search_minimal_by_gift_ids returns [] -- gift_id not found -> gone.
    # No sendMessage is expected, so the Telegram session queues nothing.
    session = _FakeTelegramSession([])
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(), notifier=notifier)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 0
    assert poller.stats["signals_stale"] == 1
    row = conn.execute("SELECT status FROM alerts_sent WHERE listing_external_id='2001'").fetchone()
    assert tuple(row) == ("skipped_stale",)


def test_tonnel_notify_disabled_makes_zero_telegram_calls(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 6: TONNEL_NOTIFY_ENABLED=false (notifier=None
    at construction) -> _maybe_notify is a no-op, zero Telegram calls.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = _conn()
    _seed_tonnel_clean_signal(conn)

    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(), notifier=None)
    poller._maybe_notify()  # must not raise, must not touch any notifier

    assert poller.stats["signals_sent"] == 0
    count = conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0]
    assert count == 0


def test_maybe_notify_respects_cooldown(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "TONNEL_SIGNAL_COOLDOWN_MIN", 60)

    conn = _conn()
    observed_at = _seed_tonnel_clean_signal(conn)
    db.mark_alert_sent(conn, "tonnel", "2001", observed_at, datetime.now(timezone.utc))

    session = _FakeTelegramSession([])
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    poller = TonnelPoller(conn, tonnel_client=FakeTonnelClient(), notifier=notifier)
    poller._maybe_notify()

    # The already-sent signal is filtered out by is_alert_sent() before
    # cooldown is even reached (same observed_at, already marked sent) --
    # confirms no duplicate send and no Telegram call for it.
    assert poller.stats["signals_sent"] == 0
    assert session.calls == []
