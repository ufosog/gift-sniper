from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.pair_floor import PairFloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def _mk_item(ext_id: str, price, listed_at="2026-09-06T14:33:00Z", model="M") -> dict:
    return {
        "id": ext_id,
        "tg_id": f"{ext_id}-tg",
        "collection_id": "col-x",
        "name": "Collection X",
        "external_collection_number": 1,
        "price": price,
        "floor_price": "30.0",
        "photo_url": None,
        "animation_url": None,
        "listed_at": listed_at,
        "unlocks_at": None,
        "status": "listed",
        "attributes": [
            {"type": "model", "value": model, "rarity_per_mille": 1.0},
            {"type": "backdrop", "value": "Copper", "rarity_per_mille": 1.0},
        ],
    }


def _seed_one(price="28.01", listed_at="2026-09-05T19:59:00Z"):
    """Seeds a single known listing via a normal poll_once, returns the
    (conn, poller, client) so a second poll_once can be issued against
    the same listing with a changed price.
    """
    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[[_mk_item("ext-1", price, listed_at)], []])
    auth = AuthManager()
    poller = Poller(conn, client, auth, FloorCache(client), PairFloorCache(client))
    poller.poll_once()
    return conn, poller, client


def test_known_listing_with_lower_price_writes_price_history_and_updates_listing():
    conn, poller, _ = _seed_one(price="28.01", listed_at="2026-09-05T19:59:00Z")

    new_client = FakePortalsClient(pages=[[_mk_item("ext-1", "24.99", "2026-09-06T14:33:00Z")], []])
    poller2 = Poller(conn, new_client, poller.auth, poller.floor_cache, poller.pair_floor_cache)
    poller2.poll_once()

    row = conn.execute(
        "SELECT old_price_nano, new_price_nano, delta_pct FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()
    assert row is not None
    old_price_nano, new_price_nano, delta_pct = row
    assert old_price_nano == int(Decimal("28.01") * config.NANO)
    assert new_price_nano == int(Decimal("24.99") * config.NANO)
    expected_delta = (Decimal("24.99") - Decimal("28.01")) / Decimal("28.01") * 100
    assert abs(Decimal(delta_pct) - expected_delta) < Decimal("0.01")

    current_price = conn.execute("SELECT price_nano FROM listings WHERE external_id='ext-1'").fetchone()[0]
    assert current_price == int(Decimal("24.99") * config.NANO)

    assert poller2.stats["price_changes_seen"] == 1
    assert poller2.stats["price_drops"] == 1


def test_known_listing_with_same_price_writes_nothing_to_price_history():
    conn, poller, _ = _seed_one(price="28.01", listed_at="2026-09-05T19:59:00Z")

    # Same price, but listed_at "touched" -- confirmed live this happens
    # with no real price change; must NOT be treated as a change.
    touch_client = FakePortalsClient(pages=[[_mk_item("ext-1", "28.01", "2026-09-06T14:33:00Z")], []])
    poller2 = Poller(conn, touch_client, poller.auth, poller.floor_cache, poller.pair_floor_cache)
    poller2.poll_once()

    count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert count == 0
    assert poller2.stats["price_changes_seen"] == 0

    # listed_at itself is not asserted as unchanged -- only that no price
    # history row was written for a same-price "touch".


def test_small_drop_below_threshold_is_flagged_noise():
    conn, poller, _ = _seed_one(price="24.99")
    client2 = FakePortalsClient(pages=[[_mk_item("ext-1", "24.95")], []])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, poller.pair_floor_cache)
    poller2.poll_once()

    is_noise, delta_pct = conn.execute(
        "SELECT is_noise, delta_pct FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()
    assert is_noise == 1
    assert abs(Decimal(delta_pct) - Decimal("-0.16")) < Decimal("0.02")


def test_real_drop_above_threshold_is_not_noise():
    conn, poller, _ = _seed_one(price="67.91")
    client2 = FakePortalsClient(pages=[[_mk_item("ext-1", "65.93")], []])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, poller.pair_floor_cache)
    poller2.poll_once()

    is_noise, delta_pct = conn.execute(
        "SELECT is_noise, delta_pct FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()
    assert is_noise == 0
    assert abs(Decimal(delta_pct) - Decimal("-2.92")) < Decimal("0.05")
    assert poller2.stats["price_drops_above_threshold"] == 1


def test_known_listing_with_null_price_is_skipped_without_error():
    conn, poller, _ = _seed_one(price="20.0")

    item = _mk_item("ext-1", None)
    client2 = FakePortalsClient(pages=[[item], []])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, poller.pair_floor_cache)

    poller2.poll_once()  # must not raise

    count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert count == 0
    assert poller2.stats["price_changes_seen"] == 0
    # Original price untouched.
    current_price = conn.execute("SELECT price_nano FROM listings WHERE external_id='ext-1'").fetchone()[0]
    assert current_price == int(Decimal("20.0") * config.NANO)


def test_page_entirely_known_with_price_changes_still_stops_pagination():
    conn, poller, _ = _seed_one(price="20.0")

    # Second page (offset=50) would be unreachable -- if fetched, the
    # test fails via the assertion on search_offsets below.
    client2 = FakePortalsClient(pages=[[_mk_item("ext-1", "18.0")], [_mk_item("ext-99", "5.0")]])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, poller.pair_floor_cache)

    result = poller2.poll_once()

    assert result == []  # no NEW listings
    assert client2.search_offsets == [0]  # pagination stopped after the one (fully-known) page
    # But the price change was still recorded.
    count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert count == 1


def test_is_noise_defect_investigation_016_pct_drop_is_noise(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 1 (is_noise defect investigation delivery):
    a 0.16% drop at the default PRICE_DROP_MIN_PCT=1.0 -> is_noise=1.
    Pinned explicitly (in addition to the pre-existing
    test_small_drop_below_threshold_is_flagged_noise, same scenario) for
    this delivery's traceability -- code review + this test both confirm
    is_noise is computed correctly in the current repo; the live 88%
    "significant" figure this delivery investigated did not reproduce
    from repo code, see noise_diagnostic.py's module docstring.
    """
    monkeypatch.setattr(config, "PRICE_DROP_MIN_PCT", Decimal("1.0"))
    conn, poller, _ = _seed_one(price="24.99")
    client2 = FakePortalsClient(pages=[[_mk_item("ext-1", "24.95")], []])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, poller.pair_floor_cache)
    poller2.poll_once()

    is_noise = conn.execute(
        "SELECT is_noise FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()[0]
    assert is_noise == 1


def test_is_noise_defect_investigation_29_pct_drop_is_not_noise(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 2: a 2.9% drop -> is_noise=0."""
    monkeypatch.setattr(config, "PRICE_DROP_MIN_PCT", Decimal("1.0"))
    conn, poller, _ = _seed_one(price="100.0")
    client2 = FakePortalsClient(pages=[[_mk_item("ext-1", "97.1")], []])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, poller.pair_floor_cache)
    poller2.poll_once()

    is_noise = conn.execute(
        "SELECT is_noise FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()[0]
    assert is_noise == 0
