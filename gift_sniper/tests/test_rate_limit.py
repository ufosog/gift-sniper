from gift_sniper.portals_client import PortalsClient


class FakeResponse:
    def __init__(self, status_code, headers=None, json_body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._json_body = json_body or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return self._responses.pop(0)


def test_429_with_retry_after_waits_exact_duration():
    responses = [
        FakeResponse(429, headers={"Retry-After": "2"}),
        FakeResponse(200, json_body={"results": [], "total_count": 0}),
    ]
    session = FakeSession(responses)
    sleeps: list[float] = []

    client = PortalsClient(
        auth_provider=lambda: "tok",
        session=session,
        sleep_fn=lambda s: sleeps.append(s),
        request_delay_ms=0,
    )

    result = client.search(limit=50, offset=0)

    assert result == {"results": [], "total_count": 0}
    assert client.rate_limited_count == 1
    # First sleep call before the request is throttling (0ms here -> not
    # necessarily recorded since remaining<=0), the retry-after wait must
    # be exactly 2.0 seconds and must appear in the recorded sleeps.
    assert 2.0 in sleeps


def test_429_without_retry_after_uses_exponential_backoff():
    responses = [
        FakeResponse(429, headers={}),
        FakeResponse(200, json_body={"ok": True}),
    ]
    session = FakeSession(responses)
    sleeps: list[float] = []

    client = PortalsClient(
        auth_provider=lambda: "tok",
        session=session,
        sleep_fn=lambda s: sleeps.append(s),
        request_delay_ms=0,
    )

    client.search(limit=50, offset=0)

    assert client.rate_limited_count == 1
    assert 2.0 in sleeps  # base backoff on first attempt


def test_429_wait_source_priority_retry_after_wins():
    responses = [
        FakeResponse(429, headers={"Retry-After": "3", "x-ratelimit-reset": "9"}),
        FakeResponse(200, json_body={}),
    ]
    session = FakeSession(responses)
    sleeps: list[float] = []
    client = PortalsClient(
        auth_provider=lambda: "tok", session=session,
        sleep_fn=lambda s: sleeps.append(s), request_delay_ms=0,
    )
    client.search(limit=50, offset=0)
    assert sleeps == [3.0]


def test_429_wait_source_priority_ratelimit_reset_when_no_retry_after():
    responses = [
        FakeResponse(429, headers={"x-ratelimit-reset": "5"}),
        FakeResponse(200, json_body={}),
    ]
    session = FakeSession(responses)
    sleeps: list[float] = []
    client = PortalsClient(
        auth_provider=lambda: "tok", session=session,
        sleep_fn=lambda s: sleeps.append(s), request_delay_ms=0,
    )
    client.search(limit=50, offset=0)
    assert sleeps == [5.0]


def test_429_wait_source_priority_backoff_when_neither_present():
    responses = [
        FakeResponse(429, headers={"x-ratelimit-reset": "0"}),  # reset=0, not usable
        FakeResponse(200, json_body={}),
    ]
    session = FakeSession(responses)
    sleeps: list[float] = []
    client = PortalsClient(
        auth_provider=lambda: "tok", session=session,
        sleep_fn=lambda s: sleeps.append(s), request_delay_ms=0,
    )
    client.search(limit=50, offset=0)
    assert sleeps == [2.0]  # base exponential backoff, attempt 0
