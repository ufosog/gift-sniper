from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.errors import WafBlock
from gift_sniper.floors import FloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def _mk_item(i: int, model: str = "Inception") -> dict:
    return {
        "id": f"page-item-{i}",
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
            {"type": "model", "value": model, "rarity_per_mille": 1.0},
            {"type": "backdrop", "value": "Copper", "rarity_per_mille": 1.0},
        ],
    }


def _make_poller(pages):
    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=pages)
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    return Poller(conn, client, auth, floor_cache), client


def test_whole_page_processed_even_when_known_and_new_are_interleaved():
    """Confirmed live: known and new items on a page are INTERLEAVED, not
    all-new-then-all-known. This test fails on the old "break on first
    known id" implementation, which would only capture item(1) here and
    silently drop item(2)/item(3) after the known one at index 1.
    """
    seed_poller, seed_client = _make_poller(pages=[[_mk_item(0)], []])
    seed_poller.poll_once()  # seeds page-item-0 as known

    mixed_page = [_mk_item(1), _mk_item(0), _mk_item(2), _mk_item(3)]  # known at index 1
    poller = Poller(seed_poller.conn, FakePortalsClient(pages=[mixed_page, []]), seed_poller.auth, seed_poller.floor_cache)

    result = poller.poll_once()
    result_ids = {l.external_id for l in result}
    assert result_ids == {"page-item-1", "page-item-2", "page-item-3"}


def test_page_entirely_known_stops_pagination_next_page_not_requested():
    items = [_mk_item(i) for i in range(3)]

    conn = db.connect(":memory:")
    auth = AuthManager()
    floor_cache = FloorCache(FakePortalsClient(pages=[]), ttl_sec=600)

    seed_client = FakePortalsClient(pages=[items, []])
    seed_poller = Poller(conn, seed_client, auth, floor_cache)
    seed_poller.poll_once()  # writes all 3 as known

    client = FakePortalsClient(pages=[items, [_mk_item(99)]])  # offset=50 must never be hit
    poller = Poller(conn, client, auth, floor_cache)
    result = poller.poll_once()

    assert result == []
    assert client.search_offsets == [0]  # pagination stopped after the fully-known page


def test_search_page_none_aborts_iteration_distinctly_from_empty_page(caplog):
    class FailingClient(FakePortalsClient):
        def search(self, limit, offset, sort=None, min_price=None):
            raise WafBlock("simulated WAF block")

    conn = db.connect(":memory:")
    client = FailingClient(pages=[])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    poller = Poller(conn, client, auth, floor_cache)

    with caplog.at_level("ERROR"):
        result = poller.poll_once()

    assert result == []
    assert any("aborting poll iteration" in rec.message for rec in caplog.records)
    assert poller.stats["pages_fetched"] == 0  # the failed fetch does not count as a fetched page


def test_pagination_cap_hit_logs_warning_and_stops(caplog):
    # Every page is entirely new, forcing the loop to keep going until the
    # pagination cap kicks in.
    pages = [[_mk_item(i) for i in range(p * 50, p * 50 + 50)] for p in range(20)]

    poller, client = _make_poller(pages)

    with caplog.at_level("WARNING"):
        result = poller.poll_once()

    assert poller.stats["pagination_cap_hits"] == 1
    assert any("pagination cap hit" in rec.message for rec in caplog.records)
    # Exactly MAX_PAGES_PER_ITERATION pages worth of items collected.
    assert len(result) == config.MAX_PAGES_PER_ITERATION * 50


def test_summary_stats_track_seen_known_and_pages():
    known_page = [_mk_item(i) for i in range(5)]
    conn = db.connect(":memory:")
    auth = AuthManager()
    floor_cache = FloorCache(FakePortalsClient(pages=[]), ttl_sec=600)

    seed_client = FakePortalsClient(pages=[known_page, []])
    Poller(conn, seed_client, auth, floor_cache).poll_once()

    mixed = known_page[:2] + [_mk_item(i) for i in range(5, 8)]  # 2 known + 3 new
    client = FakePortalsClient(pages=[mixed, []])
    poller = Poller(conn, client, auth, floor_cache)
    result = poller.poll_once()

    assert len(result) == 3
    assert poller.stats["items_seen_total"] == 5
    assert poller.stats["items_already_known"] == 2
    assert poller.stats["pages_fetched"] == 2  # the mixed page + the empty page that ended pagination
