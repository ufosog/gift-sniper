from gift_sniper.portals_client import PortalsClient


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
        self.params_seen: list[object] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.urls.append(url)
        self.params_seen.append(params)
        return FakeResponse(200, json_body={"results": []})


def test_search_pair_floor_puts_sort_first_in_the_url():
    session = UrlCapturingSession()
    client = PortalsClient(auth_provider=lambda: "tok", session=session, request_delay_ms=0)

    client.search_pair_floor("col-123", "Emperor", "Black")

    assert len(session.urls) == 1
    url = session.urls[0]
    assert "?sort=price_asc" in url or url.split("?", 1)[1].startswith("sort=price_asc")
    # No separate params= dict was passed -- the query is embedded
    # verbatim in the URL, so nothing downstream could reorder it.
    assert session.params_seen[0] is None


def test_search_pair_floor_param_order_matches_spec_exactly():
    session = UrlCapturingSession()
    client = PortalsClient(auth_provider=lambda: "tok", session=session, request_delay_ms=0)

    client.search_pair_floor("col-123", "Emperor", "Black", limit=20, offset=0)

    query = session.urls[0].split("?", 1)[1]
    keys_in_order = [pair.split("=")[0] for pair in query.split("&")]
    assert keys_in_order == [
        "sort", "limit", "offset", "collection_id", "filter_by_models", "filter_by_backdrops",
    ]


def test_search_pair_floor_url_encodes_spaces_as_percent20():
    session = UrlCapturingSession()
    client = PortalsClient(auth_provider=lambda: "tok", session=session, request_delay_ms=0)

    client.search_pair_floor("col-123", "Golden Emperor", "Onyx Black")

    url = session.urls[0]
    assert "Golden%20Emperor" in url
    assert "Onyx%20Black" in url
    assert "+" not in url.split("?", 1)[1]  # never the '+'-for-space form


def test_search_by_ids_50_ids_puts_limit_50_in_url():
    """Правка 2: querying 50 ids at once must send limit=50 -- without an
    explicit limit, the server's default page size can silently truncate
    the result set below the number of ids requested.
    """
    session = UrlCapturingSession()
    client = PortalsClient(auth_provider=lambda: "tok", session=session, request_delay_ms=0)

    ids = [f"id-{i}" for i in range(50)]
    client.search_by_ids(ids, limit=50)

    url = session.urls[0]
    assert "limit=50" in url


def test_search_by_ids_defaults_limit_to_number_of_ids_when_omitted():
    session = UrlCapturingSession()
    client = PortalsClient(auth_provider=lambda: "tok", session=session, request_delay_ms=0)

    ids = [f"id-{i}" for i in range(7)]
    client.search_by_ids(ids)

    url = session.urls[0]
    assert "limit=7" in url
