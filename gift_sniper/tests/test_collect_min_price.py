import logging

from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.pair_floor import PairFloorCache
from gift_sniper.poller import Poller
from gift_sniper.portals_client import PortalsClient
from .fakes import FakePortalsClient


class FakeResponse:
    def __init__(self, status_code=200, headers=None, json_body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_body = json_body or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        pass


class UrlCapturingSession:
    def __init__(self):
        self.urls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.urls.append(url)
        return FakeResponse(200, json_body={"results": []})


def test_search_with_min_price_includes_it_in_url_without_breaking_order():
    session = UrlCapturingSession()
    client = PortalsClient(auth_provider=lambda: "tok", session=session, request_delay_ms=0)

    client.search(limit=50, offset=0, min_price="15")

    query = session.urls[0].split("?", 1)[1]
    assert "min_price=15" in query
    keys_in_order = [pair.split("=")[0] for pair in query.split("&")]
    assert keys_in_order == ["limit", "offset", "min_price"]


def test_search_without_min_price_omits_the_param():
    session = UrlCapturingSession()
    client = PortalsClient(auth_provider=lambda: "tok", session=session, request_delay_ms=0)

    client.search(limit=50, offset=0)
    client.search(limit=50, offset=0, min_price=None)

    for url in session.urls:
        assert "min_price" not in url


def test_search_page_passes_none_when_collect_min_price_is_disabled(monkeypatch):
    """COLLECT_MIN_PRICE=0 (or unset) means poller.py must pass min_price=
    None through to the client -- this is where "0 or empty -> no filter"
    is actually decided, not inside PortalsClient.search() itself.
    """
    monkeypatch.setattr(config, "COLLECT_MIN_PRICE_NANO", 0)

    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[[], []])
    auth = AuthManager()
    poller = Poller(conn, client, auth, FloorCache(client), PairFloorCache(client))

    poller.poll_once()

    assert client.search_min_prices == [None]


def _mk_item(ext_id: str, price: str) -> dict:
    return {
        "id": ext_id,
        "tg_id": f"{ext_id}-tg",
        "collection_id": "col-x",
        "name": "Collection X",
        "external_collection_number": 1,
        "price": price,
        "floor_price": "9.0",
        "photo_url": None,
        "animation_url": None,
        "listed_at": "2026-09-05T10:00:00Z",
        "unlocks_at": None,
        "status": "listed",
        "attributes": [
            {"type": "model", "value": "M", "rarity_per_mille": 1.0},
            {"type": "backdrop", "value": "Copper", "rarity_per_mille": 1.0},
        ],
    }


def test_listing_below_collect_min_price_never_written_even_if_server_returns_it():
    """Defensive net: COLLECT_MIN_PRICE is sent as a server-side filter,
    but if the server ever returns something under it anyway, the poller
    must not write it to the DB at all -- not with a nulled raw, not with
    any row at all.
    """
    assert config.COLLECT_MIN_PRICE_NANO == 15 * config.NANO  # sanity on the default this test relies on

    too_cheap = _mk_item("too-cheap-1", "10")  # below COLLECT_MIN_PRICE=15
    fine = _mk_item("fine-1", "20")

    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[[too_cheap, fine], []])
    auth = AuthManager()
    poller = Poller(conn, client, auth, FloorCache(client), PairFloorCache(client))

    result = poller.poll_once()

    result_ids = {l.external_id for l in result}
    assert result_ids == {"fine-1"}

    count = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE external_id = 'too-cheap-1'"
    ).fetchone()[0]
    assert count == 0  # no row at all, not even with raw=NULL

    assert poller.stats["collect_filtered_count"] == 1


def test_warning_logged_when_collect_min_price_exceeds_floor_min_price(monkeypatch, caplog):
    import importlib

    monkeypatch.setenv("PORTALS_AUTH", "test-token")
    monkeypatch.setenv("COLLECT_MIN_PRICE", "20")
    monkeypatch.setenv("FLOOR_MIN_PRICE", "15")

    from gift_sniper import config as config_module

    with caplog.at_level(logging.WARNING, logger="gift_sniper.config"):
        importlib.reload(config_module)

    try:
        assert any(
            "COLLECT_MIN_PRICE" in rec.message and "FLOOR_MIN_PRICE" in rec.message
            for rec in caplog.records
        )
    finally:
        # Restore the module to its normal (env-default) state for any
        # later test in the same process.
        monkeypatch.delenv("COLLECT_MIN_PRICE", raising=False)
        monkeypatch.delenv("FLOOR_MIN_PRICE", raising=False)
        importlib.reload(config_module)


def test_pair_floor_cache_hits_increments_on_repeated_pair_within_ttl():
    responses = {("col-1", "Emperor", "Black"): {"results": [{"id": "other", "status": "listed", "price": "4.39"}]}}
    client = FakePortalsClient(pages=[], pair_floor_responses=responses)
    cache = PairFloorCache(client, ttl_sec=300)

    cache.get("col-1", "Emperor", "Black", exclude_external_id="self")
    assert cache.cache_hits == 0

    cache.get("col-1", "Emperor", "Black", exclude_external_id="self")
    cache.get("col-1", "Emperor", "Black", exclude_external_id="self")
    assert cache.cache_hits == 2
