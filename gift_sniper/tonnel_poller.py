"""Full Tonnel collector AND signaller -- feed of listings, price
tracking, lifecycle (disappearance) tracking, and (this delivery) clean
signals + Telegram notifications. NOT a subclass of poller.Poller (per
spec, "наследование tonnel_poller от poller" is explicitly excluded) --
a separate process against an unrelated API, but reusing the SAME shared
db.py/models.py/money.py/signals.py/notifier.py functions Portals uses
(see db.py schema v15's marketplace columns, signals.py's marketplace
parameter) -- never a duplicated copy of them.

Runs NO CommandHandler of its own: getUpdates' offset is a single global
stream per bot token, and two independent processes polling it
independently would be a real conflict -- see README. This process only
SENDS (send_signal), gated by TONNEL_NOTIFY_ENABLED; /status and /last
stay exclusively poller.py's (Portals') commands.

Run: python -m gift_sniper.tonnel_poller
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import config, db, journal_config, journal_db, lot_check, paper_journal
from .cross_check import BLOCKING_VERDICTS, cross_check
from .models import Listing
from .mrkt_client import build_default_mrkt_client
from .money import to_nano
from .notifier import TelegramNotifier, passes_notify_threshold, select_signals_to_send
from .signals import clean_signals
from .tonnel_client import TonnelClient, TonnelError
from .tonnel_parsing import UnparseableTonnelListing, is_purchasable_tonnel_item, parse_tonnel_listing

logger = logging.getLogger("gift_sniper.tonnel_poller")

MARKETPLACE = "tonnel"


class TonnelPoller:
    def __init__(
        self,
        conn,
        tonnel_client: TonnelClient | None = None,
        notifier: TelegramNotifier | None = None,
        sleep_fn=time.sleep,
        portals_client=None,
        mrkt_client=None,
        journal_conn=None,
    ):
        self.conn = conn
        # Paper journal (journal.db) -- None means off.
        self.journal_conn = journal_conn
        self._journal_uptime = paper_journal.UptimeTracker(journal_conn, MARKETPLACE) if journal_conn is not None else None
        # Constructed with config.TONNEL_REQUEST_DELAY_MS explicitly --
        # independently tunable from TonnelClient's own 600ms default and
        # from Portals' REQUEST_DELAY_MS, per spec.
        self.tonnel_client = tonnel_client or TonnelClient(request_delay_ms=config.TONNEL_REQUEST_DELAY_MS)
        # Правка 1 (two-way cross-check delivery): the Tonnel->Portals
        # direction needs a Portals client too, to query Portals' own
        # pair floor for a Tonnel signal's (collection, model, backdrop).
        # None by default -- build_default_poller() only constructs a
        # real one when config.CROSS_CHECK_ENABLED (same discipline as
        # poller.py's tonnel_client: always safe to construct, gated at
        # call time in _maybe_cross_check via cross_check.cross_check()).
        self.portals_client = portals_client
        # Правка 2 (MRKT third-neighbour delivery): None unless
        # MRKT_ACCESS_TOKEN is set (see mrkt_client.build_default_mrkt_client)
        # -- cross_check() already treats mrkt_client=None as "skip this
        # neighbour", never crashing.
        self.mrkt_client = mrkt_client
        # None unless TONNEL_NOTIFY_ENABLED (see build_default_poller) --
        # collection/lifecycle tracking must work with no Telegram
        # credentials at all, same discipline as poller.py's NOTIFY_ENABLED.
        self.notifier = notifier
        self._sleep = sleep_fn
        self._start_mono: float | None = None
        self._iterations = 0
        self._last_lifecycle_check_mono: float | None = None
        self._last_notify_since: datetime = datetime.now(timezone.utc) - timedelta(hours=1)
        self.stats = {
            "pages_fetched": 0,
            "items_seen_total": 0,
            "items_already_known": 0,
            "items_not_purchasable": 0,
            "new_listings": 0,
            "collect_filtered_count": 0,
            "parse_errors": 0,
            "price_changes_seen": 0,
            "price_drops": 0,
            "price_raises": 0,
            "price_drops_above_threshold": 0,
            # ДОПОЛНЕНИЕ (floor-freshness delivery): every exit branch of
            # _maybe_refresh_floor_snapshot(), counted separately --
            # measured live, 41% of Tonnel's clean signals were on a
            # stale floor_snapshots fallback (source="snapshot") instead
            # of a fresh "at_drop" one, and the reason was a mystery
            # because these branches were indistinguishable. Their sum
            # equals price_drops_above_threshold (one refresh attempt per
            # significant drop).
            "floor_refresh_ok": 0,
            "floor_refresh_missing_name": 0,
            "floor_refresh_error": 0,
            "floor_refresh_thin_book": 0,
            "floor_refresh_no_data": 0,
            "lifecycle_newly_gone": 0,
            "lifecycle_not_returned": 0,
            # ДОПОЛНЕНИЕ, Правка 2: a negative gift_id is a BUNDLE
            # (Tonnel's own documented convention) -- its price is for
            # the whole set, not comparable to a single lot's price, so
            # it must never enter `listings` at all.
            "items_bundles_skipped": 0,
            "tonnel_403_count": 0,
            "tonnel_429_count": 0,
            # ДОПОЛНЕНИЕ, Правка 3: separate from 403/429 -- a platform-
            # side outage (confirmed live: one 502 with a Cloudflare HTML
            # page), not evidence of our own client misbehaving.
            "tonnel_5xx_count": 0,
            # ДОПОЛНЕНИЕ, Правка 3: per-BATCH fallback, not a permanent
            # mode switch -- see _run_lifecycle_check_batch. Counts how
            # many batches had to fall back to per-item queries after one
            # retry of the batch call still failed.
            "lifecycle_batch_fallback_count": 0,
            # Tonnel signals delivery, Правка 4 -- same meaning as
            # poller.py's identically-named stats.
            "signals_sent": 0,
            "signals_send_failed": 0,
            "signals_stale": 0,
            "signals_suppressed_cooldown": 0,
            # Правка 5 (unified-notification delivery) -- same meaning
            # and same names as poller.py's identically-named stats, for
            # the Tonnel->Portals direction. Names match cross_check.py's
            # VERDICT_* values exactly.
            "sent_no_neighbour": 0,
            "sent_neighbour_higher": 0,
            "skipped_neighbour_cheaper": 0,
            "neighbour_thin": 0,
            "error": 0,
            # Правка 4 (two-writer SQLite contention, see poller.py's
            # matching counter): must not be swallowed silently -- counted
            # and logged, the current cycle is skipped, not a crash.
            "db_locked_count": 0,
        }

    # --- FAST PATH: feed collection -----------------------------------

    def _search_page(self, page: int) -> list[dict] | None:
        """Returns the page's items, or None if the page could not be
        fetched at all -- distinct from a genuinely empty page (real end
        of the feed). Classifies 403/429 into their own stats counters
        (see TonnelError.status_code) -- per spec, Tonnel may start
        resisting load even though no rate limit was observed at measure
        time.
        """
        min_price = config.TONNEL_COLLECT_MIN_PRICE if config.TONNEL_COLLECT_MIN_PRICE_NANO > 0 else None
        try:
            return self.tonnel_client.search(
                sort={"message_post_time": -1}, limit=30, page=page, min_price=min_price
            )
        except TonnelError as exc:
            if exc.status_code == 403:
                self.stats["tonnel_403_count"] += 1
            elif exc.status_code == 429:
                self.stats["tonnel_429_count"] += 1
            elif exc.status_code is not None and 500 <= exc.status_code < 600:
                self.stats["tonnel_5xx_count"] += 1
            logger.error("Tonnel page fetch failed at page=%d: %s", page, exc)
            return None

    def poll_once(self) -> list[Listing]:
        """Pages 1..TONNEL_MAX_PAGES_PER_ITERATION, sorted by freshness
        (message_post_time desc). Every page is processed in full, same
        discipline as poller.py's poll_once -- new and known items can be
        interleaved. Pagination stops early when a page is empty or
        entirely known (caught up to the previous iteration).
        """
        result: list[Listing] = []

        for page in range(1, config.TONNEL_MAX_PAGES_PER_ITERATION + 1):
            items = self._search_page(page)
            if items is None:
                break

            self.stats["pages_fetched"] += 1
            if not items:
                break

            self.stats["items_seen_total"] += len(items)

            new_items = []
            known_items = []
            for item in items:
                gift_id = item.get("gift_id")
                if gift_id is None:
                    logger.warning("dropping Tonnel item with no gift_id: %s", item)
                    continue
                if gift_id < 0:
                    # Правка 2: a negative gift_id is a BUNDLE (Tonnel's
                    # own documented convention) -- its price is for the
                    # whole set, never comparable to one gift's price.
                    # Dropped before dedup/known-item checks: a bundle
                    # must never enter `listings` regardless of whether
                    # this is its first or a later sighting.
                    self.stats["items_bundles_skipped"] += 1
                    continue
                if db.listing_exists(self.conn, MARKETPLACE, str(gift_id)):
                    self.stats["items_already_known"] += 1
                    known_items.append(item)
                else:
                    new_items.append(item)

            result.extend(self._process_new_items(new_items))
            self._process_known_items(known_items)

            if not new_items:
                break  # caught up to the previous iteration

        self.stats["new_listings"] += len(result)
        return result

    def _process_new_items(self, items: list[dict]) -> list[Listing]:
        """Правка 2: drops underLoan/premarketData/auction/
        dutchAuctionData items BEFORE they ever reach the DB (these are
        other trading mechanics, not an ordinary buy).

        ДОПОЛНЕНИЕ: TONNEL_COLLECT_MIN_PRICE is now sent to Tonnel itself
        (see _search_page's min_price) -- confirmed live it actually
        filters server-side (measured: 49% of a 10.4h run's traffic,
        218477/445260 items, was fetched only to be discarded here by
        this exact check). The check below stays as a DEFENSIVE
        backstop, not the primary filter anymore -- if collect_filtered_count
        stays high after this change, that's the signal the server-side
        filter stopped being applied (a regression to notice, not ignore).
        """
        written: list[Listing] = []
        now = datetime.now(timezone.utc)
        for item in items:
            if not is_purchasable_tonnel_item(item):
                self.stats["items_not_purchasable"] += 1
                continue

            try:
                listing = parse_tonnel_listing(item)
            except UnparseableTonnelListing as exc:
                self.stats["parse_errors"] += 1
                logger.error("unparseable Tonnel listing, raw=%s error=%s", item, exc)
                continue

            if (
                config.TONNEL_COLLECT_MIN_PRICE_NANO > 0
                and listing.price_nano is not None
                and listing.price_nano < config.TONNEL_COLLECT_MIN_PRICE_NANO
            ):
                self.stats["collect_filtered_count"] += 1
                continue

            db.insert_listing(self.conn, listing)
            db.touch_listing_lifecycle(
                self.conn, MARKETPLACE, listing.external_id, listing.collection_id,
                listing.model_name, listing.backdrop_name, listing.price_nano, now,
            )
            written.append(listing)

        return written

    def _process_known_items(self, items: list[dict]) -> None:
        """Same discipline as poller.py's _process_known_items: lifecycle
        last_seen_at updates for every known item regardless of price,
        price_history only gets a row for a REAL price change.
        """
        now = datetime.now(timezone.utc)
        for item in items:
            try:
                listing = parse_tonnel_listing(item)
            except UnparseableTonnelListing as exc:
                self.stats["parse_errors"] += 1
                logger.error("unparseable known Tonnel listing, raw=%s error=%s", item, exc)
                continue

            db.touch_listing_lifecycle(
                self.conn, MARKETPLACE, listing.external_id, listing.collection_id,
                listing.model_name, listing.backdrop_name, listing.price_nano, now,
            )
            self._maybe_record_price_change(listing, now)

    def _maybe_record_price_change(self, listing: Listing, now: datetime) -> None:
        """Shared by _process_known_items (feed) and
        _run_lifecycle_check_batch (КАК ТЕСТИРОВАТЬ item 5: a price seen
        during a lifecycle check is just as real a change as one seen in
        the feed) -- compares against the stored price and writes
        price_history only for an ACTUAL change, same discipline as
        poller.py.

        Правка 1/2 (Tonnel signals delivery): is_noise is now actually
        classified (TONNEL_PRICE_DROP_MIN_PCT) instead of always False --
        signals.py's cascade needs a real is_noise flag to work at all.

        ДОПОЛНЕНИЕ (stale-floor fix): on a significant drop,
        _maybe_refresh_floor_snapshot() now ALSO returns what it fetched
        (a single network call, reused for both writes -- никогда
        двух запросов за один дроп) so floor_at_drop_nano/
        floor_listed_count_at_drop/floor_fetched_at/floor_level_at_drop
        can be written into THIS price_history row directly, exactly the
        same "at drop" discipline poller.py uses for Portals (see
        Poller._process_known_items). Confirmed live this was missing
        entirely: 195 significant Tonnel drops, floor_at_drop_nano filled
        on 0 of them -- every Tonnel signal was comparing against a
        floor_snapshots row that could be hours stale (measured up to
        12.4h) by the time signals.py read it as a "snapshot"-source
        floor, while ~40 price changes happen on Tonnel per half-hour
        run. floor_snapshots itself is STILL written too (unchanged --
        report.py/signals.py's "snapshot" fallback source still needs a
        current value for listings that DIDN'T just drop).
        """
        new_price_nano = listing.price_nano
        if new_price_nano is None:
            return

        existing = db.get_listing_price_and_listed_at(self.conn, MARKETPLACE, listing.external_id)
        if existing is None:
            return  # race: not actually in the DB despite the caller having just seen it known
        old_price_nano, _old_listed_at = existing
        if old_price_nano is None or old_price_nano == new_price_nano:
            return

        self.stats["price_changes_seen"] += 1
        delta_pct = (Decimal(new_price_nano - old_price_nano) / Decimal(old_price_nano)) * 100
        is_noise = False
        floor_at_drop_nano = None
        floor_listed_count_at_drop = 0
        floor_fetched_at = None
        floor_level_at_drop = None
        if new_price_nano < old_price_nano:
            self.stats["price_drops"] += 1
            is_noise = abs(delta_pct) < config.TONNEL_PRICE_DROP_MIN_PCT
            if not is_noise:
                self.stats["price_drops_above_threshold"] += 1
                refreshed = self._maybe_refresh_floor_snapshot(listing, now)
                if refreshed is not None:
                    floor_at_drop_nano, floor_listed_count_at_drop = refreshed
                    floor_fetched_at = now
                    floor_level_at_drop = "model"
        else:
            self.stats["price_raises"] += 1

        db.record_price_change(
            self.conn, MARKETPLACE, listing.external_id,
            old_price_nano=old_price_nano,
            new_price_nano=new_price_nano,
            delta_pct=delta_pct,
            is_noise=is_noise,
            old_listed_at=None,
            new_listed_at=None,
            observed_at=now,
            floor_at_drop_nano=floor_at_drop_nano,
            floor_listed_count_at_drop=floor_listed_count_at_drop,
            floor_fetched_at=floor_fetched_at,
            floor_level_at_drop=floor_level_at_drop,
        )

    def _maybe_refresh_floor_snapshot(self, listing: Listing, now: datetime) -> tuple[int, int] | None:
        """Правка 1 (Tonnel model-floor delivery): on a significant drop,
        re-query Tonnel's own MODEL floor (gift_name + model, NO
        backdrop, self-excluded by gift_num) and write it into
        floor_snapshots (marketplace='tonnel') -- the SAME table/columns
        signals.py's cascade already reads for the "snapshot" floor
        source, so no cascade changes were needed to make this work.

        ДОПОЛНЕНИЕ (stale-floor fix): ALSO returns (floor_nano,
        listed_count) when the query found a real, usable ("ok") floor,
        or None otherwise (no usable book, or the query itself failed) --
        the caller (_maybe_record_price_change) uses this to ALSO write
        floor_at_drop_nano into the SAME price_history row this drop
        creates, the one network call serving both writes. Mirrors
        poller.py's Portals mechanism exactly (Poller._process_known_items
        re-queries and writes floor_at_drop_nano at the moment of a
        significant drop) -- reused here, not reimplemented from scratch.

        The Tonnel PAIR floor is NO LONGER QUERIED AT ALL here --
        confirmed live, 20/20 significant Tonnel drops spot-checked had
        no pair floor whatsoever (the feed is too thin for a second
        listing of the exact same collection+model+backdrop to coexist)
        -- querying it would be a wasted request, always "no_data". See
        README: this SUPERSEDES the earlier pair-floor version, which
        wrongly assumed the model level was unusable for Tonnel by
        analogy with Portals (where it IS unusable, but for a different,
        Portals-specific reason -- see config.py's
        TONNEL_MODEL_MIN_LISTED_COUNT docstring).

        TONNEL_MODEL_MIN_LISTED_COUNT is applied HERE, not in
        tonnel_client.model_floor() (which has no threshold opinion of
        its own) -- a real floor with too few listings backing it is
        recorded as model_floor_status="thin_model_book", NOT "ok", so
        signals.py's existing `model_floor_status == "ok"` gate already
        excludes it -- no separate cascade stage needed.
        """
        # ДОПОЛНЕНИЕ (floor-freshness delivery): every exit branch below
        # is counted AND logged with its own distinct reason -- per
        # spec, the earlier version returned None from THREE different
        # branches (missing name, network error, thin/no-data book) with
        # no way to tell them apart, which is exactly why the measured
        # 41% snapshot-fallback rate on Tonnel was a mystery instead of
        # an explainable number. See print_summary for the counters.
        if not (listing.collection_name and listing.model_name):
            self.stats["floor_refresh_missing_name"] += 1
            logger.warning(
                "Tonnel at-drop floor refresh skipped for %s: missing collection_name/model_name "
                "(collection_name=%r, model_name=%r)",
                listing.external_id, listing.collection_name, listing.model_name,
            )
            return None
        try:
            floor = self.tonnel_client.model_floor(
                gift_name=listing.collection_name,
                model=listing.model_name,
                exclude_gift_num=listing.gift_number,
            )
        except TonnelError as exc:
            self.stats["floor_refresh_error"] += 1
            logger.warning("Tonnel at-drop model floor query failed for %s: %s", listing.external_id, exc)
            return None

        if floor.status == "ok" and floor.listed_count < config.TONNEL_MODEL_MIN_LISTED_COUNT:
            status = "thin_model_book"
        else:
            status = floor.status

        db.upsert_tonnel_model_floor_snapshot(
            self.conn, listing.external_id, listing.model_name, listing.backdrop_name,
            floor.floor_nano, floor.listed_count, status, now,
        )

        # ДОПОЛНЕНИЕ: only a real, USABLE floor ("ok" -- already past the
        # TONNEL_MODEL_MIN_LISTED_COUNT gate above) is worth writing into
        # price_history's floor_at_drop_nano -- a thin/no_data book means
        # there's nothing trustworthy to record "at the moment of this
        # drop" either, same as the snapshot itself staying non-"ok".
        if status == "thin_model_book":
            self.stats["floor_refresh_thin_book"] += 1
            logger.info(
                "Tonnel at-drop floor for %s: thin book (listed_count=%d < TONNEL_MODEL_MIN_LISTED_COUNT=%d)",
                listing.external_id, floor.listed_count, config.TONNEL_MODEL_MIN_LISTED_COUNT,
            )
            return None
        if status != "ok":
            # "no_data" -- the query itself succeeded but found NO usable
            # listings at all for this (collection, model) -- a genuinely
            # empty book, not a thin one. Distinct reason, distinct
            # counter -- this is the branch that was previously
            # indistinguishable from "thin" and from "error" alike.
            self.stats["floor_refresh_no_data"] += 1
            logger.info("Tonnel at-drop floor for %s: no usable listings at all (status=%s)", listing.external_id, status)
            return None
        self.stats["floor_refresh_ok"] += 1
        return floor.floor_nano, floor.listed_count

    # --- lifecycle (disappearance) check pass --------------------------

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
        """ДОПОЛНЕНИЕ (lifecycle fix): batches up to
        TONNEL_LIFECYCLE_BATCH_SIZE not-yet-disappeared listings and
        checks whether they're still for sale via
        {"gift_id": {"$in": [...]}, "asset": "TON"} -- a MINIMAL filter,
        NOT the standard BASE_FILTER (see
        TonnelClient.search_minimal_by_gift_ids's docstring for the
        confirmed spot-check: using BASE_FILTER here produced false
        disappearances for 8/12 genuinely-still-listed lots).
        external_id IS gift_id for Tonnel, no extra lookup needed.

        On a batch failure: one retry of the SAME batch call, then a
        ONE-TIME fallback to per-gift_id queries for THIS batch only
        (each just search_minimal_by_gift_ids([gift_id], limit=1) --
        same minimal filter, one item at a time). The next batch always
        tries the batched call again -- a permanent mode switch on one
        network error would throw away the ~30x request savings for the
        rest of the run over a single blip.

        A gift_id genuinely ABSENT from the response IS now treated as
        disappearance (final_status="gone_unknown") -- confirmed live
        that absence even without BASE_FILTER means the platform truly
        has nothing under that gift_id (spot-check: 3/12 absent both
        with and without BASE_FILTER). Unlike Portals, the REASON
        (sold vs. delisted) cannot be determined -- see README. A gift_id
        whose per-item fallback query itself FAILS (network/API error,
        not a confirmed absence) is counted in lifecycle_not_returned
        instead -- an unverified state, not evidence either way.

        A found item's price is compared against what's stored, exactly
        like a feed sighting -- see _maybe_record_price_change (КАК
        ТЕСТИРОВАТЬ item 5).
        """
        batch_size = config.TONNEL_LIFECYCLE_BATCH_SIZE
        batch = db.get_lifecycle_check_batch(self.conn, MARKETPLACE, batch_size)
        if not batch:
            return

        ext_id_by_gift_id: dict[int, str] = {}
        for row in batch:
            ext_id = row["listing_external_id"]
            try:
                ext_id_by_gift_id[int(ext_id)] = ext_id
            except ValueError:
                continue
        if not ext_id_by_gift_id:
            return
        gift_ids = list(ext_id_by_gift_id.keys())

        now = datetime.now(timezone.utc)
        found_items_by_ext_id: dict[str, dict] = {}
        uncertain_ext_ids: set[str] = set()

        results = None
        last_exc: TonnelError | None = None
        for attempt in range(2):  # one retry of the SAME batch call, not a permanent fallback
            try:
                results = self.tonnel_client.search_minimal_by_gift_ids(gift_ids, limit=batch_size)
                break
            except TonnelError as exc:
                last_exc = exc
                logger.warning(
                    "Tonnel gift_id $in minimal-filter batch check failed (attempt=%d/2): %s", attempt + 1, exc
                )

        if results is not None:
            for item in results:
                gift_id = item.get("gift_id")
                if gift_id in ext_id_by_gift_id:
                    found_items_by_ext_id[ext_id_by_gift_id[gift_id]] = item
        else:
            logger.warning(
                "Tonnel batch lifecycle check failed twice (%s) -- falling back to per-item "
                "queries for THIS batch of %d only; the next batch tries the batched call again",
                last_exc, len(gift_ids),
            )
            self.stats["lifecycle_batch_fallback_count"] += 1
            for gift_id, ext_id in ext_id_by_gift_id.items():
                try:
                    item_results = self.tonnel_client.search_minimal_by_gift_ids([gift_id], limit=1)
                except TonnelError as exc:
                    logger.warning("Tonnel per-item lifecycle check failed for gift_id=%s: %s", gift_id, exc)
                    uncertain_ext_ids.add(ext_id)
                    continue
                if item_results:
                    found_items_by_ext_id[ext_id] = item_results[0]

        if uncertain_ext_ids:
            self.stats["lifecycle_not_returned"] += len(uncertain_ext_ids)
            for ext_id in uncertain_ext_ids:
                db.record_lifecycle_check_missing(self.conn, MARKETPLACE, ext_id, now)

        newly_gone = 0
        for ext_id in ext_id_by_gift_id.values():
            if ext_id in uncertain_ext_ids:
                continue  # already recorded above -- not confirmed either way
            if ext_id in found_items_by_ext_id:
                db.record_lifecycle_status(self.conn, MARKETPLACE, ext_id, "forsale", now)
                item = found_items_by_ext_id[ext_id]
                try:
                    listing = parse_tonnel_listing(item)
                except UnparseableTonnelListing as exc:
                    self.stats["parse_errors"] += 1
                    logger.error("unparseable Tonnel listing during lifecycle check, raw=%s error=%s", item, exc)
                    continue
                self._maybe_record_price_change(listing, now)
            else:
                db.record_lifecycle_status(self.conn, MARKETPLACE, ext_id, "gone_unknown", now)
                newly_gone += 1

        self.stats["lifecycle_newly_gone"] += newly_gone

    # --- Telegram notification (Правка 4, opt-in via TONNEL_NOTIFY_ENABLED) --

    def _check_cooldown(self, signal) -> bool:
        """TONNEL_SIGNAL_COOLDOWN_MIN -- separate setting from Portals'
        SIGNAL_COOLDOWN_MIN (Правка 1), same mechanism: suppress a repeat
        notification for the same listing within the window. Unlike
        poller.py's _check_cooldown, no resend-after-drop exemption --
        not asked for here, kept simple.
        """
        last = db.get_last_sent_alert_for_listing(self.conn, MARKETPLACE, signal.listing_external_id)
        if last is None:
            return True
        elapsed_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last["sent_at"])).total_seconds() / 60
        return elapsed_min >= config.TONNEL_SIGNAL_COOLDOWN_MIN

    def _check_signal_still_fresh(self, signal) -> bool | None:
        """Правка 4/КАК ТЕСТИРОВАТЬ item 5: one targeted gift_id query,
        the SAME minimal-filter mechanism lifecycle checking already
        uses (search_minimal_by_gift_ids), immediately before sending --
        catches a lot sold/delisted between detection and send. Returns
        True (still there, same price), False (confirmed stale), or None
        (the check itself failed -- retried later, never marked stale on
        an unverified guess, same contract as poller.py's version).
        """
        try:
            gift_id = int(signal.listing_external_id)
        except ValueError:
            return None
        try:
            results = self.tonnel_client.search_minimal_by_gift_ids([gift_id], limit=1)
        except TonnelError as exc:
            logger.error(
                "freshness check failed for %s: %s -- will retry, not marking stale",
                signal.listing_external_id, exc,
            )
            return None
        return lot_check.tonnel_available(results, signal.new_price_nano)

    def _maybe_cross_check(self, signal) -> None:
        """Правка 3/5 (unified-notification delivery): a PURE PRE-SEND
        FILTER, the Tonnel->Portals direction -- mirrors poller.py's
        Portals->Tonnel _maybe_cross_check, both call the SAME shared
        cross_check.cross_check() (see that module), which dispatches by
        signal.marketplace. Never raises -- a Portals-side failure
        degrades to verdict "error" (SEND, never blocks), logged.
        """
        if not config.CROSS_CHECK_ENABLED:
            return
        if not (signal.collection_name and signal.model_name and signal.backdrop_name):
            return

        now = datetime.now(timezone.utc)
        cross_check(self.conn, signal, portals_client=self.portals_client, mrkt_client=self.mrkt_client, now=now)

        if signal.cross_verdict in self.stats:
            self.stats[signal.cross_verdict] += 1

    def _maybe_notify(self) -> None:
        if self.notifier is None and self.journal_conn is None:
            return

        since = self._last_notify_since
        now = datetime.now(timezone.utc)
        all_signals = clean_signals(self.conn, since=since, usd_rate=Decimal(1), now=now, marketplace=MARKETPLACE)
        self._last_notify_since = now

        try:
            if self.notifier is not None:
                self._send_signals(all_signals)
        finally:
            paper_journal.record_signals_safely(self.journal_conn, all_signals)

    def _send_signals(self, all_signals) -> None:
        # ДЕФЕКТ 2 (systemic-check delivery): Portals' NOTIFY_LEVELS
        # (default {"pair"}) is not applied to Tonnel -- Tonnel has no
        # pair floor at all (see _maybe_refresh_floor_snapshot), every
        # Tonnel signal IS level="model" by construction, and applying
        # the Portals-oriented gate here would either silently drop every
        # Tonnel signal (default config) or require adding "model" to
        # NOTIFY_LEVELS, which would ALSO wrongly re-enable Portals'
        # rejected model-level signals (same shared config value). Uses
        # its OWN TONNEL_NOTIFY_LEVELS (default {"model"}) instead --
        # explicit and enforced, not just a comment saying it's fine to
        # skip the check.
        candidates = [
            s for s in all_signals
            if s.floor_level in config.TONNEL_NOTIFY_LEVELS
            and passes_notify_threshold(s)
            and not db.is_alert_sent(self.conn, MARKETPLACE, s.listing_external_id, s.observed_at)
        ]
        if not candidates:
            return

        to_send, extra = select_signals_to_send(candidates, config.NOTIFY_MAX_PER_MINUTE)
        for signal in to_send:
            if not self._check_cooldown(signal):
                self.stats["signals_suppressed_cooldown"] += 1
                logger.info(
                    "Tonnel signal suppressed by cooldown: listing_external_id=%s observed_at=%s",
                    signal.listing_external_id, signal.observed_at,
                )
                continue

            fresh = self._check_signal_still_fresh(signal)
            if fresh is None:
                self.stats["signals_send_failed"] += 1
                continue
            if fresh is False:
                db.mark_alert_sent(
                    self.conn, MARKETPLACE, signal.listing_external_id, signal.observed_at,
                    datetime.now(timezone.utc), status="skipped_stale",
                )
                self.stats["signals_stale"] += 1
                logger.info(
                    "Tonnel signal stale, not sent: listing_external_id=%s observed_at=%s",
                    signal.listing_external_id, signal.observed_at,
                )
                continue

            self._maybe_cross_check(signal)

            if signal.cross_verdict in BLOCKING_VERDICTS:
                # Правка 3: the Portals neighbour has the same pair for
                # the same price or only slightly more -- nothing to
                # gain. Never sent, recorded distinctly, same discipline
                # as poller.py's mirror-image case.
                db.mark_alert_sent(
                    self.conn, MARKETPLACE, signal.listing_external_id, signal.observed_at,
                    datetime.now(timezone.utc), status="skipped_cross_worse",
                )
                logger.info(
                    "Tonnel signal skipped, cross_verdict=%s: listing_external_id=%s observed_at=%s "
                    "price=%s neighbour_floor=%s",
                    signal.cross_verdict, signal.listing_external_id, signal.observed_at,
                    signal.new_price_nano, signal.neighbour_floor_nano,
                )
                continue

            ok = self.notifier.send_signal(signal)
            if ok:
                db.mark_alert_sent(self.conn, MARKETPLACE, signal.listing_external_id, signal.observed_at, datetime.now(timezone.utc))
                self.stats["signals_sent"] += 1
            else:
                self.stats["signals_send_failed"] += 1
                logger.error(
                    "Tonnel signal not sent, will retry: listing_external_id=%s observed_at=%s",
                    signal.listing_external_id, signal.observed_at,
                )
        if extra:
            self.notifier.send_text(f"...и ещё {extra} сигналов Tonnel, см. отчёт (--marketplace tonnel)")

    # --- run loop + summary ---------------------------------------------

    def run_forever(self, run_seconds: float | None = None) -> None:
        self._start_mono = time.monotonic()
        try:
            while True:
                if run_seconds is not None and (time.monotonic() - self._start_mono) >= run_seconds:
                    break
                start = time.monotonic()
                try:
                    self.poll_once()
                    self._iterations += 1
                    if self._journal_uptime is not None:
                        self._journal_uptime.heartbeat()
                    self.maybe_run_lifecycle_check()
                    self._maybe_notify()
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
                self._sleep(max(0.0, config.TONNEL_POLL_INTERVAL_SEC - elapsed))
        except KeyboardInterrupt:
            pass
        finally:
            self.print_summary()

    def print_summary(self) -> None:
        duration = (time.monotonic() - self._start_mono) if self._start_mono is not None else 0.0
        print()
        print("=== Tonnel collector run summary ===")
        print(f"duration_sec: {duration:.1f}")
        print(f"iterations: {self._iterations}")
        for key in (
            "pages_fetched", "items_seen_total", "items_already_known", "items_not_purchasable",
            "items_bundles_skipped",
            "new_listings", "collect_filtered_count", "parse_errors",
            "price_changes_seen", "price_drops", "price_raises", "price_drops_above_threshold",
            "floor_refresh_ok", "floor_refresh_missing_name", "floor_refresh_error",
            "floor_refresh_thin_book", "floor_refresh_no_data",
            "lifecycle_newly_gone", "lifecycle_not_returned", "lifecycle_batch_fallback_count",
            "tonnel_403_count", "tonnel_429_count", "tonnel_5xx_count",
            "signals_sent", "signals_send_failed", "signals_stale", "signals_suppressed_cooldown",
            "sent_no_neighbour", "sent_neighbour_higher", "skipped_neighbour_cheaper", "neighbour_thin", "error",
            "db_locked_count",
        ):
            print(f"{key}: {self.stats[key]}")


def build_default_poller(dsn: str = config.DB_DSN) -> TonnelPoller:
    conn = db.connect(dsn)
    db.verify_schema(conn)

    notifier = None
    if config.TONNEL_NOTIFY_ENABLED:
        # Settings SHARED with Portals, per spec: same bot token, same
        # owner/viewers. Only whether Tonnel signals are SENT is gated
        # separately (TONNEL_NOTIFY_ENABLED), not who receives them.
        bot_token = config.get_telegram_bot_token()
        owner_id = config.get_telegram_owner_id()
        viewer_ids = config.get_telegram_viewer_ids()
        notifier = TelegramNotifier(bot_token, chat_id=owner_id, viewer_chat_ids=viewer_ids)

    portals_client = None
    if config.CROSS_CHECK_ENABLED:
        # Правка 1 (two-way cross-check delivery): the Tonnel->Portals
        # direction needs a real, authenticated Portals client -- SAME
        # credentials poller.py's own process uses (AuthManager() reads
        # them from the shared config/env, no Tonnel-specific auth
        # concept exists on the Portals side). Constructed unconditionally
        # whenever cross-check is on, even though this process's PRIMARY
        # job is collecting Tonnel's own feed -- no network call happens
        # at construction time, same discipline as poller.py's
        # TonnelClient.
        from .auth import AuthManager
        from .portals_client import PortalsClient
        portals_auth = AuthManager()
        portals_client = PortalsClient(auth_provider=portals_auth.get)

    mrkt_client = build_default_mrkt_client()
    journal_conn = journal_db.connect(journal_config.JOURNAL_DB_DSN) if journal_config.PAPER_JOURNAL_ENABLED else None

    return TonnelPoller(
        conn, notifier=notifier, portals_client=portals_client, mrkt_client=mrkt_client, journal_conn=journal_conn,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-seconds", type=float, default=None,
        help="Stop automatically after this many seconds and print the run summary.",
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
