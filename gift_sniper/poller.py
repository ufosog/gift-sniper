from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import config, db, journal_config, journal_db, lot_check, paper_journal
from .auth import AuthManager
from .cross_check import BLOCKING_VERDICTS, cross_check
from .errors import AuthInvalid, RateLimited, TransientError, WafBlock
from .floors import FloorCache
from .models import FloorSnapshot, Listing, MarketConfigSnapshot
from .mrkt_client import build_default_mrkt_client
from .money import to_nano
from .notifier import CommandHandler, TelegramNotifier, passes_notify_threshold, select_signals_to_send
from .own_floors import OwnFloor, own_combo_floor
from .pair_floor import PairFloorCache
from .parsing import UnparseableListing, parse_listing
from .portals_client import PortalsClient
from .report import _percentile
from .signals import clean_signals
from .tonnel_client import TonnelClient, TonnelError

logger = logging.getLogger("gift_sniper.poller")


def _in_dual_floor_sample(external_id: str, sample_pct: int) -> bool:
    """Deterministic selection for DUAL_FLOOR_SAMPLE_PCT -- see config.py.
    Uses zlib.crc32, NOT Python's built-in hash(), because str hashing is
    randomized per-process (PYTHONHASHSEED) by default: the same
    external_id would land in or out of the sample differently across
    restarts with the builtin, defeating the entire point of a
    deterministic, repeatable sample.
    """
    if sample_pct <= 0:
        return False
    return zlib.crc32(external_id.encode("utf-8")) % 100 < sample_pct


def _compute_floor_sanity(collection_floor_nano: int | None, api_combo_floor_nano: int | None) -> str:
    """Diagnostic-only sanity check on the API combo-floor. Confirmed
    live that the API floor is frequently wrong (model names collide
    across unrelated collections, and the endpoint has no collection
    scoping) -- this catches the worst, most implausible cases so they
    can be excluded from reporting, but it is NOT the fix. The fix is
    pair_floor_nano, computed independently in pair_floor.py.
    """
    if collection_floor_nano is None or api_combo_floor_nano is None:
        return "no_data"
    if api_combo_floor_nano > collection_floor_nano * config.FLOOR_SANITY_MAX_RATIO:
        return "suspect"
    return "ok"


def _current_usd_rate(conn: sqlite3.Connection) -> Decimal:
    """USD conversion for notifier.py's profit_usd -- reuses the
    usd_course field already collected from /market/config (see
    maybe_refresh_market_config) instead of introducing a separate manual
    env var to keep in sync. Falls back to Decimal(1) (a placeholder, NOT
    a real rate) if no market_config snapshot has been observed yet --
    logged once so a Decimal(1)-based profit_usd in early notifications
    isn't silently mistaken for a real conversion.
    """
    row = db.get_latest_market_config(conn)
    if row is None or row["usd_course"] is None:
        logger.warning("no market_config usd_course available yet -- profit_usd will use a Decimal(1) placeholder")
        return Decimal(1)
    try:
        return Decimal(row["usd_course"])
    except InvalidOperation:
        logger.warning("market_config usd_course=%r is not a valid decimal -- using Decimal(1) placeholder", row["usd_course"])
        return Decimal(1)


def _decimal_field(raw: dict, key: str) -> Decimal | None:
    val = raw.get(key)
    if val is None:
        return None
    try:
        return Decimal(str(val))
    except InvalidOperation:
        return None


class Poller:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: PortalsClient,
        auth: AuthManager,
        floor_cache: FloorCache,
        pair_floor_cache: PairFloorCache | None = None,
        notifier: TelegramNotifier | None = None,
        command_handler: CommandHandler | None = None,
        tonnel_client: TonnelClient | None = None,
        mrkt_client=None,
        journal_conn=None,
    ):
        self.conn = conn
        # Paper journal (journal.db) -- None means off. Never affects
        # collection or sending, see paper_journal.record_signals_safely.
        self.journal_conn = journal_conn
        self._journal_uptime = paper_journal.UptimeTracker(journal_conn, "portals") if journal_conn is not None else None
        self.client = client
        self.auth = auth
        self.floor_cache = floor_cache
        self.pair_floor_cache = pair_floor_cache or PairFloorCache(client)
        self.notifier = notifier
        self.command_handler = command_handler
        # Constructed unconditionally (no network call happens at
        # construction time) -- config.CROSS_CHECK_ENABLED gates
        # whether it's ever actually called, in _maybe_cross_check().
        self.tonnel_client = tonnel_client or TonnelClient()
        # Правка 2 (MRKT third-neighbour delivery): None unless the
        # caller (build_default_poller) actually has MRKT_ACCESS_TOKEN --
        # unlike tonnel_client, there is no "construct anyway, gate the
        # call" option here, since MrktClient REQUIRES a token_provider
        # (see mrkt_client.py) and per spec its absence must degrade to
        # "not queried", not crash. cross_check() already handles
        # mrkt_client=None as "skip this neighbour" (see cross_check.py).
        self.mrkt_client = mrkt_client
        self._backoff_sec = config.POLL_INTERVAL_SEC
        self._last_config_fetch_mono: float | None = None
        self._last_floor_worker_mono: float | None = None
        self._last_lifecycle_check_mono: float | None = None
        self._start_mono: float | None = None
        self._iterations = 0
        self._delays_sec: list[float] = []
        # Look-back bound for clean_signals(since=...) on each notify
        # check -- a modest 1h default, NOT unbounded history: true
        # anti-duplicate correctness comes from db.is_alert_sent /
        # alerts_sent (durable across restarts), this is purely a
        # performance bound so a restart doesn't re-scan the entire
        # price_history table looking for signals to (re-)check.
        self._last_notify_since: datetime = datetime.now(timezone.utc) - timedelta(hours=1)
        self.stats = {
            "new_listings": 0,
            "pagination_cap_hits": 0,
            "rate_limited_count": 0,
            "parse_errors": 0,
            "listings_without_listed_at": 0,
            "items_seen_total": 0,
            "items_already_known": 0,
            "pages_fetched": 0,
            "collect_filtered_count": 0,
            "price_changes_seen": 0,
            "price_drops": 0,
            "price_raises": 0,
            "price_drops_above_threshold": 0,
            "floor_ok_pair": 0,
            "floor_alone_in_pair": 0,
            "floor_ok_model": 0,
            "floor_no_data": 0,
            "dual_floor_samples": 0,
            "signals_sent": 0,
            "signals_send_failed": 0,
            "signals_stale": 0,
            "signals_suppressed_cooldown": 0,
            # Правка 5: one counter per cross_check.py verdict value --
            # names match VERDICT_* in cross_check.py exactly, so
            # _maybe_cross_check can increment by verdict string alone
            # ("if signal.cross_verdict in self.stats").
            "sent_no_neighbour": 0,
            "sent_neighbour_higher": 0,
            "skipped_neighbour_cheaper": 0,
            "neighbour_thin": 0,
            "error": 0,
            "lifecycle_newly_gone": 0,
            "lifecycle_not_returned": 0,
            # Правка 4 (two-writer SQLite contention): "database is
            # locked" must never be swallowed silently -- counted here so
            # it's visible in the run summary whether contention remains
            # after the WAL/busy_timeout fix (see db.connect()). Counted,
            # logged, and the current poll cycle is skipped -- NOT a
            # process crash, same discipline as a transient network error.
            "db_locked_count": 0,
        }
        self._floor_pending_samples: list[int] = []

    # --- market config -------------------------------------------------

    def maybe_refresh_market_config(self) -> None:
        now_mono = time.monotonic()
        due = (
            self._last_config_fetch_mono is None
            or (now_mono - self._last_config_fetch_mono) >= config.CONFIG_REFRESH_SEC
        )
        if not due:
            return
        self._last_config_fetch_mono = now_mono
        try:
            raw = self.client.market_config()
        except (AuthInvalid, WafBlock, TransientError, RateLimited) as exc:
            logger.error("failed to fetch /market/config: %s", exc)
            return

        snapshot = MarketConfigSnapshot(
            fetched_at=datetime.now(timezone.utc),
            raw=raw,
            commission=_decimal_field(raw, "commission"),
            offer_fee=_decimal_field(raw, "offer_fee"),
            withdrawal_fee=_decimal_field(raw, "withdrawal_fee"),
            user_cashback=_decimal_field(raw, "user_cashback"),
            usd_course=_decimal_field(raw, "usd_course"),
        )
        changed = db.insert_market_config_snapshot_if_changed(self.conn, snapshot)
        if changed:
            logger.info("market config snapshot changed, wrote new row: %s", raw)

    # --- listing lifecycle (Правка 2): disappearance tracking ------------
    # A listing disappearing from the /nfts/search stream is the only
    # observable signal we have that it MIGHT have sold -- it is NOT
    # proof of a sale (the owner could simply have delisted it). See
    # README: this distinction must never be blurred when interpreting
    # listing_lifecycle data.

    def _touch_lifecycle(self, listing: Listing, seen_at: datetime) -> None:
        db.touch_listing_lifecycle(
            self.conn,
            "portals",
            listing.external_id,
            listing.collection_id,
            listing.model_name,
            listing.backdrop_name,
            listing.price_nano,
            seen_at,
        )

    def maybe_run_lifecycle_check(self) -> None:
        now_mono = time.monotonic()
        due = (
            self._last_lifecycle_check_mono is None
            or (now_mono - self._last_lifecycle_check_mono) >= config.LIFECYCLE_CHECK_INTERVAL_SEC
        )
        if not due:
            return
        self._last_lifecycle_check_mono = now_mono
        self._run_lifecycle_check_batch()

    def _run_lifecycle_check_batch(self) -> None:
        """Explicit GET /nfts/search?ids=... status check, confirmed
        live to return each queried listing's CURRENT status -- a
        withdrawn lot returned status="withdrawn", price=null. This
        REPLACED a time-since-last-seen rule that was confirmed to
        measure feed resurfacing, not real disappearance (see config.py
        for the concrete measured numbers). Batches
        LIFECYCLE_BATCH_SIZE not-yet-disappeared listings, oldest
        last_checked_at first.
        """
        batch = db.get_lifecycle_check_batch(self.conn, "portals", config.LIFECYCLE_BATCH_SIZE)
        if not batch:
            return
        ids = [row["listing_external_id"] for row in batch]

        try:
            # Explicit limit=len(ids): confirmed live that without it,
            # the server's default page size can silently truncate the
            # result set below the number of ids requested -- see
            # PortalsClient.search_by_ids().
            resp = self.client.search_by_ids(ids, limit=len(ids))
        except (AuthInvalid, WafBlock, TransientError, RateLimited) as exc:
            logger.error("lifecycle check batch failed: %s", exc)
            return

        now = datetime.now(timezone.utc)
        found_status: dict[str, str | None] = {}
        for item in resp.get("results", []):
            item_id = item.get("id")
            if item_id is not None:
                found_status[str(item_id)] = item.get("status")

        # Requested ids vs. returned ids: if this endpoint ever silently
        # drops requested ids (the same failure mode confirmed on the
        # floors-by-model-name endpoint), lifecycle_not_returned in the
        # run summary is how that would first become visible.
        not_returned = set(ids) - set(found_status.keys())
        if not_returned:
            self.stats["lifecycle_not_returned"] += len(not_returned)

        newly_gone = 0
        for ext_id in ids:
            if ext_id not in found_status:
                # Absence is NOT evidence of disappearance -- see
                # config.py / README. Logged, disappeared_at untouched.
                logger.warning(
                    "lifecycle check: listing %s missing from API response, not marking disappeared",
                    ext_id,
                )
                db.record_lifecycle_check_missing(self.conn, "portals", ext_id, now)
                continue

            status = found_status[ext_id]
            if status not in db.KNOWN_LIFECYCLE_STATUSES:
                logger.warning(
                    "lifecycle check: listing %s has unrecognized status=%r, treating as unknown",
                    ext_id, status,
                )
            db.record_lifecycle_status(self.conn, "portals", ext_id, status, now)
            if status in db.DISAPPEARED_STATUSES:
                newly_gone += 1

        if newly_gone:
            logger.info("lifecycle check: %d listings newly marked disappeared", newly_gone)
        self.stats["lifecycle_newly_gone"] += newly_gone

    # --- page fetch ------------------------------------------------------

    def _search_page(self, offset: int) -> list[dict] | None:
        """Returns the page's results (possibly an empty list -- a real,
        successful "nothing more here" signal), or None if the page could
        not be fetched AT ALL (auth/WAF failure, or transient retries
        exhausted). Callers MUST NOT treat these the same: None means the
        request failed and the iteration should abort with an error, not
        "we reached the end of pagination".
        """
        min_price = str(config.COLLECT_MIN_PRICE) if config.COLLECT_MIN_PRICE_NANO > 0 else None
        for attempt in range(3):
            try:
                resp = self.client.search(limit=50, offset=offset, min_price=min_price)
                return resp.get("results", [])
            except AuthInvalid:
                self.auth.mark_invalid()
                self._backoff_sec = min(self._backoff_sec * 2, 300)
                return None
            except WafBlock:
                logger.error("WAF_BLOCK on /nfts/search")
                return None
            except RateLimited as exc:
                logger.warning("rate limit exhausted client-side retries: %s", exc)
                return None
            except TransientError as exc:
                logger.warning("transient error attempt=%d offset=%d: %s", attempt, offset, exc)
                time.sleep(2**attempt)
        logger.error("giving up on page offset=%d after 3 transient failures", offset)
        return None

    # --- FAST PATH: pages + writes only, NO floor requests of any kind ---

    def _process_page_items(self, items: list[dict]) -> list[Listing]:
        """Parses and WRITES a page's worth of new items. This is the
        FAST PATH: no floor lookups here, of any kind (API combo, own, or
        pair) -- confirmed live that doing floor network calls inside this
        loop, at a measured ~8 listings/sec, stretched iterations badly
        enough to lose >99% of the stream. Every FloorSnapshot is written
        with pair_floor_status="pending"; see run_floor_worker() for the
        ANALYTICS PATH that fills it in afterwards.

        `raw` is only kept for listings at or above FLOOR_MIN_PRICE_NANO
        -- every parsed field (price, attributes, rarity, timestamps) is
        still stored for ALL listings regardless, so nothing needed for
        throughput stats or the name-collision backfill is lost.
        """
        listings: list[Listing] = []
        for item in items:
            try:
                listing = parse_listing(item)
            except UnparseableListing as exc:
                self.stats["parse_errors"] += 1
                logger.error("unparseable listing, raw=%s error=%s", item, exc)
                continue

            # Defensive net: COLLECT_MIN_PRICE is sent as a server-side
            # filter (see _search_page), so this should rarely trigger --
            # but if the server ever returns something under the
            # threshold anyway, it must NOT enter the DB at all. This is
            # a deliberate completeness/representativeness tradeoff, not
            # a storage optimization (that's FLOOR_MIN_PRICE, a separate
            # threshold -- see config.py).
            if (
                config.COLLECT_MIN_PRICE_NANO > 0
                and listing.price_nano is not None
                and listing.price_nano < config.COLLECT_MIN_PRICE_NANO
            ):
                self.stats["collect_filtered_count"] += 1
                continue

            if listing.price_nano is None or listing.price_nano < config.FLOOR_MIN_PRICE_NANO:
                listing.raw = None
            listings.append(listing)

        written: list[Listing] = []
        for listing in listings:
            snapshot = FloorSnapshot(
                listing_external_id=listing.external_id,
                model_name=listing.model_name or "",
                backdrop_name=listing.backdrop_name or "",
                api_combo_floor_nano=None,
                model_min_floor_nano=None,
                floor_fetched_at=listing.first_seen_at,
                floor_age_sec=0,
                raw_model_block={},
                pair_floor_status="pending",
            )
            db.upsert_listing_with_floor(self.conn, listing, snapshot)
            self._touch_lifecycle(listing, listing.first_seen_at)
            written.append(listing)

            if listing.listed_at is not None:
                delay = (listing.first_seen_at - listing.listed_at).total_seconds()
                self._delays_sec.append(delay)
            else:
                self.stats["listings_without_listed_at"] += 1

        return written

    def _process_known_items(self, items: list[dict]) -> None:
        """For pages' worth of ALREADY-KNOWN items: compare the page's
        price against what's stored and record any REAL change to
        price_history. Confirmed live: listed_at updates on a mere
        "touch" with no price change at all -- it is never used here to
        decide whether something changed, only the price is. A no-op
        (same price) writes nothing.
        """
        now = datetime.now(timezone.utc)
        for item in items:
            try:
                listing = parse_listing(item)
            except UnparseableListing as exc:
                self.stats["parse_errors"] += 1
                logger.error("unparseable known listing, raw=%s error=%s", item, exc)
                continue

            # Lifecycle last_seen_at must update for EVERY known listing
            # seen on this page, regardless of whether its price changed
            # -- a "touch" with no price change is still proof the
            # listing is still there (see touch_listing_lifecycle).
            self._touch_lifecycle(listing, now)

            if listing.price_nano is None:
                continue

            existing = db.get_listing_price_and_listed_at(self.conn, "portals", listing.external_id)
            if existing is None:
                continue  # race: not actually in the DB despite listing_exists() moments ago
            old_price_nano, old_listed_at = existing
            if old_price_nano is None or old_price_nano == listing.price_nano:
                continue  # no real price change -- nothing written

            self.stats["price_changes_seen"] += 1
            delta_pct = (Decimal(listing.price_nano - old_price_nano) / Decimal(old_price_nano)) * 100

            floor_at_drop_nano = None
            floor_listed_count_at_drop = 0
            floor_fetched_at = None
            floor_level_at_drop = None
            is_anomaly = False

            if listing.price_nano < old_price_nano:
                self.stats["price_drops"] += 1
                is_noise = abs(delta_pct) < config.PRICE_DROP_MIN_PCT

                # Burst check applies to every drop, noise or not -- a
                # rapid-fire sequence of tiny bot ticks is exactly the
                # kind of thing PRICE_DROP_BURST_SEC exists to catch too.
                burst_since = now - timedelta(seconds=config.PRICE_DROP_BURST_SEC)
                had_recent_drop = db.flag_burst_drops(
                    self.conn, "portals", listing.external_id, burst_since
                )
                if had_recent_drop or abs(delta_pct) > config.PRICE_DROP_MAX_PCT:
                    is_anomaly = True

                if not is_noise:
                    self.stats["price_drops_above_threshold"] += 1
                    # Re-fetch the pair floor AT THE MOMENT OF THIS DROP,
                    # self-excluded (see pair_floor.py) -- the snapshot in
                    # floor_snapshots may be stale or, for a listing alone
                    # in its pair, simply wrong (it would BE that
                    # snapshot's floor). Only done above the noise
                    # threshold: ~20/hour measured, not worth gating
                    # further; never done for noise-level ticks.
                    if listing.model_name and listing.backdrop_name:
                        pair, _age = self.pair_floor_cache.get_fresh(
                            listing.collection_id,
                            listing.model_name,
                            listing.backdrop_name,
                            exclude_external_id=listing.external_id,
                        )
                        if pair.status == "ok":
                            floor_at_drop_nano = pair.floor_excluding_self_nano
                            floor_listed_count_at_drop = pair.listed_count_excluding_self
                            floor_fetched_at = pair.fetched_at
                            floor_level_at_drop = "pair"
                        elif pair.status == "alone_in_pair":
                            # Pair level has nothing to compare against --
                            # fall back to the model level (see
                            # pair_floor.py module docstring / Правка 4).
                            model, _model_age = self.pair_floor_cache.get_model_floor_fresh(
                                listing.collection_id,
                                listing.model_name,
                                exclude_external_id=listing.external_id,
                            )
                            if model.status == "ok":
                                floor_at_drop_nano = model.floor_excluding_self_nano
                                floor_listed_count_at_drop = model.listed_count_excluding_self
                                floor_fetched_at = model.fetched_at
                                floor_level_at_drop = "model"
            else:
                self.stats["price_raises"] += 1
                is_noise = False  # noise flag is only meaningful for drops

            db.record_price_change(
                self.conn,
                "portals",
                listing.external_id,
                old_price_nano,
                listing.price_nano,
                delta_pct,
                is_noise,
                old_listed_at,
                listing.listed_at,
                now,
                floor_at_drop_nano=floor_at_drop_nano,
                floor_listed_count_at_drop=floor_listed_count_at_drop,
                floor_fetched_at=floor_fetched_at,
                is_anomaly=is_anomaly,
                floor_level_at_drop=floor_level_at_drop,
            )

    def poll_once(self) -> list[Listing]:
        """Pages through /nfts/search from offset 0. EVERY page is
        processed in full, always -- known and new items are confirmed
        live to be INTERLEAVED within a page (sorting by listed_at does
        not guarantee all-new-then-all-known), so bailing out on the
        first known id silently drops any new items after it on the same
        page. Pagination continues to the next page as long as the
        current page contained at least one NEW item; it stops when a
        page is either empty (real end of stream) or entirely composed of
        already-known ids (caught up to the previous iteration), or the
        pagination cap is hit. A page fetch that outright failed (None)
        aborts the iteration with a logged error, distinct from a
        genuinely empty page.
        """
        self.maybe_refresh_market_config()

        result: list[Listing] = []
        offset = 0

        while True:
            page = self._search_page(offset)

            if page is None:
                logger.error("aborting poll iteration: page fetch failed at offset=%d", offset)
                break

            self.stats["pages_fetched"] += 1

            if not page:
                break  # genuinely empty page: real end of pagination

            self.stats["items_seen_total"] += len(page)

            new_items = []
            known_items = []
            for item in page:
                ext_id = item.get("id")
                if ext_id is None:
                    logger.warning("dropping item with no id: %s", item)
                    continue
                if db.listing_exists(self.conn, "portals", str(ext_id)):
                    self.stats["items_already_known"] += 1
                    known_items.append(item)
                else:
                    new_items.append(item)

            result.extend(self._process_page_items(new_items))
            # Price-change detection on known items does NOT affect the
            # pagination stop condition below -- it is keyed on new_items
            # only. Prices change constantly; if a changed-but-known page
            # counted as "still going", the poller would paginate forever.
            self._process_known_items(known_items)

            if not new_items:
                # Every id on this page was already known -- we've caught
                # up to where the previous iteration left off.
                break

            if offset + 50 >= config.MAX_PAGES_PER_ITERATION * 50:
                self.stats["pagination_cap_hits"] += 1
                logger.warning(
                    "pagination cap hit, possible data loss: offset=%d cap_pages=%d",
                    offset,
                    config.MAX_PAGES_PER_ITERATION,
                )
                break

            offset += 50

        self.stats["new_listings"] += len(result)
        self.stats["rate_limited_count"] = self.client.rate_limited_count
        return result

    # --- ANALYTICS PATH: floor computation, decoupled from collection ---

    def run_floor_worker(self, limit: int | None = None) -> int:
        """Processes up to `limit` (default FLOOR_BATCH_PER_RUN) listings
        still awaiting floor computation. Never called from poll_once /
        _process_page_items -- this is deliberately a separate pass so a
        slow or rate-limited floor lookup can never stretch the FAST PATH
        collection loop. Returns how many rows were actually processed.
        """
        limit = limit if limit is not None else config.FLOOR_BATCH_PER_RUN

        resolved = db.mark_pending_below_threshold_as_no_data(self.conn, config.FLOOR_MIN_PRICE_NANO)
        if resolved:
            logger.info("resolved %d pending rows below FLOOR_MIN_PRICE with no network call", resolved)

        pending_rows = db.get_pending_floor_rows(self.conn, config.FLOOR_MIN_PRICE_NANO, limit)
        if not pending_rows:
            return 0

        model_names = {row["model_name"] for row in pending_rows if row["model_name"]}
        self.floor_cache.warm(model_names)

        for row in pending_rows:
            model_name = row["model_name"] or None
            backdrop_name = row["backdrop_name"] or None
            collection_id = row["collection_id"]
            collection_name = row["collection_name"]
            collection_floor_nano = row["collection_floor_nano"]
            first_seen_at = datetime.fromisoformat(row["first_seen_at"])

            if model_name:
                api_snapshot = self.floor_cache.snapshot_for(row["external_id"], model_name, backdrop_name)
            else:
                api_snapshot = self.floor_cache.below_threshold_snapshot(row["external_id"], model_name, backdrop_name)

            if model_name and backdrop_name:
                own = own_combo_floor(self.conn, collection_name, model_name, backdrop_name, first_seen_at)
            else:
                own = OwnFloor(floor_nano=None, sample_size=0, confidence="none", oldest_seen_at=None, newest_seen_at=None)

            floor_sanity = _compute_floor_sanity(collection_floor_nano, api_snapshot.api_combo_floor_nano)

            model_floor_excl_self_nano, model_listed_count_excl_self, model_floor_status = None, 0, "no_data"

            if model_name and backdrop_name:
                pair, pair_age_sec = self.pair_floor_cache.get(
                    collection_id, model_name, backdrop_name, exclude_external_id=row["external_id"]
                )
                pair_floor_nano = pair.floor_nano
                pair_listed_count = pair.listed_count
                pair_floor_status = pair.status
                pair_floor_excl_self_nano = pair.floor_excluding_self_nano
                pair_listed_count_excl_self = pair.listed_count_excluding_self
                pair_self_was_floor = pair.self_was_floor

                # Model-level fallback ONLY when the pair level has no one
                # to compare against -- never when pair already gave "ok",
                # to avoid spending extra rate-limit budget (see Правка 2).
                if pair_floor_status == "alone_in_pair" and model_name:
                    model, _model_age_sec = self.pair_floor_cache.get_model_floor(
                        collection_id, model_name, exclude_external_id=row["external_id"]
                    )
                    model_floor_excl_self_nano = model.floor_excluding_self_nano
                    model_listed_count_excl_self = model.listed_count_excluding_self
                    model_floor_status = model.status
                # Dual-floor sampling (opt-in, DUAL_FLOOR_SAMPLE_PCT=0 by
                # default): for a deterministic sample of rows where the
                # pair level ALREADY gave "ok", also fetch the model
                # floor purely for comparison -- there is otherwise no
                # data anywhere to measure how much the two levels
                # disagree (confirmed by model_level_audit.py: "rows with
                # BOTH pair and model floor filled: 0"). Written into the
                # same existing model_floor_* columns.
                elif (
                    pair_floor_status == "ok"
                    and model_name
                    and _in_dual_floor_sample(row["external_id"], config.DUAL_FLOOR_SAMPLE_PCT)
                ):
                    model, _model_age_sec = self.pair_floor_cache.get_model_floor(
                        collection_id, model_name, exclude_external_id=row["external_id"]
                    )
                    model_floor_excl_self_nano = model.floor_excluding_self_nano
                    model_listed_count_excl_self = model.listed_count_excluding_self
                    model_floor_status = model.status
                    self.stats["dual_floor_samples"] += 1
            else:
                pair_floor_nano, pair_listed_count, pair_floor_status, pair_age_sec = None, 0, "no_data", 0
                pair_floor_excl_self_nano, pair_listed_count_excl_self, pair_self_was_floor = None, 0, False

            if pair_floor_status == "ok":
                self.stats["floor_ok_pair"] += 1
            elif pair_floor_status == "alone_in_pair" and model_floor_status == "ok":
                self.stats["floor_ok_model"] += 1
            elif pair_floor_status == "alone_in_pair":
                self.stats["floor_alone_in_pair"] += 1
            else:
                self.stats["floor_no_data"] += 1

            result_snapshot = FloorSnapshot(
                listing_external_id=row["external_id"],
                model_name=model_name or "",
                backdrop_name=backdrop_name or "",
                api_combo_floor_nano=api_snapshot.api_combo_floor_nano,
                model_min_floor_nano=api_snapshot.model_min_floor_nano,
                floor_fetched_at=datetime.now(timezone.utc),
                floor_age_sec=api_snapshot.floor_age_sec,
                raw_model_block=api_snapshot.raw_model_block,
                floor_skip_reason=api_snapshot.floor_skip_reason,
                own_combo_floor_nano=own.floor_nano,
                own_sample_size=own.sample_size,
                own_confidence=own.confidence,
                floor_sanity=floor_sanity,
                pair_floor_nano=pair_floor_nano,
                pair_listed_count=pair_listed_count,
                pair_floor_status=pair_floor_status,
                pair_floor_age_sec=pair_age_sec,
                pair_floor_excl_self_nano=pair_floor_excl_self_nano,
                pair_listed_count_excl_self=pair_listed_count_excl_self,
                pair_self_was_floor=pair_self_was_floor,
                model_floor_excl_self_nano=model_floor_excl_self_nano,
                model_listed_count_excl_self=model_listed_count_excl_self,
                model_floor_status=model_floor_status,
            )
            db.update_floor_analytics(self.conn, row["external_id"], result_snapshot)

        return len(pending_rows)

    def maybe_run_floor_worker(self) -> None:
        now_mono = time.monotonic()
        due = (
            self._last_floor_worker_mono is None
            or (now_mono - self._last_floor_worker_mono) >= config.FLOOR_WORKER_INTERVAL_SEC
        )
        if not due:
            return
        self._last_floor_worker_mono = now_mono
        processed = self.run_floor_worker()
        if processed:
            logger.info("floor worker processed %d listings", processed)
        # Per spec: "После каждого прохода ANALYTICS PATH поллер вызывает
        # clean_signals(...)" -- hooked to this same timer (not the FAST
        # PATH poll cadence), run unconditionally (even if `processed`
        # was 0 this tick) since new clean signals can surface from
        # price drops recorded independently of this batch's floor rows.
        self._maybe_notify()

    # --- Telegram notification (opt-in, NOTIFY_ENABLED) --------------------

    def _check_signal_still_fresh(self, signal) -> bool | None:
        """Правка 3: one extra /nfts/search request per signal about to
        be sent, immediately before sending. Confirmed live: querying by
        ids returns the listing's CURRENT status/price -- a withdrawn lot
        returned status="withdrawn", price=null. A signal computed
        seconds or minutes ago can already be gone or repriced by the
        time the poller actually gets to sending it.

        Returns True (still listed at the notified price), False
        (confirmed stale -- withdrawn/sold/repriced), or None (the check
        itself failed -- network/API error, NOT a confirmed stale state;
        callers must treat None like a send failure: retry later, never
        mark skipped_stale on an unverified guess).
        """
        try:
            resp = self.client.search_by_ids([signal.listing_external_id])
        except (AuthInvalid, WafBlock, TransientError, RateLimited) as exc:
            logger.error(
                "freshness check failed for %s: %s -- will retry, not marking stale",
                signal.listing_external_id, exc,
            )
            return None
        return lot_check.portals_available(resp, signal.new_price_nano)

    def _check_cooldown(self, signal) -> tuple[bool, bool]:
        """Returns (allowed, resend_after_drop). Confirmed live: Cupid
        Charm #8599 was notified twice within a minute (22.00, then
        21.60) -- two genuinely distinct price_history rows, so the
        (listing_external_id, observed_at) anti-duplicate check in
        alerts_sent never catches this repeat-notification failure mode.
        SIGNAL_COOLDOWN_MIN: if this listing was already notified more
        recently than that, suppress -- UNLESS the new price is lower
        than the last-notified price by more than
        SIGNAL_RESEND_DROP_PCT, in which case it's sent anyway, marked
        as a price-drop resend (see notifier.format_caption).
        """
        last = db.get_last_sent_alert_for_listing(self.conn, "portals", signal.listing_external_id)
        if last is None:
            return True, False

        elapsed_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last["sent_at"])).total_seconds() / 60
        if elapsed_min >= config.SIGNAL_COOLDOWN_MIN:
            return True, False

        last_price = Decimal(last["new_price_nano"])
        new_price = Decimal(signal.new_price_nano)
        if last_price <= 0:
            return True, False
        drop_pct = (last_price - new_price) / last_price * 100
        if drop_pct > config.SIGNAL_RESEND_DROP_PCT:
            return True, True
        return False, False

    def _maybe_cross_check(self, signal) -> None:
        """Правка 3 (unified-notification delivery): a PURE PRE-SEND
        FILTER now -- after all existing filters and cooldown/freshness
        gates, but BEFORE sending, query Tonnel's own pair floor for the
        same (collection, model, backdrop), self-excluded by gift_num,
        via the SHARED cross_check.cross_check() mechanism (see
        cross_check.py). Mutates `signal` in place with the result and
        cross_verdict -- the CALLER (_maybe_notify) decides whether to
        send based on `signal.cross_verdict in cross_check.BLOCKING_VERDICTS`.
        Never raises -- a Tonnel-side failure degrades to verdict "error"
        (SEND, never blocks), logged, exactly like a Portals freshness-
        check failure never blocks a send.

        The earlier separate "cross-market" notification (buy on Tonnel,
        sell on Portals) is REMOVED entirely (Правка 1) -- a cross-
        marketplace gap is no longer its own signal type, only an input
        to this filter. Confirmed live, the two-signal-types design
        produced a real contradiction: Victory Medal #86056 was rejected
        as a Portals signal (verdict="worse"-equivalent) AND simultaneously
        sent as a separate cross-market signal -- one lot, two opposite
        decisions. That can no longer happen: there is exactly one
        decision per signal now.
        """
        if not config.CROSS_CHECK_ENABLED:
            return
        if not (signal.collection_name and signal.model_name and signal.backdrop_name):
            return

        now = datetime.now(timezone.utc)
        cross_check(self.conn, signal, tonnel_client=self.tonnel_client, mrkt_client=self.mrkt_client, now=now)

        if signal.cross_verdict in self.stats:
            self.stats[signal.cross_verdict] += 1

    def _maybe_notify(self) -> None:
        if self.notifier is None and self.journal_conn is None:
            return

        since = self._last_notify_since
        now = datetime.now(timezone.utc)
        usd_rate = _current_usd_rate(self.conn)
        all_signals = clean_signals(self.conn, since=since, usd_rate=usd_rate, now=now)
        self._last_notify_since = now

        try:
            if self.notifier is not None:
                self._send_signals(all_signals)
        finally:
            # Paper journal: every clean signal, sent or not. Signals that
            # reached the cross-check carry its verdict by now.
            paper_journal.record_signals_safely(self.journal_conn, all_signals)

    def _send_signals(self, all_signals) -> None:
        candidates = [
            s for s in all_signals
            if s.floor_level in config.NOTIFY_LEVELS
            and passes_notify_threshold(s)
            and not db.is_alert_sent(self.conn, "portals", s.listing_external_id, s.observed_at)
        ]
        if not candidates:
            return

        to_send, extra = select_signals_to_send(candidates, config.NOTIFY_MAX_PER_MINUTE)
        for signal in to_send:
            allowed, resend_after_drop = self._check_cooldown(signal)
            if not allowed:
                self.stats["signals_suppressed_cooldown"] += 1
                logger.info(
                    "signal suppressed by cooldown: listing_external_id=%s observed_at=%s",
                    signal.listing_external_id, signal.observed_at,
                )
                continue

            fresh = self._check_signal_still_fresh(signal)
            if fresh is None:
                # Verification failed (network/API error) -- NOT a
                # confirmed stale state. Retried on the next notify
                # check, exactly like a send failure.
                self.stats["signals_send_failed"] += 1
                continue
            if fresh is False:
                db.mark_alert_sent(
                    self.conn, "portals", signal.listing_external_id, signal.observed_at,
                    datetime.now(timezone.utc), status="skipped_stale",
                )
                self.stats["signals_stale"] += 1
                logger.info(
                    "signal stale, not sent: listing_external_id=%s observed_at=%s",
                    signal.listing_external_id, signal.observed_at,
                )
                continue

            self._maybe_cross_check(signal)

            if signal.cross_verdict in BLOCKING_VERDICTS:
                # Правка 3: the neighbour has the same pair for the same
                # price or only slightly more -- nothing to gain, a
                # buyer would just go there. Never sent, recorded
                # distinctly so it's not confused with a real send
                # failure (see КАК ТЕСТИРОВАТЬ items 4/5).
                db.mark_alert_sent(
                    self.conn, "portals", signal.listing_external_id, signal.observed_at,
                    datetime.now(timezone.utc), status="skipped_cross_worse",
                )
                logger.info(
                    "signal skipped, cross_verdict=%s: listing_external_id=%s observed_at=%s "
                    "price=%s neighbour_floor=%s",
                    signal.cross_verdict, signal.listing_external_id, signal.observed_at,
                    signal.new_price_nano, signal.neighbour_floor_nano,
                )
                continue

            ok = self.notifier.send_signal(signal, resend_after_drop=resend_after_drop)
            if ok:
                db.mark_alert_sent(self.conn, "portals", signal.listing_external_id, signal.observed_at, datetime.now(timezone.utc))
                self.stats["signals_sent"] += 1
            else:
                # Deliberately NOT marked sent -- retried on the next
                # notify check (КАК ТЕСТИРОВАТЬ item 5). A send failure
                # must never take down the poll loop; send_signal() never
                # raises, only returns False.
                self.stats["signals_send_failed"] += 1
                logger.error(
                    "signal not sent, will retry: listing_external_id=%s observed_at=%s",
                    signal.listing_external_id, signal.observed_at,
                )
        if extra:
            self.notifier.send_text(f"...и ещё {extra} сигналов, см. /last")

    def _maybe_process_commands(self) -> None:
        if self.command_handler is None:
            return
        try:
            self.command_handler.poll_once()
        except Exception:
            # Command handling is a convenience feature -- it must never
            # take the poll loop down, same principle as _maybe_notify.
            logger.exception("command handler poll_once failed")

    def _sample_floor_pending(self) -> None:
        self._floor_pending_samples.append(db.count_pending_floor_rows(self.conn))

    def _floor_pending_trend(self) -> str:
        samples = self._floor_pending_samples
        if len(samples) < 2:
            return "stable"
        if samples[-1] > samples[0]:
            return "growing"
        if samples[-1] < samples[0]:
            return "shrinking"
        return "stable"

    def _floor_worker_not_keeping_up(self) -> bool:
        """True if floor_pending_count never decreased across the whole
        run AND ended up higher than it started -- i.e. the ANALYTICS
        PATH is falling further behind every sample, not just noisy.
        """
        samples = self._floor_pending_samples
        if len(samples) < 2:
            return False
        non_decreasing = all(samples[i] <= samples[i + 1] for i in range(len(samples) - 1))
        return non_decreasing and samples[-1] > samples[0]

    # --- run loop + summary ------------------------------------------------

    def run_forever(self, run_seconds: float | None = None) -> None:
        """Runs the poll loop. If run_seconds is given, stops itself after
        that many seconds and prints the run summary -- used for bounded
        acceptance runs (e.g. the 30-minute live test). Ctrl+C also prints
        the summary before exiting. The ANALYTICS PATH floor worker runs
        on its own timer (FLOOR_WORKER_INTERVAL_SEC), independent of the
        FAST PATH collection cadence.
        """
        self._start_mono = time.monotonic()
        self._sample_floor_pending()
        try:
            while True:
                if run_seconds is not None and (time.monotonic() - self._start_mono) >= run_seconds:
                    break
                start = time.monotonic()
                try:
                    new = self.poll_once()
                    self._iterations += 1
                    if self._journal_uptime is not None:
                        self._journal_uptime.heartbeat()
                    self.maybe_run_floor_worker()
                    self._maybe_process_commands()
                    self.maybe_run_lifecycle_check()
                    self._sample_floor_pending()
                    if new:
                        self._backoff_sec = config.POLL_INTERVAL_SEC
                    logger.info("poll cycle: %d new listings, stats=%s", len(new), self.stats)
                except sqlite3.OperationalError as exc:
                    if "database is locked" not in str(exc):
                        raise
                    # Правка 4: even with WAL + busy_timeout (db.connect()),
                    # a writer can still exhaust the wait budget under
                    # heavy two-poller contention. Counted and logged, NOT
                    # silently swallowed -- but a single locked cycle must
                    # not crash the whole process; the next cycle retries.
                    self.stats["db_locked_count"] += 1
                    logger.error("database is locked, skipping this poll cycle: %s", exc)
                elapsed = time.monotonic() - start
                time.sleep(max(0.0, self._backoff_sec - elapsed))
        except KeyboardInterrupt:
            pass
        finally:
            self.print_summary()

    def print_summary(self) -> None:
        duration = (time.monotonic() - self._start_mono) if self._start_mono is not None else 0.0
        new_listings = self.stats["new_listings"]
        per_sec = (new_listings / duration) if duration > 0 else 0.0
        delays_sorted = sorted(self._delays_sec)

        search_requests = self.client.request_counts.get("/nfts/search", 0)
        floor_requests = self.client.request_counts.get(
            "/collections/models/backgrounds/floors", 0
        )
        floor_pending_count = db.count_pending_floor_rows(self.conn)
        floor_cache_hits = self.floor_cache.cache_hits + self.pair_floor_cache.cache_hits

        print()
        print("=== Gift Sniper run summary ===")
        print(f"duration_sec: {duration:.1f}")
        print(f"iterations: {self._iterations}")
        print(f"collect_min_price: {config.COLLECT_MIN_PRICE if config.COLLECT_MIN_PRICE_NANO > 0 else 'disabled'}")
        print(f"pages_fetched: {self.stats['pages_fetched']}")
        print(f"items_seen_total: {self.stats['items_seen_total']}")
        print(f"items_already_known: {self.stats['items_already_known']}")
        print(f"collect_filtered_count: {self.stats['collect_filtered_count']}")
        print(f"new_listings: {new_listings}")
        print(f"new_listings_per_sec: {per_sec:.2f}")
        print(f"price_changes_seen: {self.stats['price_changes_seen']}")
        print(f"price_drops: {self.stats['price_drops']}")
        print(f"price_raises: {self.stats['price_raises']}")
        print(f"price_drops_above_threshold: {self.stats['price_drops_above_threshold']}")
        print(f"search_requests: {search_requests}")
        print(f"floor_requests: {floor_requests}")
        print(f"floor_cache_hits: {floor_cache_hits}")
        print(f"rate_limited_429_count: {self.client.rate_limited_count}")
        print(f"preemptive_pause_count: {self.client.preemptive_pause_count}")
        print(f"pagination_cap_hits: {self.stats['pagination_cap_hits']}")
        print(f"waf_reset_count: {self.floor_cache.waf_reset_count}")
        print(f"parse_errors: {self.stats['parse_errors']}")
        print(
            f"detection_delay_sec: median={_percentile(delays_sorted, 0.5)} "
            f"p90={_percentile(delays_sorted, 0.9)} n={len(delays_sorted)}"
        )
        print(f"listings_without_listed_at: {self.stats['listings_without_listed_at']}")
        print(f"db_locked_count: {self.stats['db_locked_count']}")
        print(f"floor_pending_count: {floor_pending_count}")
        print(f"floor_pending_trend: {self._floor_pending_trend()}")
        print(f"floor_ok_pair: {self.stats['floor_ok_pair']}")
        print(f"floor_alone_in_pair: {self.stats['floor_alone_in_pair']}")
        print(f"floor_ok_model: {self.stats['floor_ok_model']}")
        print(f"floor_no_data: {self.stats['floor_no_data']}")
        print(f"dual_floor_samples: {self.stats['dual_floor_samples']}")
        print(f"lifecycle_newly_gone: {self.stats['lifecycle_newly_gone']}")
        print(f"lifecycle_not_returned: {self.stats['lifecycle_not_returned']}")
        print(f"signals_sent: {self.stats['signals_sent']}")
        print(f"signals_send_failed: {self.stats['signals_send_failed']}")
        print(f"signals_stale: {self.stats['signals_stale']}")
        print(f"signals_suppressed_cooldown: {self.stats['signals_suppressed_cooldown']}")
        print(f"sent_no_neighbour: {self.stats['sent_no_neighbour']}")
        print(f"sent_neighbour_higher: {self.stats['sent_neighbour_higher']}")
        print(f"skipped_neighbour_cheaper: {self.stats['skipped_neighbour_cheaper']}")
        print(f"neighbour_thin: {self.stats['neighbour_thin']}")
        print(f"cross_check_error: {self.stats['error']}")
        if self._floor_worker_not_keeping_up():
            print("WARNING: floor worker is not keeping up")


def build_default_poller(dsn: str = config.DB_DSN) -> Poller:
    conn = db.connect(dsn)
    # db.connect() already ran migrations; this is a second, defensive
    # check so a stale/unexpected schema is caught with a clear message
    # HERE, before any network request, rather than mid-batch-write on
    # the first poll cycle.
    db.verify_schema(conn)
    auth = AuthManager()
    client = PortalsClient(auth_provider=auth.get)
    floor_cache = FloorCache(client)
    pair_floor_cache = PairFloorCache(client)

    journal_conn = journal_db.connect(journal_config.JOURNAL_DB_DSN) if journal_config.PAPER_JOURNAL_ENABLED else None

    notifier = None
    command_handler = None
    if config.NOTIFY_ENABLED:
        # get_telegram_bot_token()/get_telegram_owner_id() raise
        # ConfigError if unset -- required ONLY when NOTIFY_ENABLED is
        # true, per spec; an ordinary (non-bot) run never needs these.
        # get_telegram_viewer_ids() is always optional (empty list by
        # default -- the original single-recipient behavior).
        bot_token = config.get_telegram_bot_token()
        owner_id = config.get_telegram_owner_id()
        viewer_ids = config.get_telegram_viewer_ids()
        notifier = TelegramNotifier(bot_token, chat_id=owner_id, viewer_chat_ids=viewer_ids)
        if config.BOT_COMMANDS_IN_POLLER:
            command_handler = CommandHandler(
                notifier, conn, owner_id=owner_id, viewer_ids=viewer_ids, journal_conn=journal_conn,
            )

    mrkt_client = build_default_mrkt_client()

    return Poller(
        conn, client, auth, floor_cache, pair_floor_cache, notifier=notifier, command_handler=command_handler,
        mrkt_client=mrkt_client, journal_conn=journal_conn,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-seconds",
        type=float,
        default=None,
        help="Stop automatically after this many seconds and print the run summary "
        "(used for bounded acceptance runs, e.g. the 30-minute live test).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    try:
        poller = build_default_poller()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to start: {exc}", file=sys.stderr)
        return 1
    poller.run_forever(run_seconds=args.run_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
