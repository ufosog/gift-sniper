"""The actual source of truth for combo-floor (model+backdrop within one
collection), replacing BOTH earlier broken sources:

- /collections/models/backgrounds/floors (api_combo_floor_nano):
  confirmed globally scoped by model name, ignoring collection -- returns
  other collections' floors under name collisions (Berry Box 9.35 -> 200.0
  measured live).
- own_floors.py (own_combo_floor_nano): confirmed systematically too high
  (~2x, median own/true ratio ~1.85 i.e. true/own ~0.54) because the
  poller only observes newly-listed items and misses older, still-active,
  cheaper listings.

The fix: query /nfts/search directly, filtered to the exact
collection+model+backdrop triple and sorted by price ascending. See
portals_client.PortalsClient.search_pair_floor for the confirmed request
shape (param order matters; collection_id actually filters here).

SELF-COMPARISON BUG (found via manual review of 20 top price drops: 17
of 20 were artifacts, several from this): when a listing is the ONLY
active one in its pair, it IS the pair floor -- its own (pre-drop) price.
A price cut on that listing then looks like a discount against its own
former price, which was never a price anyone actually paid. Confirmed
concretely: Nail Bracelet #4695 (195->150, pair_listed_count=1, floor
195 == the listing's own old price). The fix: every floor computation
here takes a mandatory `exclude_external_id` and excludes that listing
BEFORE computing the minimum. `floor_nano`/`listed_count` (including
self) are kept for diagnostics/comparison only --
`floor_excluding_self_nano` is what discount/profit must use.

MOST PAIRS ARE ALONE, confirmed live: measured 336 (model, backdrop)
pairs with exactly one active listing, zero with ten. Excluding self from
a 1-listing book always leaves nothing -- status="alone_in_pair" (see
below), a real and common outcome, not a rare edge case. MODEL-level
floor (search_model_floor, no backdrop filter) is the fallback for
exactly this case -- coarser (different backdrops price differently
within a model) but still real, self-excluded, live data, and available
far more often than the pair level is.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import config
from .errors import PortalsError
from .money import to_nano
from .portals_client import PortalsClient

logger = logging.getLogger("gift_sniper.pair_floor")


@dataclass
class OrderBookFloor:
    """Generic result shape, used for both the pair level (model+backdrop)
    and the model-level fallback (model only) -- the computation is
    identical, only the request filters differ.
    """
    # Including the excluded listing itself -- diagnostics/comparison only.
    floor_nano: int | None
    listed_count: int  # how many status="listed", price != null entries were in the response
    unlisted_skipped: int  # how many entries were dropped (unlisted, or null price)
    # "ok"            -- floor_excluding_self_nano is populated, usable.
    # "alone_in_pair" -- the excluded listing was the ONLY listed one in
    #                    the book. Confirmed the common case, not rare.
    #                    floor_excluding_self_nano is None.
    # "no_data"       -- the book was empty even INCLUDING the excluded
    #                    listing (e.g. it was already delisted by the time
    #                    of this query), or every entry was unlisted/null.
    # "error"         -- the request itself failed.
    # A status of "ok" is a HARD GUARANTEE that floor_excluding_self_nano
    # is not None -- report.py and poller.py rely on this; it must never
    # be computed from the pre-exclusion result.
    status: str
    fetched_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)
    # Source of truth for discount/profit -- see module docstring.
    floor_excluding_self_nano: int | None = None
    listed_count_excluding_self: int = 0
    self_was_floor: bool = False  # True if the excluded listing was the (pre-exclusion) minimum


# Backwards-compatible alias -- pair-level results are the same shape.
PairFloor = OrderBookFloor


def _floor_from_response(resp: dict, exclude_external_id: str) -> OrderBookFloor:
    """Confirmed live: status="unlisted" / price=null entries can appear,
    including as the FIRST element when sort=price_asc (a null price
    apparently sorts before everything). Taking the first element as the
    floor is explicitly wrong and produces None instead of a real floor.
    Only the minimum among status="listed", price != null entries counts
    -- and the excluded listing's own entry never counts towards
    floor_excluding_self_nano, regardless of its position.

    Status is computed strictly from the POST-exclusion result -- never
    from whether *something* (possibly only the excluded listing itself)
    was in the book.
    """
    items = resp.get("results", [])
    listed: list[tuple[Any, int]] = []  # (id, price_nano)
    unlisted_skipped = 0

    for item in items:
        if item.get("status") != "listed" or item.get("price") is None:
            unlisted_skipped += 1
            continue
        price_nano = to_nano(item.get("price"))
        if price_nano is None:
            unlisted_skipped += 1
            continue
        listed.append((item.get("id"), price_nano))

    now = datetime.now(timezone.utc)
    all_prices = [p for _, p in listed]
    floor_nano = min(all_prices) if all_prices else None
    listed_count = len(listed)

    excluding_prices = [p for i, p in listed if i != exclude_external_id]
    floor_excluding_self_nano = min(excluding_prices) if excluding_prices else None
    listed_count_excluding_self = len(excluding_prices)

    self_price = next((p for i, p in listed if i == exclude_external_id), None)
    self_was_floor = self_price is not None and floor_nano is not None and self_price == floor_nano

    if excluding_prices:
        status = "ok"
    elif listed:
        # Something was listed (at least the excluded listing itself),
        # but nothing survives exclusion -- alone in the book.
        status = "alone_in_pair"
    else:
        status = "no_data"

    return OrderBookFloor(
        floor_nano=floor_nano,
        listed_count=listed_count,
        unlisted_skipped=unlisted_skipped,
        status=status,
        fetched_at=now,
        raw=resp,
        floor_excluding_self_nano=floor_excluding_self_nano,
        listed_count_excluding_self=listed_count_excluding_self,
        self_was_floor=self_was_floor,
    )


def _error_floor(error: str) -> OrderBookFloor:
    return OrderBookFloor(
        floor_nano=None,
        listed_count=0,
        unlisted_skipped=0,
        status="error",
        fetched_at=datetime.now(timezone.utc),
        raw={"error": error},
        floor_excluding_self_nano=None,
        listed_count_excluding_self=0,
        self_was_floor=False,
    )


class PairFloorCache:
    """In-memory cache keyed by (collection_id, model_name, backdrop_name)
    for the pair level, and (collection_id, model_name) for the model
    level -- NOT by the excluded listing, since the raw order-book
    response is shared across every listing in that pair/model; exclusion
    is computed per-call from the cached raw response, at no extra
    network cost. One network call per key per TTL window, regardless of
    how many listings share it -- this is the query that costs
    rate-limit budget on the tightly-limited /nfts/search endpoint, so
    cache hits matter.
    """

    def __init__(self, client: PortalsClient, ttl_sec: int = config.PAIR_FLOOR_CACHE_TTL_SEC):
        self._client = client
        self._ttl = ttl_sec
        # key -> (fetched_mono, resp_dict_or_None, error_str_or_None)
        self._cache: dict[tuple[str, str, str], tuple[float, dict | None, str | None]] = {}
        self._model_cache: dict[tuple[str, str], tuple[float, dict | None, str | None]] = {}
        # Cumulative count of get()/get_fresh() calls served from cache
        # without a network call (pair AND model level combined). Read by
        # poller.py for the run summary's floor_cache_hits.
        self.cache_hits = 0

    def _is_fresh(self, cache: dict, key) -> bool:
        entry = cache.get(key)
        if entry is None:
            return False
        fetched_mono, _, _ = entry
        return (time.monotonic() - fetched_mono) < self._ttl

    # --- pair level (model + backdrop) ---------------------------------

    def get(
        self, collection_id: str, model_name: str, backdrop_name: str, exclude_external_id: str
    ) -> tuple[OrderBookFloor, int]:
        """Returns (OrderBookFloor, age_sec). age_sec is 0 for a fresh
        fetch, or the actual cache-entry age in seconds for a cache hit.
        `exclude_external_id` is mandatory -- see module docstring.
        """
        key = (collection_id, model_name, backdrop_name)

        if self._is_fresh(self._cache, key):
            self.cache_hits += 1
            fetched_mono, resp, error = self._cache[key]
            age_sec = int(time.monotonic() - fetched_mono)
        else:
            resp, error = self._fetch_pair_raw(collection_id, model_name, backdrop_name)
            self._cache[key] = (time.monotonic(), resp, error)
            age_sec = 0

        if error is not None:
            return _error_floor(error), age_sec
        return _floor_from_response(resp, exclude_external_id), age_sec

    def get_fresh(
        self, collection_id: str, model_name: str, backdrop_name: str, exclude_external_id: str
    ) -> tuple[OrderBookFloor, int]:
        """Like get(), but ALWAYS makes a real network call, bypassing
        the TTL cache (and refreshing it for subsequent get() calls).
        Used when a price drop is recorded -- the floor at the moment of
        the drop should be as fresh as possible, not whatever happened to
        be cached up to PAIR_FLOOR_CACHE_TTL_SEC ago. Only called for
        drops that already cleared PRICE_DROP_MIN_PCT (a small, bounded
        rate of events), never for every listing.
        """
        key = (collection_id, model_name, backdrop_name)
        resp, error = self._fetch_pair_raw(collection_id, model_name, backdrop_name)
        self._cache[key] = (time.monotonic(), resp, error)
        if error is not None:
            return _error_floor(error), 0
        return _floor_from_response(resp, exclude_external_id), 0

    def _fetch_pair_raw(
        self, collection_id: str, model_name: str, backdrop_name: str
    ) -> tuple[dict | None, str | None]:
        try:
            resp = self._client.search_pair_floor(collection_id, model_name, backdrop_name, limit=20, offset=0)
            return resp, None
        except PortalsError as exc:
            logger.error(
                "pair_floor fetch failed for (collection_id=%s, model=%s, backdrop=%s): %s",
                collection_id, model_name, backdrop_name, exc,
            )
            return None, str(exc)

    # --- model level (fallback, no backdrop filter) ---------------------

    def get_model_floor(
        self, collection_id: str, model_name: str, exclude_external_id: str
    ) -> tuple[OrderBookFloor, int]:
        """Model-level fallback. Callers (poller.py) must only call this
        when the pair level came back "alone_in_pair" -- never when pair
        already gave "ok", to avoid spending rate-limit budget on a
        comparison that won't be used.
        """
        key = (collection_id, model_name)

        if self._is_fresh(self._model_cache, key):
            self.cache_hits += 1
            fetched_mono, resp, error = self._model_cache[key]
            age_sec = int(time.monotonic() - fetched_mono)
        else:
            resp, error = self._fetch_model_raw(collection_id, model_name)
            self._model_cache[key] = (time.monotonic(), resp, error)
            age_sec = 0

        if error is not None:
            return _error_floor(error), age_sec
        return _floor_from_response(resp, exclude_external_id), age_sec

    def get_model_floor_fresh(
        self, collection_id: str, model_name: str, exclude_external_id: str
    ) -> tuple[OrderBookFloor, int]:
        key = (collection_id, model_name)
        resp, error = self._fetch_model_raw(collection_id, model_name)
        self._model_cache[key] = (time.monotonic(), resp, error)
        if error is not None:
            return _error_floor(error), 0
        return _floor_from_response(resp, exclude_external_id), 0

    def _fetch_model_raw(self, collection_id: str, model_name: str) -> tuple[dict | None, str | None]:
        try:
            resp = self._client.search_model_floor(collection_id, model_name, limit=50, offset=0)
            return resp, None
        except PortalsError as exc:
            logger.error(
                "model_floor fetch failed for (collection_id=%s, model=%s): %s",
                collection_id, model_name, exc,
            )
            return None, str(exc)
