"""MRKT token refresh: initData parsing, token extraction, the token file
reaching running clients without a restart."""
import os
import time

import pytest

from gift_sniper import config, mrkt_auth


class Resp:
    def __init__(self, status=200, cookies=None, json_data=None):
        self.status_code = status
        self.cookies = cookies or {}
        self._json = json_data
        self.text = "x"

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def test_init_data_from_url_fragment():
    url = ("https://cdn.tgmrkt.io/#tgWebAppData=query_id%3DAA%26user%3D%257B%2522id%2522%253A1%257D"
           "%26auth_date%3D1758000000%26hash%3Dabc&tgWebAppVersion=8.0&tgWebAppPlatform=android")
    assert mrkt_auth.init_data_from_url(url) == (
        "query_id=AA&user=%7B%22id%22%3A1%7D&auth_date=1758000000&hash=abc")


def test_init_data_missing_raises():
    with pytest.raises(mrkt_auth.MrktAuthError):
        mrkt_auth.init_data_from_url("https://cdn.tgmrkt.io/#tgWebAppVersion=8.0")


@pytest.mark.parametrize("resp,expected", [
    (Resp(cookies={"access_token": "cookie-tok"}, json_data={"token": "json-tok"}), "cookie-tok"),
    (Resp(json_data={"token": "t1"}), "t1"),
    (Resp(json_data={"accessToken": "t2"}), "t2"),
    (Resp(json_data={"access_token": "t3"}), "t3"),
])
def test_token_from_response(resp, expected):
    assert mrkt_auth.token_from_response(resp) == expected


def test_token_missing_logs_field_names_not_values(caplog):
    with pytest.raises(mrkt_auth.MrktAuthError):
        mrkt_auth.token_from_response(Resp(json_data={"user": "secret-value", "id": 5}))
    assert "['id', 'user']" in caplog.text
    assert "secret-value" not in caplog.text


def test_exchange_posts_init_data_with_mrkt_origin():
    calls = []

    class Session:
        def post(self, url, json, headers, impersonate, timeout):
            calls.append((url, json, headers["origin"]))
            return Resp(json_data={"token": "tok"})

    assert mrkt_auth.exchange_init_data("INIT", session=Session()) == "tok"
    assert calls == [(mrkt_auth.MRKT_AUTH_URL, {"data": "INIT", "appId": None}, "https://cdn.tgmrkt.io")]


def test_exchange_http_error_raises():
    class Session:
        def post(self, *a, **k):
            return Resp(status=403)

    with pytest.raises(mrkt_auth.MrktAuthError):
        mrkt_auth.exchange_init_data("INIT", session=Session())


def test_refreshed_token_reaches_config_without_restart(tmp_path, monkeypatch):
    path = tmp_path / "secrets" / "mrkt_token.txt"
    monkeypatch.setattr(config, "MRKT_TOKEN_FILE", str(path))
    monkeypatch.setattr(config, "_mrkt_token_cache", None)
    monkeypatch.setenv("MRKT_ACCESS_TOKEN", "env-token")
    assert config.get_mrkt_access_token() == "env-token"  # no file yet

    mrkt_auth.refresh_token(fetch=lambda: "INIT", exchange=lambda init: "first")
    assert config.get_mrkt_access_token() == "first"
    time.sleep(0.05)
    mrkt_auth.write_token("second")
    os.utime(path, (time.time() + 5, time.time() + 5))  # coarse mtime on some filesystems
    assert config.get_mrkt_access_token() == "second"


def test_supervisor_skips_refresh_without_session(monkeypatch):
    from gift_sniper import supervisor
    monkeypatch.setattr(mrkt_auth, "session_configured", lambda: False)
    sup = supervisor.Supervisor([])
    assert sup.refresh_mrkt_token("test") is False


def test_fetch_builds_valid_telethon_request():
    """Builds the real Telethon request objects against a fake client, so a
    wrong type (InputPeerUser where InputUser is required) fails here."""
    import asyncio
    from telethon.tl.types import InputPeerUser

    captured = {}

    class FakeClient:
        async def get_input_entity(self, name):
            assert name == "mrkt"
            return InputPeerUser(user_id=42, access_hash=7)

        async def __call__(self, request):
            captured["req"] = request
            request._bytes()  # serialization validates every field type

            class R:
                url = "https://cdn.tgmrkt.io/#tgWebAppData=query_id%3DQ%26hash%3Dh&tgWebAppVersion=8.0"
            return R()

    assert asyncio.run(mrkt_auth._fetch_init_data_async(FakeClient())) == "query_id=Q&hash=h"
    assert captured["req"].app.short_name == "app"
    assert captured["req"].app.bot_id.user_id == 42
