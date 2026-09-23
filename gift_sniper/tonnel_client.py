"""Tonnel Market API client (gifts2.tonnel.network) -- a SEPARATE module
from portals_client.py by design, per spec: different protocol
(no auth, different filter/sort shape, different rate-limit behavior,
different response fields), so this does NOT subclass or reuse
PortalsClient -- sharing an ancestor would couple two independently-
evolving, unrelated protocols.

ALL facts below are confirmed by live requests, not assumed:
- POST https://gifts2.tonnel.network/api/pageGifts, no auth required
  (user_auth="" -> 200).
- Requires curl_cffi with impersonate="chrome" -- plain requests/httpx
  are rejected on the TLS fingerprint. `pip install curl_cffi`.
- Request body's `sort` and `filter` fields are JSON-encoded STRINGS,
  not nested objects.
- Confirmed max `limit` is 30 -- 50 returns {"error": "limit is too big"}.
- No rate limit observed (20 requests back-to-back, all 200,
  0.45-0.75s natural latency) -- REQUEST_DELAY_MS throttling is still
  applied unconditionally, per spec ("всё равно ставить паузу").
- model/backdrop filters use a regex ({"$regex": "^<name>"}) because the
  rarity percentage is baked into the stored name (e.g.
  "Fried Chicken (1.5%)") and isn't knowable ahead of a query -- an
  anchored PREFIX match finds it regardless of rarity. Confirmed a
  nonexistent model name returns 0 results (filters are real, not
  ignored).
- Tonnel adds a 10% BUYER fee on top of `price` -- `price * 1.1` is what
  a buyer actually pays. This is a DIFFERENT fee model from Portals
  (2% SELLER fee) -- never compare the two markets' raw prices without
  converting both to what a buyer actually pays / a seller actually
  nets, see signals.py's cross-market comparison.
- Batch status lookup for lifecycle checking (see
  search_minimal_by_gift_ids): {"gift_id": {"$in": [...]}} is CONFIRMED
  live to work (200, 4/4 requested items returned). {"gift_num":
  {"$in": [...]}} does NOT work (400, {"error": "Invalid gift filter"}),
  and neither does a string-
  valued gift_num $in. {"$or": [{"gift_num": n}, ...]} also works but is
  more verbose and grows with batch size -- gift_id $in is used instead.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from curl_cffi import requests as curl_requests

from . import config

logger = logging.getLogger("gift_sniper.tonnel_client")

TONNEL_API_URL = "https://gifts2.tonnel.network/api/pageGifts"
MAX_LIMIT = 30  # confirmed live: limit=50 -> {"error": "limit is too big"}

# Confirmed live: this exact base filter must be present for the
# marketplace's own "actually for sale" semantics (price exists, not
# refunded, no buyer yet, export_at set, TON-denominated).
BASE_FILTER: dict = {
    "price": {"$exists": True},
    "refunded": {"$ne": True},
    "buyer": {"$exists": False},
    "export_at": {"$exists": True},
    "asset": "TON",
}

HEADERS: dict[str, str] = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://market.tonnel.network",
    "referer": "https://market.tonnel.network/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


class TonnelError(RuntimeError):
    """Raised on any request failure, non-200 status, non-JSON body, or
    an API-level {"error": ...} response -- callers must never receive a
    silently empty list for a genuine failure, only for a real,
    confirmed-empty result set (see TonnelFloor status="no_data").

    `status_code` is the HTTP status when the failure was an
    unexpected-status response (e.g. 403/429) -- None for every other
    failure kind (network error, non-JSON body, API-level {"error":...}).
    Lets a caller (tonnel_poller.py) count 403/429 specifically, per
    spec: "площадка может начать сопротивляться при росте нагрузки."
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _regex_filter(value: str) -> dict:
    """{"$regex": "^<escaped>"} -- an anchored PREFIX match, since the
    rarity percentage is baked into model/backdrop names
    (e.g. "Fried Chicken (1.5%)") and isn't known ahead of a query.
    re.escape() is mandatory: collection/model names containing regex-
    special characters (confirmed live case: "Durov's Cap", apostrophe)
    would otherwise either break the regex or silently match more than
    intended.
    """
    return {"$regex": f"^{re.escape(value)}"}


@dataclass
class TonnelFloor:
    """Result of pair_floor() -- the SELF-EXCLUDED minimum price for one
    exact (gift_name, model, backdrop) combination on Tonnel, mirroring
    pair_floor.py's OrderBookFloor shape/self-exclusion discipline for
    the equivalent Portals concept (a deliberately parallel design, not
    a shared implementation -- see module docstring on why this isn't a
    subclass).
    """
    floor_nano: int | None  # minimum `price`, in nano-TON, self-excluded
    floor_with_fee_nano: int | None  # floor * 1.1 -- what a buyer actually pays
    listed_count: int  # how many usable (non-excluded) listings survived
    status: str  # "ok" | "no_data" | "error"
    raw: list = field(default_factory=list)


class TonnelClient:
    def __init__(
        self,
        sleep_fn=time.sleep,
        request_delay_ms: int = 600,
        session=None,
    ):
        """`session` is injectable so tests can supply a fake with a
        `.post(url, json=..., headers=..., impersonate=..., timeout=...)`
        method, without touching curl_cffi/the network at all -- same
        discipline as portals_client.py's PortalsClient(session=...).
        """
        self._sleep = sleep_fn
        self._request_delay_ms = request_delay_ms
        self._session = session or curl_requests.Session()
        self._last_request_mono: float | None = None

    def _throttle(self) -> None:
        """Unconstrained by any confirmed rate limit (20 back-to-back
        requests all succeeded) -- still applied unconditionally, per
        spec: a marketplace can change its rate-limiting posture without
        notice, and there's no cost to staying polite.
        """
        if self._last_request_mono is not None:
            elapsed_ms = (time.monotonic() - self._last_request_mono) * 1000
            remaining_ms = self._request_delay_ms - elapsed_ms
            if remaining_ms > 0:
                self._sleep(remaining_ms / 1000)
        self._last_request_mono = time.monotonic()

    def search(
        self,
        gift_name: str | None = None,
        model: str | None = None,
        backdrop: str | None = None,
        gift_num: int | None = None,
        min_price: Decimal | str | None = None,
        limit: int = 30,
        page: int = 1,
        sort: dict | None = None,
    ) -> list[dict]:
        """POST /api/pageGifts. Returns the raw list of gift objects.
        Raises TonnelError on any failure -- never returns an empty list
        to mean "the request failed", only to mean "confirmed zero
        results" (e.g. a nonexistent model, per spec's control check).

        `min_price`: CONFIRMED live (two independent ways -- both
        {"price": {"$gte": N}} in `filter` and a top-level
        "price_range": [N, huge] returned only lots priced >= N; a
        control run with no filter at sort={"price":1} showed real lots
        as low as 3.85). Sent as {"$gte": ...} MERGED into the existing
        `price` key -- the base filter's {"$exists": true} on `price`
        must survive alongside it (Tonnel's own "actually for sale"
        semantics), never overwritten.
        """
        if limit > MAX_LIMIT:
            raise TonnelError(f"limit={limit} exceeds the confirmed max of {MAX_LIMIT} (50 returns an API error)")

        filter_dict = dict(BASE_FILTER)
        filter_dict["price"] = dict(BASE_FILTER["price"])
        if min_price is not None:
            filter_dict["price"]["$gte"] = float(min_price)
        if gift_name is not None:
            filter_dict["gift_name"] = gift_name
        if model is not None:
            filter_dict["model"] = _regex_filter(model)
        if backdrop is not None:
            filter_dict["backdrop"] = _regex_filter(backdrop)
        if gift_num is not None:
            filter_dict["gift_num"] = gift_num

        body = {
            "page": page,
            "limit": limit,
            # sort/filter are JSON-encoded STRINGS, not nested objects -- confirmed live.
            "sort": json.dumps(sort if sort is not None else {"price": 1}),
            "filter": json.dumps(filter_dict),
            "price_range": None,
            "user_auth": "",
        }

        self._throttle()
        try:
            resp = self._session.post(
                TONNEL_API_URL, json=body, headers=HEADERS, impersonate="chrome", timeout=15
            )
        except Exception as exc:  # curl_cffi raises its own exception types
            raise TonnelError(f"request failed: {exc}") from exc

        if resp.status_code != 200:
            raise TonnelError(f"unexpected status {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)

        try:
            data = resp.json()
        except ValueError as exc:
            raise TonnelError(f"non-JSON response: {exc}") from exc

        if isinstance(data, dict) and "error" in data:
            raise TonnelError(f"API error: {data['error']}")
        if not isinstance(data, list):
            raise TonnelError(f"unexpected response shape (expected a list): {type(data).__name__}")

        return data

    def search_minimal_by_gift_ids(self, gift_ids: list[int], limit: int) -> list[dict]:
        """Lifecycle status check (tonnel_poller.py) -- ДОПОЛНЕНИЕ,
        Правка 1: batches up to `limit` gift_ids into
        {"gift_id": {"$in": [...]}, "asset": "TON"} -- a DELIBERATELY
        MINIMAL filter, NOT combined with BASE_FILTER.

        CONFIRMED live (spot-check of 12 gift_ids): using the full
        BASE_FILTER for this check (price $exists, refunded $ne true,
        buyer $exists false, export_at $exists) produced FALSE
        disappearances -- 8/12 lots were still genuinely for sale
        (status="forsale", price intact) but got excluded by one of
        those extra conditions, not because they were actually gone.
        One case (gift_id=10300285) was only found once buyer/refunded
        were dropped, with status="forsale" and no buyer/refunded fields
        in the response at all -- cause not established, but it
        confirms BASE_FILTER is too strict for a mere liveness check.
        Only `asset: "TON"` is kept (this project only ever deals with
        TON-denominated lots) plus the gift_id filter itself -- nothing
        else. A queried gift_id genuinely missing from the result means
        it is not on the platform at all (spot-check: 3/12 absent both
        with and without BASE_FILTER) -- the strongest signal this
        endpoint can give, but it still can't distinguish a sale from a
        delisting (see README).

        gift_id IS this project's Tonnel external_id (external_id =
        str(gift_id)).

        `limit` MUST be passed explicitly and match (or exceed) the
        number of gift_ids requested -- confirmed elsewhere in this
        client (search()) that Tonnel's default page size can silently
        truncate a result set below what was asked for.
        """
        if not gift_ids:
            return []
        if len(gift_ids) > MAX_LIMIT:
            raise TonnelError(
                f"search_minimal_by_gift_ids: {len(gift_ids)} gift_ids exceeds the confirmed max limit of {MAX_LIMIT}"
            )
        if limit > MAX_LIMIT:
            raise TonnelError(f"limit={limit} exceeds the confirmed max of {MAX_LIMIT}")

        # Deliberately NOT `dict(BASE_FILTER)` -- see docstring above:
        # price.$exists / refunded / buyer / export_at all produced
        # false disappearances for genuinely-live lots.
        filter_dict = {
            "gift_id": {"$in": list(gift_ids)},
            "asset": "TON",
        }

        body = {
            "page": 1,
            "limit": limit,
            "sort": json.dumps({"price": 1}),
            "filter": json.dumps(filter_dict),
            "price_range": None,
            "user_auth": "",
        }

        self._throttle()
        try:
            resp = self._session.post(
                TONNEL_API_URL, json=body, headers=HEADERS, impersonate="chrome", timeout=15
            )
        except Exception as exc:
            raise TonnelError(f"request failed: {exc}") from exc

        if resp.status_code != 200:
            raise TonnelError(f"unexpected status {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)

        try:
            data = resp.json()
        except ValueError as exc:
            raise TonnelError(f"non-JSON response: {exc}") from exc

        if isinstance(data, dict) and "error" in data:
            raise TonnelError(f"API error: {data['error']}")
        if not isinstance(data, list):
            raise TonnelError(f"unexpected response shape (expected a list): {type(data).__name__}")

        return data

    def pair_floor(
        self,
        gift_name: str,
        model: str,
        backdrop: str,
        exclude_gift_num: int | None = None,
    ) -> TonnelFloor:
        """Self-excluded minimum price for one exact (gift_name, model,
        backdrop) combination -- the Tonnel-side equivalent of
        pair_floor.py's core self-comparison-bug fix. Drops:
          - status != "forsale" (not actually purchasable);
          - underLoan (collateralized, not sellable normally);
          - a non-empty premarketData (pre-market lot, not a normal buy);
          - the listing matching exclude_gift_num, if given (the same
            listing being cross-checked must never be compared against
            itself).
        """
        results = self.search(
            gift_name=gift_name, model=model, backdrop=backdrop, limit=MAX_LIMIT, sort={"price": 1}
        )

        usable = []
        for item in results:
            if item.get("status") != "forsale":
                continue
            if item.get("underLoan"):
                continue
            if item.get("premarketData"):
                continue
            if exclude_gift_num is not None and item.get("gift_num") == exclude_gift_num:
                continue
            usable.append(item)

        if not usable:
            return TonnelFloor(
                floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=results
            )

        prices = []
        for item in usable:
            try:
                # str() first: item["price"] is a JSON float (already
                # binary-imprecise by the time it reaches us) -- routing
                # it through str() avoids compounding that with a second,
                # avoidable float->Decimal conversion artifact.
                prices.append(Decimal(str(item["price"])))
            except (InvalidOperation, TypeError, KeyError):
                continue

        if not prices:
            return TonnelFloor(
                floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=results
            )

        floor_price = min(prices)
        floor_nano = int(floor_price * config.NANO)
        floor_with_fee_nano = int(floor_price * Decimal("1.1") * config.NANO)

        return TonnelFloor(
            floor_nano=floor_nano,
            floor_with_fee_nano=floor_with_fee_nano,
            listed_count=len(usable),
            status="ok",
            raw=results,
        )

    def model_floor(
        self,
        gift_name: str,
        model: str,
        exclude_gift_num: int | None = None,
    ) -> TonnelFloor:
        """Self-excluded minimum price across an entire MODEL (gift_name +
        model, NO backdrop filter) -- Правка 1 (Tonnel model-floor
        delivery). Confirmed live, TWO separate measurements: pair_floor()
        (same collection+model+backdrop) is absent in 20/20 significant
        Tonnel price drops spot-checked -- the feed is roughly half
        Portals' rate, so a second listing of the exact same pair almost
        never coexists in the book. Model-level has real depth instead
        (3-11 listings measured per model, vs. 0-1 at pair level) -- this
        is the basis this project actually has data for on Tonnel.

        Drops the same things pair_floor() does (status != "forsale",
        underLoan, premarketData, the excluded gift_num), PLUS gift_id < 0
        (a BUNDLE -- its price covers a whole set, not one lot, see
        tonnel_parsing.py's is_bundle handling in the collector).

        Caller (tonnel_poller.py) is responsible for TONNEL_MODEL_MIN_LISTED_COUNT
        -- this method reports whatever listed_count it found; it does
        NOT downgrade status to "thin_model_book" itself, since that's a
        signal-formation policy, not a fact about the query result.
        """
        results = self.search(gift_name=gift_name, model=model, limit=MAX_LIMIT, sort={"price": 1})

        usable = []
        for item in results:
            gift_id = item.get("gift_id")
            if gift_id is not None and gift_id < 0:
                continue
            if item.get("status") != "forsale":
                continue
            if item.get("underLoan"):
                continue
            if item.get("premarketData"):
                continue
            if exclude_gift_num is not None and item.get("gift_num") == exclude_gift_num:
                continue
            usable.append(item)

        if not usable:
            return TonnelFloor(
                floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=results
            )

        prices = []
        for item in usable:
            try:
                prices.append(Decimal(str(item["price"])))
            except (InvalidOperation, TypeError, KeyError):
                continue

        if not prices:
            return TonnelFloor(
                floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=results
            )

        floor_price = min(prices)
        floor_nano = int(floor_price * config.NANO)
        floor_with_fee_nano = int(floor_price * Decimal("1.1") * config.NANO)

        return TonnelFloor(
            floor_nano=floor_nano,
            floor_with_fee_nano=floor_with_fee_nano,
            listed_count=len(usable),
            status="ok",
            raw=results,
        )
