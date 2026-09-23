"""In-memory fakes for PortalsClient used across integration tests --
no real HTTP, no real authData involved anywhere in the test suite.

Pages are addressed by offset: pages[i] is the page returned for
offset == i * 50, matching how the real /nfts/search pagination works.
An offset beyond the provided list (or an explicitly empty page) returns
an empty result, which is what makes the poller's pagination loop stop.
"""
from __future__ import annotations


class FakePortalsClient:
    def __init__(
        self,
        pages: list[list[dict]],
        floors_responses: dict[frozenset, dict] | None = None,
        pair_floor_responses: dict[tuple, dict] | None = None,
        model_floor_responses: dict[tuple, dict] | None = None,
        by_ids_responses: dict[str, dict] | None = None,
    ):
        self._pages = pages
        self._floors_responses = floors_responses or {}
        self._pair_floor_responses = pair_floor_responses or {}
        self._model_floor_responses = model_floor_responses or {}
        # Keyed by external_id -> the single result dict to return for a
        # search_by_ids([external_id]) call (freshness check, poller.py
        # Правка 3). Default: echoes back status="listed" with whatever
        # price was given, i.e. "still fresh" -- tests that care about a
        # stale/withdrawn lot pass by_ids_responses explicitly.
        self._by_ids_responses = by_ids_responses or {}
        self.floors_calls: list[list[str]] = []
        self.pair_floor_calls: list[tuple] = []
        self.model_floor_calls: list[tuple] = []
        self.by_ids_calls: list[list[str]] = []
        self.by_ids_limits: list[int | None] = []
        self.search_offsets: list[int] = []
        self.search_min_prices: list[str | None] = []
        self.rate_limited_count = 0
        self.preemptive_pause_count = 0
        self.request_counts: dict[str, int] = {}

    def search(self, limit: int, offset: int, sort: str | None = None, min_price: str | None = None) -> dict:
        self.search_offsets.append(offset)
        self.search_min_prices.append(min_price)
        self.request_counts["/nfts/search"] = self.request_counts.get("/nfts/search", 0) + 1
        idx = offset // 50
        if idx >= len(self._pages):
            return {"results": [], "total_count": 0}
        return {"results": self._pages[idx], "total_count": 0}

    def model_backgrounds_floors(self, model_names: list[str]) -> dict:
        self.floors_calls.append(list(model_names))
        path = "/collections/models/backgrounds/floors"
        self.request_counts[path] = self.request_counts.get(path, 0) + 1
        key = frozenset(model_names)
        if key in self._floors_responses:
            return self._floors_responses[key]
        # default: return floors keyed by whichever names were asked for
        return {"model_backgrounds": {name: {} for name in model_names}}

    def search_pair_floor(
        self, collection_id: str, model_name: str, backdrop_name: str, limit: int = 20, offset: int = 0
    ) -> dict:
        key = (collection_id, model_name, backdrop_name)
        self.pair_floor_calls.append(key)
        self.request_counts["/nfts/search"] = self.request_counts.get("/nfts/search", 0) + 1
        if key in self._pair_floor_responses:
            return self._pair_floor_responses[key]
        # default: no data for this pair -- tests that care about a real
        # pair floor pass pair_floor_responses explicitly.
        return {"results": []}

    def search_model_floor(self, collection_id: str, model_name: str, limit: int = 50, offset: int = 0) -> dict:
        key = (collection_id, model_name)
        self.model_floor_calls.append(key)
        self.request_counts["/nfts/search"] = self.request_counts.get("/nfts/search", 0) + 1
        if key in self._model_floor_responses:
            return self._model_floor_responses[key]
        return {"results": []}

    def search_by_ids(self, ids: list[str], limit: int | None = None) -> dict:
        self.by_ids_calls.append(list(ids))
        self.by_ids_limits.append(limit)
        self.request_counts["/nfts/search"] = self.request_counts.get("/nfts/search", 0) + 1
        results = [self._by_ids_responses[ext_id] for ext_id in ids if ext_id in self._by_ids_responses]
        return {"results": results}

    def market_config(self) -> dict:
        # Never due by default (Poller.maybe_refresh_market_config only
        # calls this when CONFIG_REFRESH_SEC has elapsed or on first
        # call); tests that care about config snapshots override this.
        return {}
