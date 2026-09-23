from gift_sniper import db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.pair_floor import PairFloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def _mk_item(i: int) -> dict:
    return {
        "id": f"fp-item-{i}",
        "tg_id": f"Item-{i}",
        "collection_id": "col-x",
        "name": "Collection X",
        "external_collection_number": i,
        "price": "20.0",
        "floor_price": "25.0",
        "photo_url": None,
        "animation_url": None,
        "listed_at": "2026-09-05T10:00:00Z",
        "unlocks_at": None,
        "status": "listed",
        "attributes": [
            {"type": "model", "value": "Inception", "rarity_per_mille": 1.0},
            {"type": "backdrop", "value": "Copper", "rarity_per_mille": 1.0},
        ],
    }


class AssertNoFloorCallsClient(FakePortalsClient):
    """Any floor-related network call made from the FAST PATH is a bug --
    fail loudly instead of quietly returning fake data.
    """

    def model_backgrounds_floors(self, model_names):
        raise AssertionError("model_backgrounds_floors must not be called from the FAST PATH collection loop")

    def search_pair_floor(self, collection_id, model_name, backdrop_name, limit=20, offset=0):
        raise AssertionError("search_pair_floor must not be called from the FAST PATH collection loop")


def test_poll_once_makes_zero_floor_requests():
    items = [_mk_item(i) for i in range(10)]
    conn = db.connect(":memory:")
    client = AssertNoFloorCallsClient(pages=[items, []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    pair_floor_cache = PairFloorCache(client, ttl_sec=300)
    poller = Poller(conn, client, auth, floor_cache, pair_floor_cache)

    result = poller.poll_once()  # must not raise

    assert len(result) == 10
    statuses = {r[0] for r in conn.execute("SELECT pair_floor_status FROM floor_snapshots")}
    assert statuses == {"pending"}
