"""Combo-floor (model+backdrop) retrieval and caching.

Cache key is the MODEL NAME ONLY, not collection+model -- the floors
endpoint ignores collection_id/short_name, so floor is computed globally
per model name. This is also exactly why name collisions across
collections are a real data-quality hazard (see report.py backfill pass).

Confirmed live: the server silently drops model names it does not
recognize from a batch response -- requesting N names can return fewer
than N keys with no error signal at all. Never assume a batch response is
complete.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from . import config
from .errors import WafReset
from .models import FloorSnapshot
from .money import to_nano
from .portals_client import PortalsClient

logger = logging.getLogger("gift_sniper.floors")


class FloorCache:
    def __init__(self, client: PortalsClient, ttl_sec: int = config.FLOOR_CACHE_TTL_SEC):
        self._client = client
        self._ttl = ttl_sec
        # model_name -> {"fetched_mono", "fetched_wall", "block": dict, "reason": str|None}
        self._cache: dict[str, dict] = {}
        self.waf_reset_count = 0
        # Cumulative count of model names that were already fresh in
        # cache when warm() was called for them -- i.e. network calls
        # avoided. Read by poller.py for the run summary's
        # floor_cache_hits.
        self.cache_hits = 0

    def _is_fresh(self, model_name: str) -> bool:
        entry = self._cache.get(model_name)
        if entry is None:
            return False
        return (time.monotonic() - entry["fetched_mono"]) < self._ttl

    def _fetch_chunk(self, names: list[str]) -> dict[str, dict]:
        """Single batch call. Returns {name: block} for whatever the server
        actually returned -- may be a subset of `names`.
        """
        try:
            resp = self._client.model_backgrounds_floors(names)
        except WafReset:
            self.waf_reset_count += 1
            logger.error("WAF_RESET fetching floors for models=%s", names)
            return {}
        return resp.get("model_backgrounds", {})

    def warm(self, model_names: set[str]) -> None:
        """Fetch all models not already cached-and-fresh. Requests go out
        in batches of FLOORS_BATCH_SIZE, never one request per listing.
        Any name missing from a batch response is retried ONCE,
        individually; still missing after that -> reason="model_not_returned".
        """
        requested = [m for m in model_names if m]
        missing = sorted(m for m in requested if not self._is_fresh(m))
        self.cache_hits += len(requested) - len(missing)
        if not missing:
            return

        now_mono = time.monotonic()
        now_wall = datetime.now(timezone.utc)
        batch_size = config.FLOORS_BATCH_SIZE

        for i in range(0, len(missing), batch_size):
            chunk = missing[i : i + batch_size]
            blocks = self._fetch_chunk(chunk)

            not_returned = [name for name in chunk if name not in blocks]
            if not_returned:
                logger.info("models not returned in batch, retrying individually: %s", not_returned)

            for name in chunk:
                if name in blocks:
                    self._cache[name] = {
                        "fetched_mono": now_mono,
                        "fetched_wall": now_wall,
                        "block": blocks[name],
                        "reason": None,
                    }

            for name in not_returned:
                single = self._fetch_chunk([name])
                if name in single:
                    self._cache[name] = {
                        "fetched_mono": now_mono,
                        "fetched_wall": now_wall,
                        "block": single[name],
                        "reason": None,
                    }
                else:
                    logger.info("model still not returned after single retry: %s", name)
                    self._cache[name] = {
                        "fetched_mono": now_mono,
                        "fetched_wall": now_wall,
                        "block": {},
                        "reason": "model_not_returned",
                    }

    def snapshot_for(
        self, listing_external_id: str, model_name: str | None, backdrop_name: str | None
    ) -> FloorSnapshot:
        now_wall = datetime.now(timezone.utc)

        if not model_name or model_name not in self._cache:
            return FloorSnapshot(
                listing_external_id=listing_external_id,
                model_name=model_name or "",
                backdrop_name=backdrop_name or "",
                api_combo_floor_nano=None,
                model_min_floor_nano=None,
                floor_fetched_at=now_wall,
                floor_age_sec=0,
                raw_model_block={},
                floor_skip_reason=None,
            )

        entry = self._cache[model_name]
        age_sec = int(time.monotonic() - entry["fetched_mono"])

        if entry["reason"] == "model_not_returned":
            return FloorSnapshot(
                listing_external_id=listing_external_id,
                model_name=model_name,
                backdrop_name=backdrop_name or "",
                api_combo_floor_nano=None,
                model_min_floor_nano=None,
                floor_fetched_at=entry["fetched_wall"],
                floor_age_sec=age_sec,
                raw_model_block={},
                floor_skip_reason="model_not_returned",
            )

        block = entry["block"]
        api_combo_floor_nano = to_nano(block.get(backdrop_name)) if backdrop_name else None
        prices_nano = [p for p in (to_nano(v) for v in block.values()) if p is not None]
        model_min_floor_nano = min(prices_nano) if prices_nano else None

        return FloorSnapshot(
            listing_external_id=listing_external_id,
            model_name=model_name,
            backdrop_name=backdrop_name or "",
            api_combo_floor_nano=api_combo_floor_nano,
            model_min_floor_nano=model_min_floor_nano,
            floor_fetched_at=entry["fetched_wall"],
            floor_age_sec=age_sec,
            raw_model_block=block,
            floor_skip_reason=None,
        )

    def below_threshold_snapshot(
        self, listing_external_id: str, model_name: str | None, backdrop_name: str | None
    ) -> FloorSnapshot:
        """Used by the poller for listings under FLOOR_MIN_PRICE -- no
        network call is made at all.
        """
        return FloorSnapshot(
            listing_external_id=listing_external_id,
            model_name=model_name or "",
            backdrop_name=backdrop_name or "",
            api_combo_floor_nano=None,
            model_min_floor_nano=None,
            floor_fetched_at=datetime.now(timezone.utc),
            floor_age_sec=0,
            raw_model_block={},
            floor_skip_reason="below_price_threshold",
        )
