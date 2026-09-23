from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gift_sniper import config, db
from gift_sniper.mrkt_client import MrktError, MrktFloor
from gift_sniper.mrkt_poller import MrktPoller


def _event(event_type, event_id, amount, gift, date="2026-09-12T07:13:33Z"):
    return {"type": event_type, "id": event_id, "amount": amount, "date": date, "gift": gift}


def _gift(gift_id="uuid-1", number=1, sale_price_nano=None, **overrides):
    base = {
        "id": gift_id, "name": f"SnoopCigar-{number}", "number": number,
        "collectionName": "Snoop Cigar", "modelName": "Classic", "backdropName": "Black",
        "symbolName": "Star", "salePrice": sale_price_nano, "salePriceWithoutFee": 0,
        "isOnSale": True, "isOnAuction": False, "isLocked": False, "isLockedForSale": False,
        "salesCount": 0, "premarketStatus": "None",
        "floorPriceNanoTONsByCollection": None, "floorPriceNanoTONsByBackdropModel": None,
    }
    base.update(overrides)
    return base


class FakeMrktClient:
    """Pages queued as a list of (items, cursor) tuples, consumed in
    order by feed(). `pair_floor_response`/`pair_floor_raises` control
    the at-drop floor query. `find_by_number_response` controls the
    pre-send freshness check.
    """

    def __init__(self, pages=None, pair_floor_response=None, pair_floor_raises=None, find_by_number_response=None):
        self._pages = list(pages) if pages is not None else []
        self._pair_floor_response = pair_floor_response
        self._pair_floor_raises = pair_floor_raises
        self._find_by_number_response = find_by_number_response
        self.feed_calls: list[dict] = []
        self.pair_floor_calls: list[dict] = []
        self.find_by_number_calls: list[dict] = []

    def feed(self, count=20, cursor=""):
        self.feed_calls.append({"count": count, "cursor": cursor})
        if not self._pages:
            return [], ""
        return self._pages.pop(0)

    def pair_floor(self, collection_name=None, model_name=None, backdrop_name=None, exclude_number=None):
        self.pair_floor_calls.append({
            "collection_name": collection_name, "model_name": model_name,
            "backdrop_name": backdrop_name, "exclude_number": exclude_number,
        })
        if self._pair_floor_raises is not None:
            raise self._pair_floor_raises
        if self._pair_floor_response is not None:
            return self._pair_floor_response
        return MrktFloor(floor_nano=None, listed_count=0, status="no_data", raw=[])

    def find_by_number(self, collection_name, number):
        self.find_by_number_calls.append({"collection_name": collection_name, "number": number})
        return self._find_by_number_response


def _conn():
    return db.connect(":memory:")


# --- item 2: listing event ------------------------------------------------


def test_listing_event_writes_listing_with_mrkt_marketplace_and_tg_id_as_is():
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    event = _event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)
    client = FakeMrktClient(pages=[([event], "")])
    poller = MrktPoller(conn, mrkt_client=client)

    poller.poll_once()

    row = conn.execute(
        "SELECT marketplace, external_id, tg_id, price_nano FROM listings"
    ).fetchone()
    assert tuple(row) == ("mrkt", "uuid-1", "SnoopCigar-1", int(Decimal("20.0") * config.NANO))
    assert poller.stats["new_listings"] == 1


def test_repeated_event_id_is_not_reprocessed():
    """КАК ТЕСТИРОВАТЬ item 6: no duplicates on repeated event id."""
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    event = _event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)
    client = FakeMrktClient(pages=[([event], "")])
    poller = MrktPoller(conn, mrkt_client=client)
    poller.poll_once()

    client2 = FakeMrktClient(pages=[([event], "")])
    poller2 = MrktPoller(conn, mrkt_client=client2)
    poller2.poll_once()

    count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    assert count == 1
    assert poller2.stats["items_already_known"] == 1
    assert poller2.stats["new_listings"] == 0


def test_pagination_stops_at_known_event_id():
    conn = _conn()
    gift1 = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    known_event = _event("listing", "evt-known", int(Decimal("20.0") * config.NANO), gift1)
    db.mark_event_processed(conn, "mrkt", "evt-known", datetime.now(timezone.utc))

    gift2 = _gift(gift_id="uuid-2", number=2, sale_price_nano=int(Decimal("30.0") * config.NANO))
    new_event = _event("listing", "evt-new", int(Decimal("30.0") * config.NANO), gift2)

    # Page 1: [new_event, known_event] -- must process new_event, then
    # stop AT known_event without fetching a second page.
    client = FakeMrktClient(pages=[([new_event, known_event], "cursor-2"), ([_event("listing", "evt-should-not-reach", 1, _gift())], "")])
    poller = MrktPoller(conn, mrkt_client=client)
    poller.poll_once()

    assert len(client.feed_calls) == 1  # never fetched page 2
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE external_id='uuid-2'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE external_id='should-not-reach'").fetchone()[0] == 0


def test_max_pages_per_iteration_respected(monkeypatch):
    monkeypatch.setattr(config, "MRKT_MAX_PAGES_PER_ITERATION", 2)
    conn = _conn()

    def _page(n):
        gift = _gift(gift_id=f"uuid-{n}", number=n, sale_price_nano=int(Decimal("20.0") * config.NANO))
        return ([_event("listing", f"evt-{n}", int(Decimal("20.0") * config.NANO), gift)], f"cursor-{n}")

    client = FakeMrktClient(pages=[_page(1), _page(2), _page(3)])
    poller = MrktPoller(conn, mrkt_client=client)
    poller.poll_once()

    assert len(client.feed_calls) == 2  # stopped at MRKT_MAX_PAGES_PER_ITERATION


# --- item 8: MRKT_COLLECT_MIN_PRICE filter --------------------------------


def test_listing_below_collect_min_price_not_written(monkeypatch):
    monkeypatch.setattr(config, "MRKT_COLLECT_MIN_PRICE_NANO", int(Decimal("15") * config.NANO))
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("5.0") * config.NANO))
    event = _event("listing", "evt-1", int(Decimal("5.0") * config.NANO), gift)
    client = FakeMrktClient(pages=[([event], "")])
    poller = MrktPoller(conn, mrkt_client=client)

    poller.poll_once()

    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert poller.stats["collect_filtered_count"] == 1


# --- item 3/4: change_price event ------------------------------------------


def test_change_price_for_known_listing_writes_price_history_with_old_price_from_db():
    """КАК ТЕСТИРОВАТЬ item 3."""
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    listing_event = _event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)
    client = FakeMrktClient(pages=[([listing_event], "")])
    MrktPoller(conn, mrkt_client=client).poll_once()

    gift2 = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("15.0") * config.NANO))
    change_event = _event("change_price", "evt-2", int(Decimal("15.0") * config.NANO), gift2)
    client2 = FakeMrktClient(pages=[([change_event], "")])
    poller2 = MrktPoller(conn, mrkt_client=client2)
    poller2.poll_once()

    row = conn.execute(
        "SELECT marketplace, old_price_nano, new_price_nano FROM price_history WHERE listing_external_id='uuid-1'"
    ).fetchone()
    assert tuple(row) == ("mrkt", int(Decimal("20.0") * config.NANO), int(Decimal("15.0") * config.NANO))
    assert poller2.stats["price_drops"] == 1


def test_change_price_for_unknown_listing_creates_it_without_price_history():
    """КАК ТЕСТИРОВАТЬ item 4."""
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    change_event = _event("change_price", "evt-1", int(Decimal("20.0") * config.NANO), gift)
    client = FakeMrktClient(pages=[([change_event], "")])
    poller = MrktPoller(conn, mrkt_client=client)

    poller.poll_once()

    assert conn.execute("SELECT COUNT(*) FROM listings WHERE external_id='uuid-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    assert poller.stats["new_listings"] == 1


def test_significant_drop_refreshes_pair_floor_snapshot_and_fills_floor_at_drop():
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    listing_event = _event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)
    MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([listing_event], "")])).poll_once()

    gift2 = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("10.0") * config.NANO))
    change_event = _event("change_price", "evt-2", int(Decimal("10.0") * config.NANO), gift2)
    floor = MrktFloor(floor_nano=int(Decimal("13.0") * config.NANO), listed_count=5, status="ok", raw=[])
    client2 = FakeMrktClient(pages=[([change_event], "")], pair_floor_response=floor)
    poller2 = MrktPoller(conn, mrkt_client=client2)
    poller2.poll_once()

    assert client2.pair_floor_calls[0]["collection_name"] == "Snoop Cigar"
    assert client2.pair_floor_calls[0]["exclude_number"] == 1

    row = conn.execute(
        "SELECT floor_at_drop_nano, floor_listed_count_at_drop, floor_level_at_drop "
        "FROM price_history WHERE listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] == int(Decimal("13.0") * config.NANO)
    assert row[1] == 5
    assert row[2] == "pair"

    snap = conn.execute(
        "SELECT pair_floor_excl_self_nano, pair_listed_count_excl_self, pair_floor_status "
        "FROM floor_snapshots WHERE listing_external_id='uuid-1' AND marketplace='mrkt'"
    ).fetchone()
    assert tuple(snap) == (int(Decimal("13.0") * config.NANO), 5, "ok")


def test_thin_pair_book_below_threshold_gets_thin_status(monkeypatch):
    monkeypatch.setattr(config, "MRKT_FLOOR_MIN_LISTED_COUNT", 3)
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([_event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)], "")])).poll_once()

    gift2 = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("10.0") * config.NANO))
    change_event = _event("change_price", "evt-2", int(Decimal("10.0") * config.NANO), gift2)
    floor = MrktFloor(floor_nano=int(Decimal("13.0") * config.NANO), listed_count=2, status="ok", raw=[])
    client2 = FakeMrktClient(pages=[([change_event], "")], pair_floor_response=floor)
    MrktPoller(conn, mrkt_client=client2).poll_once()

    row = conn.execute(
        "SELECT floor_at_drop_nano, pair_floor_status FROM price_history ph "
        "JOIN floor_snapshots fs ON fs.listing_external_id = ph.listing_external_id AND fs.marketplace = 'mrkt' "
        "WHERE ph.listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "thin_pair_book"


def test_floor_query_error_still_records_drop_with_null_floor():
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([_event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)], "")])).poll_once()

    gift2 = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("10.0") * config.NANO))
    change_event = _event("change_price", "evt-2", int(Decimal("10.0") * config.NANO), gift2)
    client2 = FakeMrktClient(pages=[([change_event], "")], pair_floor_raises=MrktError("boom"))
    poller2 = MrktPoller(conn, mrkt_client=client2)
    poller2.poll_once()  # must not raise

    row = conn.execute(
        "SELECT new_price_nano, floor_at_drop_nano FROM price_history WHERE listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] == int(Decimal("10.0") * config.NANO)
    assert row[1] is None


# --- item 5: sale event -----------------------------------------------


def test_sale_event_marks_sold_with_price():
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([_event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)], "")])).poll_once()

    sale_gift = _gift(gift_id="uuid-1", number=1)
    sale_event = _event("sale", "evt-2", int(Decimal("18.0") * config.NANO), sale_gift)
    poller2 = MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([sale_event], "")]))
    poller2.poll_once()

    row = conn.execute(
        "SELECT disappeared_at, final_status, sold_price_nano FROM listing_lifecycle "
        "WHERE marketplace='mrkt' AND listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] is not None
    assert row[1] == "sold"
    assert row[2] == int(Decimal("18.0") * config.NANO)
    assert poller2.stats["sales_recorded"] == 1


# --- sale-vs-floor delivery, КАК ТЕСТИРОВАТЬ items 1-4 --------------------


def _seed_and_sell(conn, sale_price_ton, pair_floor_response=None, pair_floor_raises=None, number=1, gift_id="uuid-1"):
    gift = _gift(gift_id=gift_id, number=number, sale_price_nano=int(Decimal("20.0") * config.NANO))
    MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([_event("listing", f"evt-list-{gift_id}", int(Decimal("20.0") * config.NANO), gift)], "")])).poll_once()

    sale_gift = _gift(gift_id=gift_id, number=number)
    sale_event = _event("sale", f"evt-sale-{gift_id}", int(Decimal(str(sale_price_ton)) * config.NANO), sale_gift)
    fake_client = FakeMrktClient(
        pages=[([sale_event], "")], pair_floor_response=pair_floor_response, pair_floor_raises=pair_floor_raises,
    )
    poller = MrktPoller(conn, mrkt_client=fake_client)
    poller.poll_once()
    return poller, fake_client


def test_item1_sale_above_threshold_queries_floor_and_fills_fields():
    conn = _conn()
    poller, fake_client = _seed_and_sell(
        conn, "50.0",
        pair_floor_response=MrktFloor(floor_nano=int(Decimal("60.0") * config.NANO), listed_count=4, status="ok", raw=[]),
    )

    assert len(fake_client.pair_floor_calls) == 1
    row = conn.execute(
        "SELECT final_status, sold_price_nano, floor_at_sale_nano, floor_listed_count_at_sale, floor_fetched_at_sale "
        "FROM listing_lifecycle WHERE marketplace='mrkt' AND listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] == "sold"
    assert row[1] == int(Decimal("50.0") * config.NANO)
    assert row[2] == int(Decimal("60.0") * config.NANO)
    assert row[3] == 4
    assert row[4] is not None
    assert poller.stats["sale_floor_recorded"] == 1


def test_item2_sale_below_threshold_never_queries_floor():
    conn = _conn()

    class RaisingMrktClient(FakeMrktClient):
        def pair_floor(self, **kwargs):
            raise AssertionError("must not query the floor for a below-threshold sale")

    gift = _gift(gift_id="uuid-2", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    MrktPoller(conn, mrkt_client=RaisingMrktClient(pages=[([_event("listing", "evt-list-2", int(Decimal("20.0") * config.NANO), gift)], "")])).poll_once()

    sale_gift = _gift(gift_id="uuid-2", number=1)
    sale_event = _event("sale", "evt-sale-2", int(Decimal("5.0") * config.NANO), sale_gift)  # below MRKT_COLLECT_MIN_PRICE=15
    poller = MrktPoller(conn, mrkt_client=RaisingMrktClient(pages=[([sale_event], "")]))
    poller.poll_once()  # must not raise -- RaisingMrktClient would if pair_floor were called

    row = conn.execute(
        "SELECT final_status, sold_price_nano, floor_at_sale_nano FROM listing_lifecycle "
        "WHERE marketplace='mrkt' AND listing_external_id='uuid-2'"
    ).fetchone()
    assert row[0] == "sold"
    assert row[1] == int(Decimal("5.0") * config.NANO)
    assert row[2] is None
    assert poller.stats["sales_recorded"] == 1
    assert poller.stats["sale_floor_recorded"] == 0


def test_item3_floor_query_error_leaves_fields_null_sale_still_recorded():
    conn = _conn()
    poller, _fake_client = _seed_and_sell(conn, "50.0", pair_floor_raises=MrktError("boom"))

    row = conn.execute(
        "SELECT final_status, sold_price_nano, floor_at_sale_nano FROM listing_lifecycle "
        "WHERE marketplace='mrkt' AND listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] == "sold"
    assert row[1] == int(Decimal("50.0") * config.NANO)
    assert row[2] is None
    assert poller.stats["sale_floor_recorded"] == 0
    assert poller.stats["mrkt_error_count"] + poller.stats["mrkt_403_count"] + poller.stats["mrkt_429_count"] + poller.stats["mrkt_5xx_count"] == 1


def test_item4_floor_consists_only_of_the_sold_lot_no_data_after_self_exclusion():
    conn = _conn()
    poller, _fake_client = _seed_and_sell(
        conn, "50.0",
        pair_floor_response=MrktFloor(floor_nano=None, listed_count=0, status="no_data", raw=[]),
    )

    row = conn.execute(
        "SELECT final_status, sold_price_nano, floor_at_sale_nano, floor_listed_count_at_sale FROM listing_lifecycle "
        "WHERE marketplace='mrkt' AND listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] == "sold"
    assert row[1] == int(Decimal("50.0") * config.NANO)
    assert row[2] is None
    assert row[3] is None
    assert poller.stats["sale_floor_recorded"] == 0


# --- item 9: cross-check wiring (Portals + Tonnel neighbours) -------------


def test_mrkt_signal_cross_checked_against_portals_and_tonnel(monkeypatch):
    from gift_sniper.tonnel_client import TonnelFloor

    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 1)
    monkeypatch.setattr(config, "MRKT_FLOOR_MIN_LISTED_COUNT", 1)

    class FakeTelegramSession:
        def __init__(self):
            self.calls = []

        def post(self, url, data=None, timeout=None):
            self.calls.append((url, data))
            class R:
                status_code = 200
                text = "{}"
                def json(self):
                    return {"ok": True, "result": {}}
            return R()

    class FakeTonnelClient:
        def pair_floor(self, gift_name=None, model=None, backdrop=None, exclude_gift_num=None):
            return TonnelFloor(floor_nano=int(Decimal("30.0") * config.NANO), floor_with_fee_nano=int(Decimal("33.0") * config.NANO), listed_count=5, status="ok", raw=[])

    class FakePortalsClient:
        def search_pair_floor(self, collection_id, model_name, backdrop_name, limit=20, offset=0):
            return {"results": [{"id": "p-1", "status": "listed", "price": "28.00"}]}

    conn = _conn()
    db.get_portals_collection_id_by_name  # sanity import touch
    from .test_price_drops_report import _listing, _snapshot
    anchor = _listing("portals-anchor", int(Decimal("1.0") * config.NANO))
    anchor.collection_name = "Snoop Cigar"
    anchor.collection_id = "col-1"
    db.upsert_listing_with_floor(conn, anchor, _snapshot(anchor, int(Decimal("1.0") * config.NANO)))

    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([_event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)], "")])).poll_once()

    gift2 = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("15.0") * config.NANO))
    change_event = _event("change_price", "evt-2", int(Decimal("15.0") * config.NANO), gift2)
    from gift_sniper.notifier import TelegramNotifier
    session = FakeTelegramSession()
    notifier = TelegramNotifier("tok", "OWNER", session=session)
    # MRKT's OWN pair floor (mrkt_poller's at-drop query) must be "ok"
    # with enough listings for the signal to even form -- 30 at depth 5
    # (realization rate 0.95) still leaves real profit over 15.
    client2 = FakeMrktClient(
        pages=[([change_event], "")],
        pair_floor_response=MrktFloor(floor_nano=int(Decimal("30.0") * config.NANO), listed_count=5, status="ok", raw=[]),
        find_by_number_response={"isOnSale": True, "salePrice": int(Decimal("15.0") * config.NANO)},
    )
    poller2 = MrktPoller(
        conn, mrkt_client=client2, notifier=notifier,
        portals_client=FakePortalsClient(), tonnel_client=FakeTonnelClient(),
    )
    poller2.poll_once()
    poller2._maybe_notify()

    assert poller2.stats["sent_neighbour_higher"] >= 1 or poller2.stats["signals_sent"] == 1


# --- missing event types fix -----------------------------------------


def test_unlisting_event_sets_final_status_unlisted_not_sold():
    """КАК ТЕСТИРОВАТЬ item 1: confirmed live example -- PrettyPosy-132026,
    salePrice=6426000000, isOnSale=false.
    """
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=132026, sale_price_nano=int(Decimal("20.0") * config.NANO))
    MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([_event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)], "")])).poll_once()

    unlist_gift = _gift(gift_id="uuid-1", number=132026, sale_price_nano=6426000000, isOnSale=False)
    unlist_event = _event("unlisting", "evt-2", 6426000000, unlist_gift)
    poller2 = MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([unlist_event], "")]))
    poller2.poll_once()

    row = conn.execute(
        "SELECT disappeared_at, final_status, sold_price_nano FROM listing_lifecycle "
        "WHERE marketplace='mrkt' AND listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] is not None
    assert row[1] == "unlisted"
    assert row[2] is None  # sold_price_nano must NOT be filled for an unlisting
    assert poller2.stats["unlistings_recorded"] == 1


def test_return_event_sets_final_status_returned():
    """КАК ТЕСТИРОВАТЬ item 2."""
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([_event("listing", "evt-1", int(Decimal("20.0") * config.NANO), gift)], "")])).poll_once()

    return_gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=0, isOnSale=False)
    return_event = _event("return", "evt-2", 0, return_gift)
    poller2 = MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([return_event], "")]))
    poller2.poll_once()

    row = conn.execute(
        "SELECT final_status FROM listing_lifecycle WHERE marketplace='mrkt' AND listing_external_id='uuid-1'"
    ).fetchone()
    assert row[0] == "returned"
    assert poller2.stats["returns_recorded"] == 1


@pytest.mark.parametrize("event_type", ["lucky_buy", "plinko_win", "crafting"])
def test_game_events_skipped_not_written_to_listings_not_unknown(event_type):
    """КАК ТЕСТИРОВАТЬ item 3: counted in events_game_skipped, never
    unknown_event_type_count; crafting's isOnSale=true gift is still
    never written to `listings`.
    """
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO), isOnSale=True)
    event = _event(event_type, "evt-1", int(Decimal("20.0") * config.NANO), gift)
    poller = MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([event], "")]))

    poller.poll_once()

    assert poller.stats["events_game_skipped"] == 1
    assert poller.stats["unknown_event_type_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_unknown_event_type_logs_full_event(caplog):
    """КАК ТЕСТИРОВАТЬ item 4."""
    conn = _conn()
    gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("20.0") * config.NANO))
    event = _event("foobar", "evt-1", int(Decimal("20.0") * config.NANO), gift)
    poller = MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([event], "")]))

    with caplog.at_level("WARNING"):
        poller.poll_once()

    assert poller.stats["unknown_event_type_count"] == 1
    assert any("foobar" in r.message and "uuid-1" in r.message for r in caplog.records)


def test_unlisted_gift_excluded_from_floor_query_via_client_filter():
    """КАК ТЕСТИРОВАТЬ item 5: mrkt_client.pair_floor()'s own isOnSale
    filter (already unit-tested in test_mrkt_client.py) is what actually
    keeps an unlisted lot out of the floor computation -- confirmed here
    end-to-end: a floor query response that STILL includes the unlisted
    gift (isOnSale=false, as the raw API might briefly show) is excluded
    from the computed floor.
    """
    from gift_sniper.mrkt_client import MrktClient

    class FakeSession:
        def __init__(self, response):
            self._response = response

        def post(self, url, json=None, headers=None, impersonate=None, timeout=None):
            class R:
                status_code = 200
                def json(self_inner):
                    return self._response
                text = "{}"
            return R()

    unlisted_gift = _gift(gift_id="uuid-1", number=1, sale_price_nano=int(Decimal("5.0") * config.NANO), isOnSale=False)
    usable_gift = _gift(gift_id="uuid-2", number=2, sale_price_nano=int(Decimal("30.0") * config.NANO), isOnSale=True)
    session = FakeSession({"gifts": [unlisted_gift, usable_gift], "cursor": "", "total": 2})
    real_client = MrktClient(token_provider=lambda: "tok", session=session, request_delay_ms=0)

    floor = real_client.pair_floor("Snoop Cigar", "Classic", "Black")
    assert floor.floor_nano == int(Decimal("30.0") * config.NANO)  # NOT the unlisted gift's 5.0


# --- mrkt_error_count breakdown (403/429/5xx) -----------------------------


def test_feed_error_classified_by_status_code():
    conn = _conn()

    class RaisingFeedClient:
        def feed(self, count=20, cursor=""):
            raise MrktError("forbidden", status_code=403)

    poller = MrktPoller(conn, mrkt_client=RaisingFeedClient())
    poller.poll_once()

    assert poller.stats["mrkt_403_count"] == 1
    assert poller.stats["mrkt_error_count"] == 0


def test_feed_5xx_error_counted_separately():
    conn = _conn()

    class RaisingFeedClient:
        def feed(self, count=20, cursor=""):
            raise MrktError("bad gateway", status_code=502)

    poller = MrktPoller(conn, mrkt_client=RaisingFeedClient())
    poller.poll_once()

    assert poller.stats["mrkt_5xx_count"] == 1
    assert poller.stats["mrkt_403_count"] == 0


def test_feed_error_with_no_status_code_falls_back_to_generic_bucket():
    conn = _conn()

    class RaisingFeedClient:
        def feed(self, count=20, cursor=""):
            raise MrktError("connection reset")

    poller = MrktPoller(conn, mrkt_client=RaisingFeedClient())
    poller.poll_once()

    assert poller.stats["mrkt_error_count"] == 1
    assert poller.stats["mrkt_403_count"] == 0
    assert poller.stats["mrkt_5xx_count"] == 0


def test_premarket_events_skipped_separately_without_warning(caplog):
    """Live 2026-09-20: 50 premarket_listing + 2 premarket_sale in 12 h were
    counted as unknown types and logged as warnings. They are known and
    deliberately skipped: a premarket lot can never be part of a floor."""
    conn = _conn()
    poller = MrktPoller(conn, mrkt_client=FakeMrktClient(pages=[([], "")]))
    with caplog.at_level("WARNING", logger="gift_sniper.mrkt_poller"):
        poller._process_event({"type": "premarket_listing", "id": "e1", "gift": {"name": "X-1"}, "amount": 1})
        poller._process_event({"type": "premarket_sale", "id": "e2", "gift": {"name": "X-1"}, "amount": 1})
    assert poller.stats["events_premarket_skipped"] == 2
    assert poller.stats["unknown_event_type_count"] == 0
    assert "premarket" not in caplog.text
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_genuinely_unknown_event_still_warns():
    poller = MrktPoller(_conn(), mrkt_client=FakeMrktClient(pages=[([], "")]))
    poller._process_event({"type": "brand_new_type", "id": "e3", "gift": {}, "amount": 1})
    assert poller.stats["unknown_event_type_count"] == 1
