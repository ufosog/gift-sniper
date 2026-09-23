import json

from gift_sniper import config
from gift_sniper.mrkt_client import MAX_COUNT, MrktClient, MrktError, MrktFloor, build_default_mrkt_client


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
        self.calls: list[tuple[str, dict, dict]] = []  # (url, json_body, headers)

    def post(self, url, json=None, headers=None, impersonate=None, timeout=None):
        self.calls.append((url, json, headers))
        return self._responses.pop(0)


def _gift(number, sale_price_ton, isOnSale=True, isOnAuction=False, isLocked=False,
          isLockedForSale=False, premarketStatus="None"):
    """`sale_price_ton`: a human-readable TON amount (e.g. 20.0) --
    converted to a nano-TON INT here, matching the real API's confirmed
    wire format (salePrice is already an int in nano-TON, e.g.
    16289400000 == 16.29 TON -- see mrkt_client.py's fixed unit bug).
    """
    sale_price_nano = int(round(sale_price_ton * int(config.NANO)))
    return {
        "id": f"uuid-{number}",
        "name": f"SnoopCigar-{number}",
        "number": number,
        "collectionName": "Snoop Cigar",
        "modelName": "Classic",
        "backdropName": "Black",
        "symbolName": "Star",
        "salePrice": sale_price_nano,
        "salePriceWithoutFee": int(round(sale_price_nano / 1.02)),
        "isOnSale": isOnSale,
        "isOnAuction": isOnAuction,
        "isLocked": isLocked,
        "isLockedForSale": isLockedForSale,
        "salesCount": 0,
        "premarketStatus": premarketStatus,
        "floorPriceNanoTONsByCollection": None,
        "floorPriceNanoTONsByBackdropModel": None,
    }


def _client(session):
    return MrktClient(token_provider=lambda: "tok-1234", session=session, request_delay_ms=0)


# --- item 1: Cookie header used, not Authorization --------------------


def test_pair_floor_sends_cookie_not_authorization():
    session = FakeSession([FakeResponse(200, {"gifts": [], "cursor": "", "total": 0})])
    client = _client(session)
    client.pair_floor("Snoop Cigar", "Classic", "Black")

    _url, _body, headers = session.calls[0]
    assert headers["cookie"] == "access_token=tok-1234"
    assert "Authorization" not in headers
    assert "authorization" not in headers


def test_pair_floor_uses_cdn_origin_and_referer_not_api():
    session = FakeSession([FakeResponse(200, {"gifts": [], "cursor": "", "total": 0})])
    client = _client(session)
    client.pair_floor("Snoop Cigar", "Classic", "Black")

    _url, _body, headers = session.calls[0]
    assert headers["origin"] == "https://cdn.tgmrkt.io"
    assert headers["referer"] == "https://cdn.tgmrkt.io/"


# --- item 2: full request body, count never exceeds 20 -----------------


def test_pair_floor_body_has_all_required_fields_and_count_at_most_20():
    session = FakeSession([FakeResponse(200, {"gifts": [], "cursor": "", "total": 0})])
    client = _client(session)
    client.pair_floor("Snoop Cigar", "Classic", "Black")

    _url, body, _headers = session.calls[0]
    for field in (
        "availableForStaking", "backdropNames", "collectionNames", "count", "craftable",
        "cursor", "forGame", "giftType", "isCrafted", "isNew", "isPremarket",
        "isTransferable", "lowToHigh", "luckyBuy", "maxPrice", "minPrice", "modelNames",
        "number", "ordering", "query", "removeSelfSales", "symbolNames", "tgCanBeCraftedFrom",
    ):
        assert field in body, field
    assert body["count"] <= MAX_COUNT
    assert body["collectionNames"] == ["Snoop Cigar"]
    assert body["modelNames"] == ["Classic"]
    assert body["backdropNames"] == ["Black"]


def test_ordering_none_date_and_latest_never_sent():
    """КАК ТЕСТИРОВАТЬ (spec): ordering="Date"/"Latest"/"CreatedAt" -> 400,
    never send them -- this client only ever sends "None"/"Price"/"Number".
    """
    session = FakeSession([FakeResponse(200, {"gifts": [], "cursor": "", "total": 0})])
    client = _client(session)
    client.pair_floor("Snoop Cigar", "Classic", "Black")
    _url, body, _headers = session.calls[0]
    assert body["ordering"] in ("None", "Price", "Number")


# --- item 3: auction lots excluded --------------------------------------


def test_pair_floor_excludes_auction_locked_premarket_and_not_on_sale():
    gifts = [
        _gift(1, 10.0, isOnAuction=True),  # excluded
        _gift(2, 5.0, isLocked=True),  # excluded
        _gift(3, 6.0, isLockedForSale=True),  # excluded
        _gift(4, 4.0, premarketStatus="Active"),  # excluded
        _gift(5, 3.0, isOnSale=False),  # excluded
        _gift(6, 20.0),  # usable -- the real floor
    ]
    session = FakeSession([FakeResponse(200, {"gifts": gifts, "cursor": "", "total": 6})])
    client = _client(session)
    floor = client.pair_floor("Snoop Cigar", "Classic", "Black")

    assert floor.status == "ok"
    assert floor.floor_nano == int(20.0 * config.NANO)


# --- item 4: salePrice taken as-is, no multiplication -------------------


def test_pair_floor_price_taken_as_is_no_fee_multiplication():
    gifts = [_gift(1, 50.0)]
    session = FakeSession([FakeResponse(200, {"gifts": gifts, "cursor": "", "total": 1})])
    client = _client(session)
    floor = client.pair_floor("Snoop Cigar", "Classic", "Black")

    assert floor.floor_nano == int(50.0 * config.NANO)  # NOT *1.02, NOT *1.1


def test_mrkt_floor_dataclass_has_no_fee_field():
    """Unlike TonnelFloor (floor_nano + floor_with_fee_nano), MrktFloor
    has only ONE price field -- there is no separate "with fee" variant,
    since salePrice already includes the fee.
    """
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(MrktFloor)}
    assert field_names == {"floor_nano", "listed_count", "status", "raw"}


# --- item 9: listed_count from `total`, not len(gifts) -------------------


def test_listed_count_comes_from_total_field_not_len_gifts():
    gifts = [_gift(1, 10.0)]  # only 1 in the page
    session = FakeSession([FakeResponse(200, {"gifts": gifts, "cursor": "", "total": 137})])
    client = _client(session)
    floor = client.pair_floor("Snoop Cigar", "Classic", "Black")

    assert floor.listed_count == 137
    assert floor.listed_count != len(gifts)


# --- self-exclusion by number --------------------------------------------


def test_pair_floor_excludes_self_by_number():
    gifts = [_gift(1, 5.0), _gift(2, 20.0)]
    session = FakeSession([FakeResponse(200, {"gifts": gifts, "cursor": "", "total": 2})])
    client = _client(session)
    floor = client.pair_floor("Snoop Cigar", "Classic", "Black", exclude_number=1)

    assert floor.floor_nano == int(20.0 * config.NANO)


# --- no usable listings -> no_data ---------------------------------------


def test_pair_floor_no_usable_listings_gives_no_data():
    gifts = [_gift(1, 10.0, isOnAuction=True)]
    session = FakeSession([FakeResponse(200, {"gifts": gifts, "cursor": "", "total": 1})])
    client = _client(session)
    floor = client.pair_floor("Snoop Cigar", "Classic", "Black")

    assert floor.status == "no_data"
    assert floor.floor_nano is None
    assert floor.listed_count == 1  # still reports total, per spec item 9


def test_pair_floor_empty_response_gives_no_data():
    session = FakeSession([FakeResponse(200, {"gifts": [], "cursor": "", "total": 0})])
    client = _client(session)
    floor = client.pair_floor("Snoop Cigar", "Classic", "Black")

    assert floor.status == "no_data"
    assert floor.listed_count == 0


# --- errors -----------------------------------------------------------


def test_non_200_raises_mrkt_error():
    session = FakeSession([FakeResponse(401, text="unauthorized")])
    client = _client(session)
    try:
        client.pair_floor("Snoop Cigar", "Classic", "Black")
        assert False, "must raise"
    except MrktError as exc:
        assert exc.status_code == 401


def test_non_json_response_raises_mrkt_error():
    session = FakeSession([FakeResponse(200, body=None, text="<html>not json</html>")])
    client = _client(session)
    try:
        client.pair_floor("Snoop Cigar", "Classic", "Black")
        assert False, "must raise"
    except MrktError:
        pass


# --- find_by_number -------------------------------------------------------


def test_find_by_number_returns_the_gift():
    session = FakeSession([FakeResponse(200, {"gifts": [_gift(62464, 30.0)], "cursor": "", "total": 1})])
    client = _client(session)
    gift = client.find_by_number("Lunar Snake", 62464)
    assert gift["number"] == 62464
    assert gift["name"] == "SnoopCigar-62464"


def test_find_by_number_returns_none_when_not_found():
    session = FakeSession([FakeResponse(200, {"gifts": [], "cursor": "", "total": 0})])
    client = _client(session)
    gift = client.find_by_number("Lunar Snake", 999999)
    assert gift is None


# --- build_default_mrkt_client: token-optional wiring --------------------


def test_build_default_mrkt_client_returns_none_when_token_unset(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 8: no MRKT_ACCESS_TOKEN -> None, no crash."""
    import gift_sniper.mrkt_client as mrkt_module

    monkeypatch.delenv("MRKT_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(mrkt_module, "_logged_missing_token", False)
    # The token can also come from MRKT_TOKEN_FILE: point it at a path that
    # does not exist, so the test never depends on the machine's own files.
    monkeypatch.setattr(config, "MRKT_TOKEN_FILE", "no-such-token-file")
    monkeypatch.setattr(config, "_mrkt_token_cache", None)

    client = build_default_mrkt_client()
    assert client is None


def test_build_default_mrkt_client_returns_client_when_token_set(monkeypatch):
    monkeypatch.setenv("MRKT_ACCESS_TOKEN", "real-token")
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)

    client = build_default_mrkt_client()
    assert isinstance(client, MrktClient)


def test_build_default_mrkt_client_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("MRKT_ACCESS_TOKEN", "real-token")
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", False)

    client = build_default_mrkt_client()
    assert client is None


# --- unit-conversion bug fix: salePrice is ALREADY nano-TON -------------


def test_sale_price_already_nano_no_double_conversion():
    """КАК ТЕСТИРОВАТЬ item 1 (bug fix): a real measured example --
    salePrice=16289400000 (== 16.29 TON) -> floor_nano=16289400000
    exactly, NOT 1.6e19 (the old bug's *config.NANO double-conversion).
    """
    gift = {
        "id": "uuid-1", "name": "LunarSnake-1", "number": 1,
        "collectionName": "Snoop Cigar", "modelName": "Classic", "backdropName": "Black",
        "symbolName": "Star", "salePrice": 16289400000, "salePriceWithoutFee": 15970000000,
        "isOnSale": True, "isOnAuction": False, "isLocked": False, "isLockedForSale": False,
        "salesCount": 0, "premarketStatus": "None",
        "floorPriceNanoTONsByCollection": None, "floorPriceNanoTONsByBackdropModel": None,
    }
    session = FakeSession([FakeResponse(200, {"gifts": [gift], "cursor": "", "total": 1})])
    client = _client(session)
    floor = client.pair_floor("Snoop Cigar", "Classic", "Black")

    assert floor.floor_nano == 16289400000
    assert floor.floor_nano != 16289400000 * config.NANO


# --- feed() (MRKT full-signaller delivery) -------------------------------


def test_feed_sends_count_and_cursor_and_returns_items_and_cursor():
    """КАК ТЕСТИРОВАТЬ item 1: feed() sends count<=20 and returns a cursor."""
    session = FakeSession([FakeResponse(200, {"items": [], "cursor": "next-cursor-uuid"})])
    client = _client(session)
    items, cursor = client.feed(count=20, cursor="")

    _url, body, _headers = session.calls[0]
    assert body["count"] == 20
    assert body["cursor"] == ""
    assert items == []
    assert cursor == "next-cursor-uuid"


def test_feed_uses_the_feed_url_not_the_saling_url():
    from gift_sniper.mrkt_client import MRKT_API_URL, MRKT_FEED_URL

    session = FakeSession([FakeResponse(200, {"items": [], "cursor": ""})])
    client = _client(session)
    client.feed()

    url, _body, _headers = session.calls[0]
    assert url == MRKT_FEED_URL
    assert url != MRKT_API_URL


def test_feed_amount_not_multiplied():
    """КАК ТЕСТИРОВАТЬ item 7: amount is not domultiplied by 10**9 --
    this is a pure passthrough client method, so the caller (mrkt_poller.py)
    is responsible, but confirm the raw value survives untouched.
    """
    event = {
        "type": "listing", "id": "evt-1", "amount": 16289400000,
        "date": "2026-09-12T07:13:33Z", "gift": {"id": "uuid-1"},
    }
    session = FakeSession([FakeResponse(200, {"items": [event], "cursor": "c1"})])
    client = _client(session)
    items, _cursor = client.feed()

    assert items[0]["amount"] == 16289400000


def test_feed_propagates_mrkt_error_on_failure():
    session = FakeSession([FakeResponse(500, text="server error")])
    client = _client(session)
    try:
        client.feed()
        assert False, "must raise"
    except MrktError:
        pass
