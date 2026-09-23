import json
import re

import pytest

from gift_sniper import config
from gift_sniper.tonnel_client import TonnelClient, TonnelError, MAX_LIMIT, _regex_filter


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, headers=None, impersonate=None, timeout=None):
        self.calls.append((url, json))
        return self._responses.pop(0)


def _gift(gift_num, price, status="forsale", underLoan=False, premarketData=None, gift_id=None):
    return {
        "gift_num": gift_num,
        "name": "Ice Cream",
        "model": "Emperor (5%)",
        "backdrop": "Black (10%)",
        "symbol": "Star (2%)",
        "price": price,
        "gift_id": gift_id if gift_id is not None else 1000 + gift_num,
        "status": status,
        "asset": "TON",
        "limited": True,
        "underLoan": underLoan,
        "premarketData": premarketData,
        "dutchAuctionData": None,
        "auction": None,
    }


def test_search_body_has_sort_and_filter_as_strings_limit_and_empty_auth():
    """КАК ТЕСТИРОВАТЬ item 1."""
    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    client.search(gift_name="Ice Cream", limit=30)

    _url, body = session.calls[0]
    assert isinstance(body["sort"], str)
    assert isinstance(body["filter"], str)
    json.loads(body["sort"])  # must parse as JSON
    json.loads(body["filter"])
    assert body["limit"] <= MAX_LIMIT
    assert body["user_auth"] == ""
    assert body["price_range"] is None


def test_search_rejects_limit_above_30_without_a_request():
    session = FakeSession([])
    client = TonnelClient(session=session, request_delay_ms=0)
    try:
        client.search(limit=50)
        assert False, "expected TonnelError"
    except TonnelError:
        pass
    assert session.calls == []  # never even attempted the request


def test_search_min_price_merges_gte_into_existing_exists_condition():
    """КАК ТЕСТИРОВАТЬ item 1: min_price=15 -> filter has
    {"price": {"$exists": true, "$gte": 15}}, both conditions present,
    $exists NOT clobbered.
    """
    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    client.search(min_price=15)

    _url, body = session.calls[0]
    filter_dict = json.loads(body["filter"])
    assert filter_dict["price"] == {"$exists": True, "$gte": 15}


def test_search_without_min_price_has_only_exists_condition():
    """КАК ТЕСТИРОВАТЬ item 2: no min_price -> filter's price key has
    only {"$exists": true}.
    """
    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    client.search()

    _url, body = session.calls[0]
    filter_dict = json.loads(body["filter"])
    assert filter_dict["price"] == {"$exists": True}


def test_search_min_price_accepts_decimal():
    from decimal import Decimal

    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    client.search(min_price=Decimal("15"))

    _url, body = session.calls[0]
    filter_dict = json.loads(body["filter"])
    assert filter_dict["price"]["$gte"] == 15.0


def test_search_min_price_does_not_mutate_base_filter():
    """BASE_FILTER is a module-level dict -- a per-call min_price must
    never leak into a later call that doesn't pass one.
    """
    session = FakeSession([FakeResponse(200, []), FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    client.search(min_price=15)
    client.search()

    _url, body = session.calls[1]
    filter_dict = json.loads(body["filter"])
    assert "$gte" not in filter_dict["price"]


def test_regex_filter_escapes_apostrophe():
    """КАК ТЕСТИРОВАТЬ item 2: model "Durov's Cap" gives a valid regex."""
    result = _regex_filter("Durov's Cap")
    pattern = result["$regex"]
    re.compile(pattern)  # must not raise -- valid regex syntax
    assert re.match(pattern, "Durov's Cap (1.5%)")
    assert "\\'" in pattern or "'" in pattern  # escaped or literal, either way compiles safely


def test_search_uses_regex_filter_for_model_and_backdrop():
    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    client.search(model="Fried Chicken", backdrop="Onyx Black")

    _url, body = session.calls[0]
    filter_dict = json.loads(body["filter"])
    assert filter_dict["model"] == {"$regex": "^Fried\\ Chicken"} or filter_dict["model"]["$regex"].startswith("^Fried")
    assert filter_dict["backdrop"]["$regex"].startswith("^Onyx")


def test_pair_floor_excludes_underloan_listing():
    """КАК ТЕСТИРОВАТЬ item 3: 3 lots, one underLoan=True -> excluded."""
    gifts = [
        _gift(1, 10.0),
        _gift(2, 5.0, underLoan=True),  # cheapest but excluded
        _gift(3, 8.0),
    ]
    session = FakeSession([FakeResponse(200, gifts)])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.pair_floor("Ice Cream", "Emperor", "Black")

    assert floor.status == "ok"
    assert floor.floor_nano == int(8.0 * config.NANO)
    assert floor.listed_count == 2


def test_pair_floor_excludes_matching_gift_num():
    """КАК ТЕСТИРОВАТЬ item 4: a lot with the excluded gift_num is dropped."""
    gifts = [
        _gift(42, 3.0),  # cheapest but this is the excluded listing itself
        _gift(43, 9.0),
    ]
    session = FakeSession([FakeResponse(200, gifts)])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.pair_floor("Ice Cream", "Emperor", "Black", exclude_gift_num=42)

    assert floor.status == "ok"
    assert floor.floor_nano == int(9.0 * config.NANO)
    assert floor.listed_count == 1


def test_pair_floor_excludes_premarket_listing():
    gifts = [
        _gift(1, 4.0, premarketData={"something": True}),
        _gift(2, 12.0),
    ]
    session = FakeSession([FakeResponse(200, gifts)])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.pair_floor("Ice Cream", "Emperor", "Black")

    assert floor.status == "ok"
    assert floor.floor_nano == int(12.0 * config.NANO)


def test_pair_floor_computes_floor_with_fee():
    gifts = [_gift(1, 60.0)]
    session = FakeSession([FakeResponse(200, gifts)])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.pair_floor("Ice Cream", "Emperor", "Black")

    assert floor.floor_nano == int(60.0 * config.NANO)
    assert floor.floor_with_fee_nano == int(66.0 * config.NANO)


def test_pair_floor_empty_response_gives_no_data_not_error():
    """КАК ТЕСТИРОВАТЬ item 5: empty response -> status=no_data, no exception."""
    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.pair_floor("Ice Cream", "Emperor", "Black")

    assert floor.status == "no_data"
    assert floor.floor_nano is None
    assert floor.floor_with_fee_nano is None
    assert floor.listed_count == 0


def test_pair_floor_all_excluded_gives_no_data():
    gifts = [_gift(1, 5.0, underLoan=True), _gift(2, 6.0, status="sold")]
    session = FakeSession([FakeResponse(200, gifts)])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.pair_floor("Ice Cream", "Emperor", "Black")

    assert floor.status == "no_data"


def test_model_floor_no_backdrop_in_query():
    """Правка 1: model_floor() queries gift_name + model only, no
    backdrop filter.
    """
    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    client.model_floor("Ice Cream", "Emperor")

    _url, body = session.calls[0]
    filter_dict = json.loads(body["filter"])
    assert "backdrop" not in filter_dict
    assert filter_dict["model"]["$regex"].startswith("^Emperor")


def test_model_floor_excludes_bundle_negative_gift_id():
    """КАК ТЕСТИРОВАТЬ item 6: gift_id < 0 (a bundle) excluded from the
    model floor computation.
    """
    gifts = [
        _gift(1, 5.0, gift_id=-555),  # bundle, cheapest but excluded
        _gift(2, 9.0),
        _gift(3, 10.0),
    ]
    session = FakeSession([FakeResponse(200, gifts)])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.model_floor("Ice Cream", "Emperor")

    assert floor.status == "ok"
    assert floor.floor_nano == int(9.0 * config.NANO)
    assert floor.listed_count == 2


def test_model_floor_excludes_underloan_and_self():
    """КАК ТЕСТИРОВАТЬ item 6: underLoan excluded too."""
    gifts = [
        _gift(1, 4.0, underLoan=True),
        _gift(2, 6.0),
        _gift(42, 2.0),  # self, excluded by gift_num
    ]
    session = FakeSession([FakeResponse(200, gifts)])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.model_floor("Ice Cream", "Emperor", exclude_gift_num=42)

    assert floor.status == "ok"
    assert floor.floor_nano == int(6.0 * config.NANO)
    assert floor.listed_count == 1


def test_model_floor_reports_listed_count_without_threshold_opinion():
    """model_floor() itself doesn't know about TONNEL_MODEL_MIN_LISTED_COUNT
    -- it just reports the real count; the caller (tonnel_poller.py)
    decides "thin_model_book" vs "ok".
    """
    gifts = [_gift(1, 10.0), _gift(2, 12.0)]  # only 2 listings
    session = FakeSession([FakeResponse(200, gifts)])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.model_floor("Ice Cream", "Emperor")

    assert floor.status == "ok"  # a real floor was found
    assert floor.listed_count == 2  # thinness is visible, not hidden


def test_model_floor_empty_response_gives_no_data():
    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    floor = client.model_floor("Ice Cream", "Emperor")
    assert floor.status == "no_data"


def test_search_raises_tonnel_error_on_non_200():
    session = FakeSession([FakeResponse(500, None, text="Internal Server Error")])
    client = TonnelClient(session=session, request_delay_ms=0)
    try:
        client.search(gift_name="Ice Cream")
        assert False, "expected TonnelError"
    except TonnelError:
        pass


def test_search_raises_tonnel_error_on_api_error_body():
    session = FakeSession([FakeResponse(200, {"error": "limit is too big"})])
    client = TonnelClient(session=session, request_delay_ms=0)
    try:
        client.search(gift_name="Ice Cream", limit=30)
        assert False, "expected TonnelError"
    except TonnelError:
        pass


def test_search_throttles_between_requests(monkeypatch):
    sleeps = []
    session = FakeSession([FakeResponse(200, []), FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=600, sleep_fn=sleeps.append)

    fake_time = {"t": 1000.0}
    monkeypatch.setattr("gift_sniper.tonnel_client.time.monotonic", lambda: fake_time["t"])

    client.search(gift_name="A")
    fake_time["t"] += 0.1  # only 100ms elapsed -- well under 600ms
    client.search(gift_name="B")

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.5, abs=0.01)


def test_search_minimal_by_gift_ids_uses_in_filter_and_no_base_filter():
    """ДОПОЛНЕНИЕ, lifecycle fix: {"gift_id": {"$in": [...]}} is the
    CONFIRMED working format (gift_num $in returns HTTP 400). КАК
    ТЕСТИРОВАТЬ item 3: the sent filter must NOT contain buyer,
    refunded, export_at, or price.$exists -- only gift_id + asset.
    """
    session = FakeSession([FakeResponse(200, [_gift(1, 10.0), _gift(2, 12.0)])])
    client = TonnelClient(session=session, request_delay_ms=0)

    result = client.search_minimal_by_gift_ids([1001, 1002, 1003], limit=30)

    _url, body = session.calls[0]
    filter_dict = json.loads(body["filter"])
    assert filter_dict == {"gift_id": {"$in": [1001, 1002, 1003]}, "asset": "TON"}
    assert "gift_num" not in filter_dict
    assert "buyer" not in filter_dict
    assert "refunded" not in filter_dict
    assert "export_at" not in filter_dict
    assert "price" not in filter_dict
    assert body["limit"] == 30
    assert len(result) == 2


def test_search_minimal_by_gift_ids_passes_explicit_limit():
    """limit MUST be passed explicitly -- confirmed elsewhere in this
    client that Tonnel's default page size can silently truncate results.
    """
    session = FakeSession([FakeResponse(200, [])])
    client = TonnelClient(session=session, request_delay_ms=0)
    client.search_minimal_by_gift_ids([1, 2], limit=4)
    _url, body = session.calls[0]
    assert body["limit"] == 4


def test_search_minimal_by_gift_ids_empty_list_returns_empty_no_request():
    session = FakeSession([])
    client = TonnelClient(session=session, request_delay_ms=0)
    assert client.search_minimal_by_gift_ids([], limit=30) == []
    assert session.calls == []


def test_search_minimal_by_gift_ids_over_limit_raises_without_request():
    session = FakeSession([])
    client = TonnelClient(session=session, request_delay_ms=0)
    with pytest.raises(TonnelError):
        client.search_minimal_by_gift_ids(list(range(MAX_LIMIT + 1)), limit=30)
    assert session.calls == []


def test_search_minimal_by_gift_ids_limit_over_max_raises_without_request():
    session = FakeSession([])
    client = TonnelClient(session=session, request_delay_ms=0)
    with pytest.raises(TonnelError):
        client.search_minimal_by_gift_ids([1, 2], limit=MAX_LIMIT + 1)
    assert session.calls == []


def test_search_minimal_by_gift_ids_raises_tonnel_error_on_api_error_body():
    session = FakeSession([FakeResponse(200, {"error": "unsupported filter"})])
    client = TonnelClient(session=session, request_delay_ms=0)
    with pytest.raises(TonnelError):
        client.search_minimal_by_gift_ids([1, 2], limit=30)
