"""MRKT (tgmrkt.io) API client -- a SEPARATE module from
tonnel_client.py/portals_client.py by design, per spec: a third,
independently-evolving marketplace protocol, so this does NOT subclass
or reuse either existing client.

MRKT was originally added as a cross-check NEIGHBOUR ONLY (see
cross_check.py), via `pair_floor()`/`find_by_number()` below, which
query /gifts/saling -- confirmed live that THIS endpoint's `ordering`
has no reliable freshness order at all (`ordering="None"` gave an
UNSTABLE order, all 20 of the first page's lots changed within 60
seconds, and the first three `receivedDate` values were 2026-09-12
08:50, 2026-09-12 10:21, and 2026-09-10 13:45 -- NOT freshness-ordered;
`isNew=true` returned a lot from 2026-08-29). /gifts/saling is NEVER
used for collection -- see mrkt_poller.py.

A SEPARATE endpoint, /api/v1/feed (`feed()` below), was later found and
confirmed to be a real, strictly chronological event stream -- THIS is
what mrkt_poller.py uses to make MRKT a full third signaller (listing/
change_price/sale events), not just a cross-check neighbour. The two
endpoints are unrelated in this client: /gifts/saling's randomness does
not apply to /feed's confirmed clean, gap-free, cursor-paginated,
date-descending order.

ALL facts below are confirmed by live requests, not assumed:
- POST https://api.tgmrkt.io/api/v1/gifts/saling
- Auth is a `Cookie: access_token=<uuid>` header -- CONFIRMED the
  `Authorization` header does NOT authorize (a request with
  Authorization but no Cookie got 401; Cookie alone, no Authorization,
  got 200). Token comes from env MRKT_ACCESS_TOKEN.
- Requires curl_cffi with impersonate="chrome" (Cloudflare).
- `origin`/`referer` MUST be https://cdn.tgmrkt.io (NOT api.tgmrkt.io) --
  confirmed the wrong origin is rejected.
- Request body is a FULL, fixed-shape object -- every field below is
  required (confirmed omitting fields is not equivalent to the API's
  own defaults for this endpoint).
- `ordering`: only "None", "Price", "Number" work -- confirmed "Date",
  "Latest", "CreatedAt" all return HTTP 400.
- `count`: hard-capped at 20 server-side -- confirmed 50 and 100 are
  silently truncated to 20, not rejected.
- `cursor`: cursor-based pagination (an opaque uuid-like string in the
  response), NOT an offset.
- collectionNames/modelNames/backdropNames are arrays of EXACT strings,
  no rarity percentage baked in (unlike Tonnel's regex-matched names).
- `salePrice` / `salePriceWithoutFee` = 1.0200 exactly on every lot
  checked -- `salePrice` is the BUYER's total (2% fee already included).
  Compared AS-IS, never multiplied by anything -- this is a DIFFERENT
  fee convention from Tonnel (raw `price` needs *1.1 there) and from
  Portals (raw listed price, no buyer fee at all).
- `floorPriceNanoTONsByBackdropModel` is ALWAYS null in the feed
  (confirmed on 100 lots) -- never used; the floor here is computed the
  same self-excluded-minimum way as every other marketplace in this
  project, from a live filtered query.
- 10 back-to-back requests, 10/10 succeeded, ~0.84s/request, no rate
  limit observed -- MRKT_REQUEST_DELAY_MS is still applied
  unconditionally, same "полит" discipline as every other client here.
- /api/v1/feed: `{"count": 20, "cursor": ""}` -> `{"items": [...],
  "cursor": "<uuid>"}`. Each item: `type` ("listing"/"sale"/
  "change_price"), `id` (event id, used for dedup), `amount` (nano-TON
  int, NOT converted), `date` (ISO8601 UTC), `gift` (the full lot
  object, same shape as /gifts/saling's items). Confirmed live: count
  capped at 20 (same as /gifts/saling); pagination via cursor has ZERO
  overlap between pages; order is strictly reverse-chronological
  (07:13:33, :32, :29, ... measured); at a 30s poll interval the first
  page's 20 events fully turned over within 30s -- events are
  guaranteed lost at that interval (see MRKT_POLL_INTERVAL_SEC=8s);
  ~1.2 events/sec measured; sample-of-20 type distribution: listing 10,
  sale 7, change_price 3. `change_price` events carry NO old price,
  only the new `amount` -- the old price must come from this project's
  own DB. `listing` events have `salePriceWithoutFee=0` even though
  `salePrice` is filled -- `salePrice` (== `amount`) is still the
  correct total to use, per the confirmed 2% fee-already-included
  convention.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from curl_cffi import requests as curl_requests

from . import config

logger = logging.getLogger("gift_sniper.mrkt_client")

MRKT_API_URL = "https://api.tgmrkt.io/api/v1/gifts/saling"
MRKT_FEED_URL = "https://api.tgmrkt.io/api/v1/feed"
MAX_COUNT = 20  # confirmed live: 50/100 silently truncated to 20

# Confirmed live: this exact body shape is required -- every field
# present, even when null/empty. Per-call fields (collectionNames,
# modelNames, backdropNames, number, count, cursor, ordering, lowToHigh)
# are merged in on top of this template.
_BODY_TEMPLATE: dict = {
    "availableForStaking": None,
    "backdropNames": [],
    "collectionNames": [],
    "count": 20,
    "craftable": None,
    "cursor": "",
    "forGame": None,
    "giftType": None,
    "isCrafted": None,
    "isNew": None,
    "isPremarket": None,
    "isTransferable": None,
    "lowToHigh": False,
    "luckyBuy": None,
    "maxPrice": None,
    "minPrice": None,
    "modelNames": [],
    "number": None,
    "ordering": "None",
    "query": None,
    "removeSelfSales": None,
    "symbolNames": [],
    "tgCanBeCraftedFrom": None,
}


def _headers(access_token: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        # CONFIRMED: Cookie is what actually authorizes -- Authorization
        # does nothing here (see module docstring). Never send a bare
        # Authorization header expecting it to work.
        "cookie": f"access_token={access_token}",
        # CONFIRMED: must be cdn.tgmrkt.io, NOT api.tgmrkt.io.
        "origin": "https://cdn.tgmrkt.io",
        "referer": "https://cdn.tgmrkt.io/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }


class MrktError(RuntimeError):
    """Raised on any request failure, non-200 status, non-JSON body, or
    an unexpected response shape. Same discipline as TonnelError/
    PortalsError -- callers never receive a silently empty result for a
    genuine failure.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class MrktFloor:
    """Result of pair_floor() -- the SELF-EXCLUDED minimum salePrice for
    one exact (collection, model, backdrop) combination on MRKT.
    `listed_count` comes from the response's own `total` field (the
    depth of the book under this filter, server-computed, NOT
    len(gifts) -- confirmed the feed page is capped at 20 while `total`
    can be far larger).
    """
    floor_nano: int | None  # minimum salePrice, in nano-TON, self-excluded, AS-IS (2% fee already included)
    listed_count: int  # from response["total"]
    status: str  # "ok" | "no_data" | "error"
    raw: list = field(default_factory=list)


class MrktClient:
    def __init__(
        self,
        token_provider,
        sleep_fn=time.sleep,
        request_delay_ms: int = 600,
        session=None,
    ):
        """`token_provider`: callable() -> str, returns the current
        MRKT_ACCESS_TOKEN value -- same indirection style as
        portals_client.PortalsClient(auth_provider=...), lets the token
        be swapped without this class knowing how.

        `session` is injectable so tests can supply a fake with a
        `.post(url, json=..., headers=..., impersonate=..., timeout=...)`
        method, without touching curl_cffi/the network -- same
        discipline as tonnel_client.TonnelClient(session=...).
        """
        self._token_provider = token_provider
        self._sleep = sleep_fn
        self._request_delay_ms = request_delay_ms
        self._session = session or curl_requests.Session()
        self._last_request_mono: float | None = None

    def _throttle(self) -> None:
        if self._last_request_mono is not None:
            elapsed_ms = (time.monotonic() - self._last_request_mono) * 1000
            remaining_ms = self._request_delay_ms - elapsed_ms
            if remaining_ms > 0:
                self._sleep(remaining_ms / 1000)
        self._last_request_mono = time.monotonic()

    def _post(self, body: dict) -> dict:
        token = self._token_provider()
        self._throttle()
        try:
            resp = self._session.post(
                MRKT_API_URL, json=body, headers=_headers(token), impersonate="chrome", timeout=15
            )
        except Exception as exc:  # curl_cffi raises its own exception types
            raise MrktError(f"request failed: {exc}") from exc

        if resp.status_code != 200:
            raise MrktError(f"unexpected status {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)

        try:
            data = resp.json()
        except ValueError as exc:
            raise MrktError(f"non-JSON response: {exc}") from exc

        if not isinstance(data, dict) or "gifts" not in data:
            raise MrktError(f"unexpected response shape (expected a dict with 'gifts'): {type(data).__name__}")

        return data

    def pair_floor(
        self,
        collection_name: str,
        model_name: str,
        backdrop_name: str,
        exclude_number: int | None = None,
    ) -> MrktFloor:
        """Self-excluded minimum salePrice for one exact (collection,
        model, backdrop) combination. Drops:
          - isOnSale=false (not actually purchasable);
          - isOnAuction=true (a different trading mechanic, not an
            ordinary buy);
          - isLocked=true / isLockedForSale=true (not currently sellable);
          - premarketStatus != "None" (a pre-market lot);
          - the lot matching exclude_number, if given (never compare a
            signal against itself).
        Sorted ascending (ordering="Price", lowToHigh=True) so the
        cheapest usable lot is near the front of the first (and only,
        count<=20) page fetched -- same "good enough without full
        pagination" discipline as every other pair_floor()-shaped method
        in this project.
        """
        body = dict(_BODY_TEMPLATE)
        body.update(
            collectionNames=[collection_name],
            modelNames=[model_name],
            backdropNames=[backdrop_name],
            count=MAX_COUNT,
            ordering="Price",
            lowToHigh=True,
        )
        return self._floor_from_body(body, exclude_number)

    def model_floor(
        self,
        collection_name: str,
        model_name: str,
        exclude_number: int | None = None,
    ) -> MrktFloor:
        """Self-excluded minimum salePrice across a whole MODEL (collection +
        model, NO backdrop filter). Used by cross_check.py only as a LOWER
        BOUND on the pair price when the pair itself isn't listed -- a
        model floor is the minimum over all backdrops, so it can never
        exceed the pair floor. Same filters as pair_floor().
        """
        body = dict(_BODY_TEMPLATE)
        body.update(
            collectionNames=[collection_name],
            modelNames=[model_name],
            count=MAX_COUNT,
            ordering="Price",
            lowToHigh=True,
        )
        return self._floor_from_body(body, exclude_number)

    def _floor_from_body(self, body: dict, exclude_number: int | None) -> MrktFloor:
        data = self._post(body)
        gifts = data.get("gifts", [])
        total = data.get("total", 0) or 0

        usable = []
        for item in gifts:
            if item.get("isOnSale") is False:
                continue
            if item.get("isOnAuction"):
                continue
            if item.get("isLocked"):
                continue
            if item.get("isLockedForSale"):
                continue
            if item.get("premarketStatus") not in (None, "None"):
                continue
            if exclude_number is not None and item.get("number") == exclude_number:
                continue
            usable.append(item)

        if not usable:
            return MrktFloor(floor_nano=None, listed_count=total, status="no_data", raw=gifts)

        prices = []
        for item in usable:
            try:
                # salePrice is ALREADY in nano-TON (an int, confirmed
                # live -- e.g. 16289400000 == 16.29 TON) -- UNLIKE
                # Portals (a decimal string needing conversion) and
                # Tonnel (a float needing conversion), MRKT needs NO
                # unit conversion here at all. str() first only to avoid
                # a float/Decimal construction surprise if the API ever
                # sends this as a JSON float instead of an int.
                prices.append(Decimal(str(item["salePrice"])))
            except (InvalidOperation, TypeError, KeyError):
                continue

        if not prices:
            return MrktFloor(floor_nano=None, listed_count=total, status="no_data", raw=gifts)

        # NO * config.NANO here -- salePrice is already nano-TON. The
        # earlier `* config.NANO` bug produced OverflowError writing to
        # SQLite (1.6e19, past SQLite's ~9.2e18 INTEGER limit) --
        # confirmed live, db.py:record_cross_check_snapshot.
        floor_nano = int(min(prices))

        return MrktFloor(floor_nano=floor_nano, listed_count=total, status="ok", raw=gifts)

    def feed(self, count: int = 20, cursor: str = "") -> tuple[list[dict], str]:
        """POST /api/v1/feed -- the event feed found for THIS delivery
        (mrkt_poller.py), distinct from /gifts/saling (the "gift
        showcase" query pair_floor()/find_by_number() use, whose order
        was CONFIRMED RANDOM live and is never used for collection).

        Confirmed live: `count` is capped at 20 (same limit as
        /gifts/saling, 50/100 silently truncated); pagination via
        `cursor` is a clean cut (0 overlap measured between pages); order
        is strictly reverse-chronological (`date` descending, confirmed
        against wall-clock-second timestamps). Returns (items, cursor)
        -- `cursor` is the opaque token for the NEXT page, always
        present in the response even on the last page.

        Each item's `amount` is already nano-TON (an int) -- same
        confirmed convention as `salePrice` in pair_floor(), NEVER
        multiplied by anything here. Caller (mrkt_poller.py) is
        responsible for capping `count` <= MAX_COUNT itself if it ever
        varies this away from the default; this method does not silently
        clamp it (the server does that anyway, per spec: "потолок count
        = 20, значения 50/100/200 молча урезаются").
        """
        body = {"count": count, "cursor": cursor}
        token = self._token_provider()
        self._throttle()
        try:
            resp = self._session.post(
                MRKT_FEED_URL, json=body, headers=_headers(token), impersonate="chrome", timeout=15
            )
        except Exception as exc:  # curl_cffi raises its own exception types
            raise MrktError(f"request failed: {exc}") from exc

        if resp.status_code != 200:
            raise MrktError(f"unexpected status {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)

        try:
            data = resp.json()
        except ValueError as exc:
            raise MrktError(f"non-JSON response: {exc}") from exc

        if not isinstance(data, dict) or "items" not in data:
            raise MrktError(f"unexpected response shape (expected a dict with 'items'): {type(data).__name__}")

        return data.get("items", []), data.get("cursor", "")

    def find_by_number(self, collection_name: str, number: int) -> dict | None:
        """Exact lookup of one gift by its number within a collection --
        confirmed live: number=62464 returned LunarSnake-62464. Returns
        None if not found (a real, confirmed-empty result, not a
        failure) -- raises MrktError only on an actual request failure.
        """
        body = dict(_BODY_TEMPLATE)
        body.update(collectionNames=[collection_name], number=number, count=MAX_COUNT)

        data = self._post(body)
        gifts = data.get("gifts", [])
        return gifts[0] if gifts else None


_logged_missing_token = False


def build_default_mrkt_client() -> MrktClient | None:
    """Shared factory for both poller.py and tonnel_poller.py: returns
    None (never raises) when MRKT_ACCESS_TOKEN is unset -- per spec, the
    absence must degrade to "MRKT not queried", logged exactly ONCE
    across the whole process lifetime (not per poll cycle, which would
    flood the log for a deliberately-unconfigured neighbour), never
    crash either poller. Also returns None when MRKT_CROSS_CHECK_ENABLED
    is false -- no point constructing a client that will never be used.
    """
    global _logged_missing_token
    if not config.MRKT_CROSS_CHECK_ENABLED:
        return None
    token = config.get_mrkt_access_token()
    if not token:
        if not _logged_missing_token:
            logger.warning("MRKT_ACCESS_TOKEN is not set -- MRKT cross-check neighbour disabled")
            _logged_missing_token = True
        return None
    # The provider re-reads the token on every request: a refreshed token
    # (mrkt_auth.py) is used without a restart.
    return MrktClient(token_provider=lambda: config.get_mrkt_access_token() or token,
                      request_delay_ms=config.MRKT_REQUEST_DELAY_MS)
