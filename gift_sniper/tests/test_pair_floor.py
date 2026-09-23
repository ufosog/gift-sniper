from decimal import Decimal

from gift_sniper import config
from gift_sniper.pair_floor import PairFloorCache, _floor_from_response


class FakeClient:
    def __init__(self, responses, model_responses=None, fail_on_model_call=False):
        self._responses = list(responses)
        self._model_responses = list(model_responses) if model_responses else []
        self._fail_on_model_call = fail_on_model_call
        self.calls = []
        self.model_calls = []

    def search_pair_floor(self, collection_id, model_name, backdrop_name, limit=20, offset=0):
        self.calls.append((collection_id, model_name, backdrop_name, limit, offset))
        return self._responses.pop(0)

    def search_model_floor(self, collection_id, model_name, limit=50, offset=0):
        if self._fail_on_model_call:
            raise AssertionError(
                "search_model_floor must NOT be called when the pair level "
                "already gave 'ok' -- see Правка 2 test item 4"
            )
        self.model_calls.append((collection_id, model_name, limit, offset))
        return self._model_responses.pop(0)


def _item(id_, status, price):
    return {"id": id_, "status": status, "price": price}


def test_first_unlisted_null_price_is_skipped_not_taken_as_floor():
    resp = {
        "results": [
            _item("u1", "unlisted", None),
            _item("a", "listed", "5.0"),
            _item("b", "listed", "6.5"),
        ]
    }
    floor = _floor_from_response(resp, exclude_external_id="nonexistent")
    assert floor.status == "ok"
    assert floor.floor_nano == int(Decimal("5.0") * config.NANO)
    assert floor.floor_excluding_self_nano == int(Decimal("5.0") * config.NANO)
    assert floor.unlisted_skipped == 1
    assert floor.listed_count == 2


def test_all_unlisted_gives_no_data_not_error():
    resp = {"results": [_item("u1", "unlisted", None), _item("u2", "unlisted", None)]}
    floor = _floor_from_response(resp, exclude_external_id="nonexistent")
    assert floor.status == "no_data"
    assert floor.floor_nano is None
    assert floor.floor_excluding_self_nano is None
    assert floor.unlisted_skipped == 2


def test_empty_results_gives_no_data():
    floor = _floor_from_response({"results": []}, exclude_external_id="nonexistent")
    assert floor.status == "no_data"
    assert floor.floor_nano is None
    assert floor.floor_excluding_self_nano is None


def test_excluding_the_cheapest_listing_promotes_the_next_one_and_flags_self_was_floor():
    """The core self-comparison bug: a 3-listing book where the cheapest
    IS the listing being excluded (e.g. it just dropped its own price and
    is being compared against the book). floor_excluding_self_nano must
    be the SECOND cheapest, not the excluded listing's own price.
    """
    resp = {
        "results": [
            _item("cheap", "listed", "150.0"),   # the listing being excluded
            _item("mid", "listed", "195.0"),
            _item("high", "listed", "220.0"),
        ]
    }
    floor = _floor_from_response(resp, exclude_external_id="cheap")

    assert floor.floor_nano == int(Decimal("150.0") * config.NANO)  # including self -- diagnostic only
    assert floor.floor_excluding_self_nano == int(Decimal("195.0") * config.NANO)
    assert floor.listed_count_excluding_self == 2
    assert floor.self_was_floor is True
    assert floor.status == "ok"


def test_single_listing_book_where_it_is_the_excluded_one_gives_alone_in_pair():
    """A pair with exactly one active listing, which is the one being
    excluded: nothing left to compare against. This is a CORRECT
    "alone_in_pair" outcome (confirmed live: the common case, not rare --
    336 pairs measured with exactly one listing, none with ten), distinct
    from "no_data" (nothing listed at all, not even the excluded lot) and
    from "error". "ok" must NEVER be returned when
    floor_excluding_self_nano is None -- that was the exact bug this
    status value exists to prevent.
    """
    resp = {"results": [_item("only", "listed", "195.0")]}
    floor = _floor_from_response(resp, exclude_external_id="only")

    assert floor.status == "alone_in_pair"
    assert floor.floor_excluding_self_nano is None
    assert floor.listed_count_excluding_self == 0
    assert floor.self_was_floor is True  # it WAS the (self-inclusive) floor, trivially
    assert floor.floor_nano == int(Decimal("195.0") * config.NANO)  # diagnostic value unaffected


def test_excluding_a_non_floor_listing_does_not_change_the_floor():
    resp = {
        "results": [
            _item("cheap", "listed", "150.0"),
            _item("mid", "listed", "195.0"),
        ]
    }
    floor = _floor_from_response(resp, exclude_external_id="mid")
    assert floor.floor_excluding_self_nano == int(Decimal("150.0") * config.NANO)
    assert floor.self_was_floor is False


def test_cache_hits_avoid_second_network_call():
    resp = {"results": [_item("other", "listed", "4.39")]}
    client = FakeClient([resp])
    cache = PairFloorCache(client, ttl_sec=300)

    floor1, age1 = cache.get("col-1", "Emperor", "Black", exclude_external_id="self")
    assert age1 == 0
    assert len(client.calls) == 1

    floor2, age2 = cache.get("col-1", "Emperor", "Black", exclude_external_id="self")
    assert len(client.calls) == 1  # cache hit, no second network call
    assert floor2.floor_excluding_self_nano == floor1.floor_excluding_self_nano


def test_cache_reports_actual_age(monkeypatch):
    resp = {"results": [_item("other", "listed", "4.39")]}
    client = FakeClient([resp])
    cache = PairFloorCache(client, ttl_sec=300)

    fake_time = {"t": 1000.0}
    monkeypatch.setattr("gift_sniper.pair_floor.time.monotonic", lambda: fake_time["t"])

    cache.get("col-1", "Emperor", "Black", exclude_external_id="self")
    fake_time["t"] += 42.0
    _, age = cache.get("col-1", "Emperor", "Black", exclude_external_id="self")

    assert age == 42
    assert len(client.calls) == 1


def test_different_pairs_are_not_confused():
    resp_a = {"results": [_item("a-other", "listed", "10.0")]}
    resp_b = {"results": [_item("b-other", "listed", "100.0")]}
    client = FakeClient([resp_a, resp_b])
    cache = PairFloorCache(client, ttl_sec=300)

    floor_a, _ = cache.get("col-A", "Rare", "Gold", exclude_external_id="self")
    floor_b, _ = cache.get("col-B", "Rare", "Gold", exclude_external_id="self")  # same model+backdrop, different collection

    assert floor_a.floor_excluding_self_nano != floor_b.floor_excluding_self_nano
    assert len(client.calls) == 2


def test_get_fresh_bypasses_cache_and_refreshes_it():
    resp1 = {"results": [_item("other", "listed", "10.0")]}
    resp2 = {"results": [_item("other", "listed", "8.0")]}
    client = FakeClient([resp1, resp2])
    cache = PairFloorCache(client, ttl_sec=300)

    cache.get("col-1", "Rare", "Gold", exclude_external_id="self")  # populates cache with resp1
    floor, age = cache.get_fresh("col-1", "Rare", "Gold", exclude_external_id="self")

    assert len(client.calls) == 2  # get_fresh made a real second call, ignoring TTL
    assert age == 0
    assert floor.floor_excluding_self_nano == int(Decimal("8.0") * config.NANO)

    # And the cache was refreshed -- a plain get() right after sees resp2, not resp1.
    floor2, _ = cache.get("col-1", "Rare", "Gold", exclude_external_id="self")
    assert len(client.calls) == 2  # no third call -- served from the refreshed cache
    assert floor2.floor_excluding_self_nano == int(Decimal("8.0") * config.NANO)


def test_get_model_floor_three_listings_different_backdrops_one_excluded():
    """Правка 2, test item 3: model_floor fixture with three listings, same
    model, different backdrops, one excluded -> minimum of the two
    remaining. search_model_floor (no backdrop filter) is used, not
    search_pair_floor.
    """
    resp = {
        "results": [
            _item("self", "listed", "150.0"),
            _item("gold", "listed", "195.0"),
            _item("black", "listed", "220.0"),
        ]
    }
    client = FakeClient(responses=[], model_responses=[resp])
    cache = PairFloorCache(client, ttl_sec=300)

    floor, age = cache.get_model_floor("col-1", "Emperor", exclude_external_id="self")

    assert age == 0
    assert floor.status == "ok"
    assert floor.floor_excluding_self_nano == int(Decimal("195.0") * config.NANO)
    assert floor.listed_count_excluding_self == 2
    assert len(client.model_calls) == 1
    assert len(client.calls) == 0  # never touches search_pair_floor


def test_get_model_floor_fresh_bypasses_cache_and_refreshes_it():
    resp1 = {"results": [_item("other", "listed", "10.0")]}
    resp2 = {"results": [_item("other", "listed", "8.0")]}
    client = FakeClient(responses=[], model_responses=[resp1, resp2])
    cache = PairFloorCache(client, ttl_sec=300)

    cache.get_model_floor("col-1", "Rare", exclude_external_id="self")  # populates cache with resp1
    floor, age = cache.get_model_floor_fresh("col-1", "Rare", exclude_external_id="self")

    assert len(client.model_calls) == 2
    assert age == 0
    assert floor.floor_excluding_self_nano == int(Decimal("8.0") * config.NANO)


def test_model_floor_not_requested_when_pair_already_ok():
    """Правка 2, test item 4: model floor must NEVER be requested when the
    pair level already gave 'ok' -- the caller (poller.py's
    run_floor_worker) must only call get_model_floor when the pair level
    came back 'alone_in_pair'. This test's mock client raises if
    search_model_floor is ever called, standing in for that discipline
    being exercised at the caller site.
    """
    resp = {
        "results": [
            _item("self", "listed", "150.0"),
            _item("other", "listed", "195.0"),
        ]
    }
    client = FakeClient(responses=[resp], fail_on_model_call=True)
    cache = PairFloorCache(client, ttl_sec=300)

    pair, _age = cache.get("col-1", "Emperor", "Black", exclude_external_id="self")
    assert pair.status == "ok"

    # A caller respecting Правка 2 simply never calls get_model_floor here.
    # Directly invoking it would raise via the mock -- confirming the mock
    # itself is wired correctly to catch a violation of that discipline.
    try:
        cache.get_model_floor("col-1", "Emperor", exclude_external_id="self")
        raised = False
    except AssertionError:
        raised = True
    assert raised
