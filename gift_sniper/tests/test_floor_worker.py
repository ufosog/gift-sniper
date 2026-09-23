from datetime import datetime, timezone

from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.pair_floor import PairFloorCache
from gift_sniper.poller import Poller, _in_dual_floor_sample
from .fakes import FakePortalsClient


def _mk_item(i: int, price: str = "20.0") -> dict:
    return {
        "id": f"fw-item-{i}",
        "tg_id": f"Item-{i}",
        "collection_id": "col-x",
        "name": "Collection X",
        "external_collection_number": i,
        "price": price,
        "floor_price": "25.0",
        "photo_url": None,
        "animation_url": None,
        "listed_at": "2026-09-05T10:00:00Z",
        "unlocks_at": None,
        "status": "listed",
        "attributes": [
            {"type": "model", "value": f"Model{i}", "rarity_per_mille": 1.0},
            {"type": "backdrop", "value": "Copper", "rarity_per_mille": 1.0},
        ],
    }


def _seeded_poller(n_items, price="20.0"):
    items = [_mk_item(i, price) for i in range(n_items)]
    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[items, []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    pair_floor_cache = PairFloorCache(client, ttl_sec=300)
    poller = Poller(conn, client, auth, floor_cache, pair_floor_cache)
    poller.poll_once()
    return poller


def test_floor_worker_processes_at_most_batch_limit_per_run():
    poller = _seeded_poller(config.FLOOR_BATCH_PER_RUN + 5)

    pending_before = db.count_pending_floor_rows(poller.conn)
    assert pending_before == config.FLOOR_BATCH_PER_RUN + 5

    processed = poller.run_floor_worker()
    assert processed == config.FLOOR_BATCH_PER_RUN

    pending_after = db.count_pending_floor_rows(poller.conn)
    assert pending_after == 5

    # A second run clears the rest.
    processed_2 = poller.run_floor_worker()
    assert processed_2 == 5
    assert db.count_pending_floor_rows(poller.conn) == 0


def test_floor_worker_updates_status_from_pending_to_resolved():
    poller = _seeded_poller(3)

    statuses_before = {r[0] for r in poller.conn.execute("SELECT pair_floor_status FROM floor_snapshots")}
    assert statuses_before == {"pending"}

    poller.run_floor_worker()

    statuses_after = {r[0] for r in poller.conn.execute("SELECT pair_floor_status FROM floor_snapshots")}
    assert "pending" not in statuses_after
    assert statuses_after <= {"ok", "no_data", "error"}


def test_floor_worker_fetches_model_floor_only_when_pair_alone_in_pair():
    """Правка 2: run_floor_worker must call search_model_floor ONLY for
    rows where the pair level came back "alone_in_pair" -- never when the
    pair query has no results at all (pair status stays "no_data", the
    default when FakePortalsClient has no configured pair_floor_response),
    and never for a fixture where two OTHER listings share the pair so the
    pair level is directly "ok".
    """
    item_alone = _mk_item(0, price="20.0")  # own pair query returns only itself -> alone_in_pair
    item_ok = _mk_item(1, price="20.0")
    items = [item_alone, item_ok]

    conn = db.connect(":memory:")
    client = FakePortalsClient(
        pages=[items, []],
        pair_floor_responses={
            ("col-x", "Model0", "Copper"): {
                "results": [{"id": "fw-item-0", "status": "listed", "price": "20.0"}]
            },
            ("col-x", "Model1", "Copper"): {
                "results": [
                    {"id": "fw-item-1", "status": "listed", "price": "20.0"},
                    {"id": "other", "status": "listed", "price": "18.0"},
                ]
            },
        },
        model_floor_responses={
            ("col-x", "Model0"): {
                "results": [
                    {"id": "fw-item-0", "status": "listed", "price": "20.0"},
                    {"id": "model-other", "status": "listed", "price": "15.0"},
                ]
            },
        },
    )
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    pair_floor_cache = PairFloorCache(client, ttl_sec=300)
    poller = Poller(conn, client, auth, floor_cache, pair_floor_cache)
    poller.poll_once()
    poller.run_floor_worker()

    assert client.model_floor_calls == [("col-x", "Model0")]  # only the alone_in_pair row

    rows = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT listing_external_id, pair_floor_status, model_floor_status FROM floor_snapshots"
        )
    }
    assert rows["fw-item-0"] == ("alone_in_pair", "ok")
    assert rows["fw-item-1"] == ("ok", "no_data")

    assert poller.stats["floor_ok_pair"] == 1
    assert poller.stats["floor_ok_model"] == 1
    assert poller.stats["floor_alone_in_pair"] == 0
    assert poller.stats["floor_no_data"] == 0


def _seeded_poller_with_pair_ok(n_items, price="20.0"):
    """Every item's own pair query resolves to pair_floor_status='ok'
    (itself plus one other listed price), so run_floor_worker never
    naturally needs the model-level fallback -- the only reason it would
    still fetch the model floor is DUAL_FLOOR_SAMPLE_PCT (Правка, this
    task).
    """
    items = [_mk_item(i, price) for i in range(n_items)]
    conn = db.connect(":memory:")
    pair_floor_responses = {
        ("col-x", f"Model{i}", "Copper"): {
            "results": [
                {"id": f"fw-item-{i}", "status": "listed", "price": price},
                {"id": f"other-{i}", "status": "listed", "price": "18.0"},
            ]
        }
        for i in range(n_items)
    }
    model_floor_responses = {
        ("col-x", f"Model{i}"): {
            "results": [
                {"id": f"fw-item-{i}", "status": "listed", "price": price},
                {"id": f"model-other-{i}", "status": "listed", "price": "15.0"},
            ]
        }
        for i in range(n_items)
    }
    client = FakePortalsClient(
        pages=[items, []],
        pair_floor_responses=pair_floor_responses,
        model_floor_responses=model_floor_responses,
    )
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    pair_floor_cache = PairFloorCache(client, ttl_sec=300)
    poller = Poller(conn, client, auth, floor_cache, pair_floor_cache)
    poller.poll_once()
    return poller, client


def test_dual_floor_sample_pct_zero_never_requests_model_floor_when_pair_ok(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 1: DUAL_FLOOR_SAMPLE_PCT=0 (the default) ->
    model floor is never requested for a row whose pair floor is 'ok'.
    """
    monkeypatch.setattr(config, "DUAL_FLOOR_SAMPLE_PCT", 0)
    poller, client = _seeded_poller_with_pair_ok(5)
    poller.run_floor_worker()

    assert client.model_floor_calls == []
    assert poller.stats["dual_floor_samples"] == 0

    statuses = {r[0] for r in poller.conn.execute("SELECT model_floor_status FROM floor_snapshots")}
    assert statuses == {"no_data"}


def test_dual_floor_sample_pct_100_requests_model_floor_for_every_ok_row(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 2: DUAL_FLOOR_SAMPLE_PCT=100 -> requested for
    ALL rows whose pair floor is 'ok'.
    """
    monkeypatch.setattr(config, "DUAL_FLOOR_SAMPLE_PCT", 100)
    poller, client = _seeded_poller_with_pair_ok(5)
    poller.run_floor_worker()

    assert len(client.model_floor_calls) == 5
    assert poller.stats["dual_floor_samples"] == 5
    assert poller.stats["floor_ok_pair"] == 5  # sampling doesn't change the pair/model classification

    rows = {
        r[0]: (r[1], r[2])
        for r in poller.conn.execute(
            "SELECT listing_external_id, pair_floor_status, model_floor_status FROM floor_snapshots"
        )
    }
    for ext_id, (pair_status, model_status) in rows.items():
        assert pair_status == "ok"
        assert model_status == "ok"


def test_dual_floor_sample_selection_is_deterministic_across_calls():
    """КАК ТЕСТИРОВАТЬ item 3: the same external_id at the same
    percentage lands in (or out of) the sample identically across
    repeated calls -- no per-call randomness.
    """
    ext_id = "some-fixed-external-id-42"
    pct = 37
    results = {_in_dual_floor_sample(ext_id, pct) for _ in range(20)}
    assert len(results) == 1  # always the same answer

    # And a completely different process/run (a fresh call, no shared
    # state) must agree too -- there is no per-call or per-process seed.
    assert _in_dual_floor_sample(ext_id, pct) == _in_dual_floor_sample(ext_id, pct)


def test_dual_floor_sample_pct_100_always_true_pct_0_always_false():
    assert _in_dual_floor_sample("anything", 100) is True
    assert _in_dual_floor_sample("anything", 0) is False


def test_floor_worker_resolves_below_threshold_rows_without_network_call(monkeypatch):
    # This is about the FLOOR_MIN_PRICE fast-resolution path specifically
    # -- disable the independent COLLECT_MIN_PRICE filter so a price of 5
    # still reaches the DB in the first place.
    monkeypatch.setattr(config, "COLLECT_MIN_PRICE_NANO", 0)
    poller = _seeded_poller(2, price="5")  # below FLOOR_MIN_PRICE

    processed = poller.run_floor_worker()
    assert processed == 0  # nothing needed a network call
    assert db.count_pending_floor_rows(poller.conn) == 0

    statuses = {r[0] for r in poller.conn.execute("SELECT pair_floor_status FROM floor_snapshots")}
    assert statuses == {"no_data"}
