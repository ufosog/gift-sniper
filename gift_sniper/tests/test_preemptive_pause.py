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

    def get(self, url, params=None, headers=None, timeout=None):
        return self._responses.pop(0)


def test_zero_remaining_triggers_extra_pause_before_next_request():
    responses = [
        FakeResponse(200, headers={"x-ratelimit-remaining": "0"}, json_body={"a": 1}),
        FakeResponse(200, headers={"x-ratelimit-remaining": "5"}, json_body={"b": 2}),
    ]
    session = FakeSession(responses)
    sleeps: list[float] = []

    client = PortalsClient(
        auth_provider=lambda: "tok",
        session=session,
        sleep_fn=lambda s: sleeps.append(s),
        request_delay_ms=100,
    )

    client.search(limit=50, offset=0)
    assert client.preemptive_pause_count == 1

    sleeps.clear()
    client.search(limit=50, offset=0)
    # Normal throttle sleep (~0.1s) PLUS the extra pending pause (~0.1s)
    # must both have been recorded before the second request went out.
    assert len(sleeps) == 2
    assert client.preemptive_pause_count == 1  # not incremented again this time
