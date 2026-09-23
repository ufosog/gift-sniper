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


class FakeSession:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls += 1
        return FakeResponse(200, json_body={"ok": True})


def test_different_paths_do_not_wait_on_each_other():
    session = FakeSession()
    sleeps: list[float] = []
    client = PortalsClient(
        auth_provider=lambda: "tok",
        session=session,
        sleep_fn=lambda s: sleeps.append(s),
        request_delay_ms=10_000,  # huge delay -- would obviously show up if shared
    )

    client.search(limit=50, offset=0)
    sleeps.clear()
    client.model_backgrounds_floors(["ModelA"])

    # Different path -- no throttle sleep should have been triggered even
    # though request_delay_ms is huge and the previous /nfts/search call
    # just happened.
    assert sleeps == []


def test_same_path_twice_waits():
    session = FakeSession()
    sleeps: list[float] = []
    client = PortalsClient(
        auth_provider=lambda: "tok",
        session=session,
        sleep_fn=lambda s: sleeps.append(s),
        request_delay_ms=10_000,
    )

    client.search(limit=50, offset=0)
    sleeps.clear()
    client.search(limit=50, offset=50)

    # Same path, second call -- must wait ~10s (10_000ms).
    assert len(sleeps) == 1
    assert sleeps[0] > 5.0
