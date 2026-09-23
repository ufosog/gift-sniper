"""Thin HTTP wrapper over the Portal Market API. No business logic here:
no floor caching, no dedup, no discount math -- just requests, pacing and
error categorization.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import quote, urlencode

import requests

from . import config
from .errors import AuthInvalid, RateLimited, TransientError, WafBlock, WafReset

logger = logging.getLogger("gift_sniper.portals_client")

MAX_RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_BASE_BACKOFF_SEC = 2.0


class PortalsClient:
    def __init__(
        self,
        auth_provider,
        session: requests.Session | None = None,
        sleep_fn=time.sleep,
        request_delay_ms: int | None = None,
    ):
        """auth_provider: callable() -> str, returns the current authData
        value. Indirection lets auth.py swap the token without this class
        knowing anything about token refresh.

        sleep_fn is injectable so tests can assert on exact wait durations
        without actually sleeping.
        """
        self._auth_provider = auth_provider
        self._session = session or requests.Session()
        self._sleep = sleep_fn
        self._request_delay_ms = (
            request_delay_ms if request_delay_ms is not None else config.REQUEST_DELAY_MS
        )
        # Confirmed live: rate limits differ PER ENDPOINT (/nfts/search:
        # x-ratelimit-limit 2; floors endpoint: x-ratelimit-limit 5, with
        # a longer x-ratelimit-reset window). Throttling state is
        # therefore tracked per path -- two requests to different
        # endpoints never wait on each other.
        self._last_request_mono_by_path: dict[str, float] = {}
        self._extra_delay_pending_by_path: dict[str, bool] = {}
        # Cumulative count of 429 responses observed, across all calls made
        # through this client instance. Read by poller.py / step0.py to
        # include in the run summary.
        self.rate_limited_count = 0
        # Cumulative count of times a successful response's
        # x-ratelimit-remaining was 0, triggering a preemptive extra pause
        # before the NEXT request on that SAME path (instead of waiting to
        # get 429'd first).
        self.preemptive_pause_count = 0
        # Per-path attempt counts (every actual HTTP GET, including 429
        # retries) -- used for the run summary's "search requests" /
        # "floor requests" counts.
        self.request_counts: dict[str, int] = {}

    def _headers(self) -> dict[str, str]:
        headers = dict(config.HEADERS_STATIC)
        token = self._auth_provider()
        if token:  # no token: anonymous request, see config.get_portals_auth
            headers["Authorization"] = f"tma {token}"
        return headers

    def _throttle(self, path: str) -> None:
        """Enforces REQUEST_DELAY_MS between two requests to the SAME
        path -- rate limits are confirmed to differ per endpoint, so a
        request to one path never waits on a request to a different path.
        Additionally, if the previous successful response on THIS path
        reported x-ratelimit-remaining: 0, waits one EXTRA
        REQUEST_DELAY_MS on top of the normal spacing before hitting that
        same path again.
        """
        last_mono = self._last_request_mono_by_path.get(path)
        if last_mono is not None:
            elapsed_ms = (time.monotonic() - last_mono) * 1000
            remaining_ms = self._request_delay_ms - elapsed_ms
            if remaining_ms > 0:
                self._sleep(remaining_ms / 1000)

        if self._extra_delay_pending_by_path.get(path):
            self._extra_delay_pending_by_path[path] = False
            self._sleep(self._request_delay_ms / 1000)

        self._last_request_mono_by_path[path] = time.monotonic()

    def _do_request(self, url: str, params: dict | None):
        try:
            return self._session.get(url, params=params, headers=self._headers(), timeout=15)
        except requests.exceptions.ConnectionError as exc:
            raise TransientError(str(exc)) from exc
        except requests.exceptions.Timeout as exc:
            raise TransientError(str(exc)) from exc

    @staticmethod
    def _build_ordered_url(path: str, ordered_params: list[tuple[str, str]]) -> str:
        """Builds the full URL with the query string embedded, in EXACTLY
        the given parameter order. Confirmed live: parameter order is
        significant for /nfts/search -- sort silently stops being applied
        if it doesn't come first. This bypasses `requests`' own params=
        handling (and any dict re-keying) entirely, so there is no code
        path between here and the wire that could reorder anything.
        Spaces are encoded as %20 (quote_via=quote), not '+'.
        """
        query = urlencode(ordered_params, quote_via=quote)
        return f"{config.BASE_URL}{path}?{query}"

    def _wait_seconds_for_429(self, headers: dict, attempt: int) -> float:
        """Priority order, per confirmed live behavior (no Retry-After
        header was ever observed, but a well-behaved server might send
        one): Retry-After > x-ratelimit-reset (if > 0) > exponential
        backoff from 2s.
        """
        retry_after = headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass

        reset = headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                reset_val = float(reset)
                if reset_val > 0:
                    return reset_val
            except ValueError:
                pass

        return RATE_LIMIT_BASE_BACKOFF_SEC * (2**attempt)

    def _get(
        self,
        path: str,
        params: dict | None = None,
        ordered_params: list[tuple[str, str]] | None = None,
    ) -> dict:
        if ordered_params is not None:
            url = self._build_ordered_url(path, ordered_params)
            request_params = None  # query already embedded in url, verbatim order
        else:
            url = f"{config.BASE_URL}{path}"
            request_params = params

        for attempt in range(MAX_RATE_LIMIT_ATTEMPTS):
            self._throttle(path)
            self.request_counts[path] = self.request_counts.get(path, 0) + 1
            resp = self._do_request(url, request_params)

            if resp.status_code == 429:
                self.rate_limited_count += 1
                headers = dict(resp.headers)
                logger.warning("RATE_LIMIT 429 on %s, headers=%s", path, headers)
                wait_sec = self._wait_seconds_for_429(headers, attempt)
                self._sleep(wait_sec)
                continue

            if resp.status_code in (401, 403):
                content_type = resp.headers.get("content-type", "")
                is_cloudflare = "cloudflare" in resp.headers.get("server", "").lower() or bool(
                    resp.headers.get("cf-ray")
                )
                if is_cloudflare or "html" in content_type.lower():
                    raise WafBlock(f"{resp.status_code} WAF block, content-type={content_type}")
                raise AuthInvalid(f"{resp.status_code} auth invalid")

            if 500 <= resp.status_code < 600:
                raise TransientError(f"{resp.status_code} server error")

            resp.raise_for_status()

            remaining = resp.headers.get("x-ratelimit-remaining")
            if remaining is not None:
                try:
                    if int(remaining) == 0:
                        self._extra_delay_pending_by_path[path] = True
                        self.preemptive_pause_count += 1
                except ValueError:
                    pass

            return resp.json()

        raise RateLimited({"note": f"exhausted {MAX_RATE_LIMIT_ATTEMPTS} attempts"})

    def search(
        self, limit: int, offset: int, sort: str | None = None, min_price: str | None = None
    ) -> dict:
        """GET /nfts/search. Confirmed live: max effective limit is 50 --
        values of 100/200 return 200 OK but only 50 results, page size is
        capped server-side regardless of what's requested. Default sort
        order, sort=latest, and sort=listed_at desc all confirmed to give
        the same, correctly-descending order.

        `min_price` (ordinary units, e.g. "15") is COLLECT_MIN_PRICE --
        narrowing the collection loop itself to a price segment, measured
        to be the only way to keep up with the full stream of a segment
        under Portals' rate limit (see README). Parameters are sent via
        an explicit ordered list, same discipline as search_pair_floor --
        parameter order has been confirmed significant on this endpoint
        for other parameter combinations, so it is never left to a dict's
        incidental ordering here either. Omitted (None or falsy) means no
        price filter, same as before this parameter existed.
        """
        ordered_params: list[tuple[str, str]] = [("limit", str(limit)), ("offset", str(offset))]
        if sort is not None:
            ordered_params.append(("sort", sort))
        if min_price:
            ordered_params.append(("min_price", str(min_price)))
        return self._get("/nfts/search", ordered_params=ordered_params)

    def model_backgrounds_floors(self, model_names: list[str]) -> dict:
        """GET /collections/models/backgrounds/floors?models=a,b,c
        Confirmed: collection_id / short_name params are ignored; floor is
        global per model name. backdrops param is also ignored -- all
        backgrounds are always returned, filter client-side.
        Confirmed: the server silently drops model names it doesn't
        recognize -- a request for N names can return fewer than N keys
        with no error. Callers must never assume everything requested came
        back; see floors.py for the retry-once-then-give-up handling.
        This endpoint drops the connection (reset, not 403) if Origin/
        Referer/User-Agent are missing -- that failure mode is WafReset.
        """
        if not model_names:
            return {"model_backgrounds": {}}
        params = {"models": ",".join(model_names)}
        try:
            return self._get("/collections/models/backgrounds/floors", params)
        except TransientError as exc:
            # This endpoint's known failure signature for missing WAF
            # headers is a bare connection reset -- reclassify here since
            # this is the one endpoint where that has been observed.
            raise WafReset(str(exc)) from exc

    def search_pair_floor(
        self, collection_id: str, model_name: str, backdrop_name: str, limit: int = 20, offset: int = 0
    ) -> dict:
        """GET /nfts/search filtered to one exact collection+model+backdrop
        triple, sorted by price ascending -- the actual source of truth
        for combo-floor (see pair_floor.py).

        Confirmed live, both facts critical:
        - filter_by_models / filter_by_backdrops / filter_by_collections
          are the correct param names (comma-separated string values);
          the bracketed form (filter_by_models[]=X) gets the connection
          reset. Unprefixed model=/backdrop= are silently ignored (200 OK,
          unfiltered data) -- the worst kind of wrong: no error at all.
        - collection_id actually filters here (unlike the floors
          endpoint, which ignores it) and MUST always be passed.
        - PARAMETER ORDER IS SIGNIFICANT: sort must be first, or the sort
          is silently not applied (confirmed: prices came back in
          listed_at order, not price order, when sort was placed after
          filter_by_models). Hence the explicit ordered_params call
          instead of a dict.
        """
        ordered_params = [
            ("sort", "price_asc"),
            ("limit", str(limit)),
            ("offset", str(offset)),
            ("collection_id", collection_id),
            ("filter_by_models", model_name),
            ("filter_by_backdrops", backdrop_name),
        ]
        return self._get("/nfts/search", ordered_params=ordered_params)

    def search_model_floor(self, collection_id: str, model_name: str, limit: int = 50, offset: int = 0) -> dict:
        """GET /nfts/search filtered to collection+model, WITHOUT a
        backdrop filter -- the fallback comparison level for pairs where
        model+backdrop has too few (or zero) OTHER active listings to
        compare against. Confirmed live: most (model, backdrop) pairs
        have exactly one active listing (336 pairs measured with one,
        zero with ten) -- self-exclusion at the pair level then leaves
        nothing to compare to far more often than not. Model-level is a
        deliberately coarser fallback (different backdrops price
        differently -- see pair_floor.py/report.py for why pair- and
        model-level signals are never combined into one distribution).
        Same param order discipline as search_pair_floor -- sort first,
        collection_id always passed.
        """
        ordered_params = [
            ("sort", "price_asc"),
            ("limit", str(limit)),
            ("offset", str(offset)),
            ("collection_id", collection_id),
            ("filter_by_models", model_name),
        ]
        return self._get("/nfts/search", ordered_params=ordered_params)

    def search_by_ids(self, ids: list[str], limit: int | None = None) -> dict:
        """GET /nfts/search?ids=<id1>,<id2>,...&limit=<N> -- confirmed
        live: /nfts/search accepts a comma-separated `ids` param
        (CONFIRMED for multiple values in one call: a 2-id query
        returned 200 with both results, correct status/price for each --
        not just the single-id case) and returns those exact listings
        with their CURRENT status/price. Used by poller.py's pre-send
        freshness check and the listing-lifecycle background check. Not
        used in the hot collection loop.

        `limit` MUST be passed explicitly, equal to (at least) the
        number of ids requested -- confirmed max effective page size on
        this endpoint is 50 (see search()); without an explicit limit
        the server's default page size can silently truncate the
        result set below the number of ids asked for. Defaults to
        len(ids) (capped at 50) when not given.
        """
        effective_limit = limit if limit is not None else min(len(ids), 50)
        ordered_params = [("ids", ",".join(ids)), ("limit", str(effective_limit))]
        return self._get("/nfts/search", ordered_params=ordered_params)

    def market_config(self) -> dict:
        """GET /market/config. Called once at poller startup and then
        every CONFIG_REFRESH_SEC -- fees are NOT constants (user_cashback
        was observed at 0.05 and then 0 within the same day).
        """
        return self._get("/market/config", {})
