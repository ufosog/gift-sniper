"""MRKT access_token without the owner: a Telegram user session opens the
MRKT mini-app, takes its initData and exchanges it for a token.

How MRKT itself does it (read from its frontend, cdn.tgmrkt.io
js/auth-preflight-*.js, 2026-09-19):
    POST https://api.tgmrkt.io/api/v1/auth
    {"data": Telegram.WebApp.initData, "appId": null}
The server checks the initData signature: invalid initData -> HTTP 403
"Forbid 1" (measured).

initData comes from messages.requestAppWebView for the bot @mrkt, app
short name "app" (the mini-app link is t.me/mrkt/app). The returned URL
carries it in the fragment: #tgWebAppData=<initData>&tgWebAppVersion=...

UNCONFIRMED until the first live run: where the token is in the auth
response. MRKT's API reads `Cookie: access_token=...`, so the cookie is
tried first, then JSON fields token / accessToken / access_token. If none
is found the JSON field NAMES (never values) are logged.

Setup, once (owner's hands):
    1. api_id / api_hash from https://my.telegram.org -> API development tools
    2. python -m gift_sniper.mrkt_auth --login     (phone + code from Telegram)
Then:
    python -m gift_sniper.mrkt_auth --refresh       (the supervisor calls this)

The token is written to MRKT_TOKEN_FILE; config.get_mrkt_access_token()
reads that file first, so running processes pick up a new token without a
restart.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config

logger = logging.getLogger("gift_sniper.mrkt_auth")

MRKT_AUTH_URL = "https://api.tgmrkt.io/api/v1/auth"
MRKT_BOT = "mrkt"
MRKT_APP_SHORT_NAME = "app"
_TOKEN_JSON_FIELDS = ("token", "accessToken", "access_token")


class MrktAuthError(RuntimeError):
    pass


def session_configured() -> bool:
    return bool(config.TG_API_ID and config.TG_API_HASH and Path(config.TG_SESSION + ".session").exists())


def init_data_from_url(url: str) -> str:
    """initData from a web-app URL returned by requestAppWebView."""
    fragment = urlsplit(url).fragment
    values = parse_qs(fragment).get("tgWebAppData")
    if not values:
        raise MrktAuthError("tgWebAppData not in the web-app URL")
    return values[0]


async def _fetch_init_data_async(client) -> str:
    from telethon import functions, types, utils
    bot = await client.get_input_entity(MRKT_BOT)
    result = await client(functions.messages.RequestAppWebViewRequest(
        peer=bot,
        # bot_id takes an InputUser, not the InputPeerUser that peer takes.
        app=types.InputBotAppShortName(bot_id=utils.get_input_user(bot), short_name=MRKT_APP_SHORT_NAME),
        platform="android",
    ))
    return init_data_from_url(result.url)


def _telegram_client():
    from telethon import TelegramClient
    if not (config.TG_API_ID and config.TG_API_HASH):
        raise MrktAuthError("TG_API_ID / TG_API_HASH not set")
    Path(config.TG_SESSION).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(config.TG_SESSION, int(config.TG_API_ID), config.TG_API_HASH)


def fetch_init_data() -> str:
    async def run():
        client = _telegram_client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise MrktAuthError("Telegram session is not logged in: run --login")
            return await _fetch_init_data_async(client)
        finally:
            await client.disconnect()
    return asyncio.run(run())


def token_from_response(resp) -> str:
    cookie = resp.cookies.get("access_token") if getattr(resp, "cookies", None) is not None else None
    if cookie:
        return cookie
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        for field in _TOKEN_JSON_FIELDS:
            if isinstance(data.get(field), str) and data[field]:
                return data[field]
        logger.error("MRKT auth: no token found; response JSON fields: %s", sorted(data))
    raise MrktAuthError("no access_token in the MRKT auth response")


def exchange_init_data(init_data: str, session=None) -> str:
    if session is None:
        from curl_cffi import requests as curl_requests
        session = curl_requests.Session()
    resp = session.post(
        MRKT_AUTH_URL, json={"data": init_data, "appId": None},
        headers={"content-type": "application/json", "origin": "https://cdn.tgmrkt.io",
                 "referer": "https://cdn.tgmrkt.io/"},
        impersonate="chrome", timeout=20,
    )
    if resp.status_code != 200:
        raise MrktAuthError(f"MRKT auth HTTP {resp.status_code}: {resp.text[:100]}")
    return token_from_response(resp)


def write_token(token: str, path: str | None = None) -> None:
    """Atomic: a reader never sees a half-written file."""
    target = Path(path or config.MRKT_TOKEN_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    os.replace(tmp, target)


def refresh_token(fetch=fetch_init_data, exchange=exchange_init_data) -> datetime:
    """New token into MRKT_TOKEN_FILE. Returns the time it was issued.
    Never logs the token or the initData."""
    token = exchange(fetch())
    write_token(token)
    issued = datetime.now(timezone.utc)
    logger.info("MRKT token refreshed at %s", issued.isoformat())
    return issued


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MRKT token from a Telegram session")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--login", action="store_true", help="first login of the Telegram session (interactive)")
    group.add_argument("--refresh", action="store_true", help="get a new MRKT token")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if args.login:
        client = _telegram_client()
        client.start()  # asks for the phone number and the code
        me = client.loop.run_until_complete(client.get_me())
        client.disconnect()
        print(f"Сессия сохранена: {config.TG_SESSION}.session (аккаунт id {me.id})")
        return 0
    refresh_token()
    print(f"Токен MRKT обновлён: {config.MRKT_TOKEN_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
