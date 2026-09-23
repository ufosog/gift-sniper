from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.pair_floor import PairFloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def _mk_item(ext_id: str, price, listed_at="2026-09-06T14:33:00Z") -> dict:
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
            {"type": "model", "value": "M", "rarity_per_mille": 1.0},
            {"type": "backdrop", "value": "Copper", "rarity_per_mille": 1.0},
        ],
    }


def _seed_one(price: str):
    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[[_mk_item("ext-1", price)], []])
    auth = AuthManager()
    poller = Poller(conn, client, auth, FloorCache(client), PairFloorCache(client))
    poller.poll_once()
    return conn, poller


class FailingPairFloorClient(FakePortalsClient):
    """Any call to search_pair_floor (used by get_fresh for above-threshold
    drops) fails loudly -- used to prove a below-threshold (noise) drop
    never triggers a floor re-fetch.
    """

    def search_pair_floor(self, collection_id, model_name, backdrop_name, limit=20, offset=0):
        raise AssertionError("search_pair_floor must not be called for a noise-level drop")


def test_drop_above_threshold_triggers_floor_refetch():
    conn, poller = _seed_one("28.01")

    client2 = FakePortalsClient(
        pages=[[_mk_item("ext-1", "24.99")], []],
        pair_floor_responses={("col-x", "M", "Copper"): {"results": [{"id": "other", "status": "listed", "price": "20.0"}]}},
    )
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, PairFloorCache(client2))
    poller2.poll_once()

    assert client2.pair_floor_calls == [("col-x", "M", "Copper")]
    floor_at_drop = conn.execute(
        "SELECT floor_at_drop_nano FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()[0]
    assert floor_at_drop == int(Decimal("20.0") * config.NANO)


def test_drop_below_threshold_never_calls_pair_floor():
    conn, poller = _seed_one("28.01")

    client2 = FailingPairFloorClient(pages=[[_mk_item("ext-1", "27.99")], []])  # tiny drop, < 1%
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, PairFloorCache(client2))

    poller2.poll_once()  # must not raise

    floor_at_drop = conn.execute(
        "SELECT floor_at_drop_nano FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()[0]
    assert floor_at_drop is None


def test_single_step_90_percent_drop_is_anomaly():
    conn, poller = _seed_one("999")

    client2 = FakePortalsClient(pages=[[_mk_item("ext-1", "99")], []])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, PairFloorCache(client2))
    poller2.poll_once()

    is_anomaly = conn.execute(
        "SELECT is_anomaly FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()[0]
    assert is_anomaly == 1


def test_single_step_30_percent_drop_is_not_anomaly():
    conn, poller = _seed_one("100")

    client2 = FakePortalsClient(pages=[[_mk_item("ext-1", "70")], []])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, PairFloorCache(client2))
    poller2.poll_once()

    is_anomaly = conn.execute(
        "SELECT is_anomaly FROM price_history WHERE listing_external_id='ext-1'"
    ).fetchone()[0]
    assert is_anomaly == 0


def test_two_drops_within_burst_window_both_flagged_anomaly(monkeypatch):
    conn, poller = _seed_one("999")

    fake_now = {"t": datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr("gift_sniper.poller.datetime", type(
        "FakeDatetime", (),
        {"now": staticmethod(lambda tz=None: fake_now["t"]), "fromisoformat": staticmethod(datetime.fromisoformat)},
    ))

    client2 = FakePortalsClient(pages=[[_mk_item("ext-1", "99")], []])
    poller2 = Poller(conn, client2, poller.auth, poller.floor_cache, PairFloorCache(client2))
    poller2.poll_once()  # 999 -> 99, first drop, "now" = 12:00:00

    fake_now["t"] = datetime(2026, 9, 6, 12, 0, 10, tzinfo=timezone.utc)  # 10 seconds later
    client3 = FakePortalsClient(pages=[[_mk_item("ext-1", "29")], []])
    poller3 = Poller(conn, client3, poller.auth, poller.floor_cache, PairFloorCache(client3))
    poller3.poll_once()  # 99 -> 29, second drop, 10s after the first

    rows = conn.execute(
        "SELECT old_price_nano, is_anomaly FROM price_history WHERE listing_external_id='ext-1' ORDER BY observed_at"
    ).fetchall()
    assert len(rows) == 2
    assert all(is_anomaly == 1 for _old, is_anomaly in rows)  # BOTH flagged, including the retroactively-updated first
