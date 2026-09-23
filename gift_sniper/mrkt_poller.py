"""Full MRKT collector AND signaller -- event feed (listing/change_price/
sale), price tracking, lifecycle (disappearance/sale) tracking, and
clean signals + Telegram notifications. NOT a subclass of poller.Poller
or tonnel_poller.TonnelPoller (per spec, no inheritance between
pollers) -- a separate process against an unrelated API, but reusing
the SAME shared db.py/models.py/money.py/signals.py/notifier.py
functions Portals/Tonnel use (marketplace='mrkt' scopes every row) --
never a duplicated copy of them.

Collection uses /api/v1/feed (mrkt_client.MrktClient.feed()) -- a
confirmed real, strictly chronological event stream -- NEVER
/gifts/saling (the "showcase" endpoint pair_floor()/find_by_number()
use for cross-check, whose order was confirmed RANDOM live and is
useless for collection, see mrkt_client.py's module docstring).

Runs NO CommandHandler of its own, same reasoning as tonnel_poller.py:
getUpdates' offset is a single global stream per bot token, shared by
independent processes would be a real conflict. This process only
SENDS (send_signal), gated by MRKT_NOTIFY_ENABLED.

Run: python -m gift_sniper.mrkt_poller
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
from .mrkt_client import MrktClient, MrktError
from .mrkt_parsing import UnparseableMrktListing, is_purchasable_mrkt_gift, parse_mrkt_listing
from .notifier import TelegramNotifier, passes_notify_threshold, select_signals_to_send
from .signals import clean_signals
from .tonnel_client import TonnelClient

logger = logging.getLogger("gift_sniper.mrkt_poller")

MARKETPLACE = "mrkt"


class MrktPoller:
    def __init__(
        self,
        conn,
        mrkt_client: MrktClient | None = None,
        notifier: TelegramNotifier | None = None,
        sleep_fn=time.sleep,
        portals_client=None,
        tonnel_client=None,
        journal_conn=None,
    ):
        self.conn = conn
        # Paper journal (journal.db) -- None means off.
        self.journal_conn = journal_conn
        self._journal_uptime = paper_journal.UptimeTracker(journal_conn, MARKETPLACE) if journal_conn is not None else None
        # Unlike Tonnel/Portals clients, MrktClient has no zero-arg
        # default (it REQUIRES a token_provider, see mrkt_client.py) --
        # build_default_poller() is what actually constructs a real one,
        # requiring MRKT_ACCESS_TOKEN (this poller's ENTIRE job is
        # querying MRKT, unlike the optional cross-check use of MRKT
        # elsewhere -- there is no meaningful "run without it" mode).
        self.mrkt_client = mrkt_client
        # Правка 5 (cross-check direction): an MRKT signal's neighbours
        # are Portals and Tonnel -- both optional, same "construct
        # unconditionally, gate at call time" discipline as poller.py/
        # tonnel_poller.py use for their own cross-check clients.
        self.portals_client = portals_client
        self.tonnel_client = tonnel_client or TonnelClient(request_delay_ms=config.TONNEL_REQUEST_DELAY_MS)
        # None unless MRKT_NOTIFY_ENABLED (see build_default_poller).
        self.notifier = notifier
        self._sleep = sleep_fn
        self._start_mono: float | None = None
        self._iterations = 0
        self._last_notify_since: datetime = datetime.now(timezone.utc) - timedelta(hours=1)
        self.stats = {
            "pages_fetched": 0,
            "items_seen_total": 0,
            "items_already_known": 0,
            "new_listings": 0,
            "collect_filtered_count": 0,
            "parse_errors": 0,
            "unknown_event_type_count": 0,
            "price_changes_seen": 0,
            "price_drops": 0,
            "price_raises": 0,
            "price_drops_above_threshold": 0,
            "sales_recorded": 0,
            # ПРАВКА 1 (sale-vs-floor delivery): how many of those sales
            # ALSO got a usable pair floor recorded alongside them (see
            # _handle_sale_event) -- expected well under sales_recorded,
            # since most sales are below MRKT_COLLECT_MIN_PRICE (never
            # queried at all, ПРАВКА 2) and some queries hit a thin book/
            # error/no_data even for the ones that are queried.
            "sale_floor_recorded": 0,
            "unlistings_recorded": 0,
            "returns_recorded": 0,
            # ДОПОЛНЕНИЕ (missing-event-types fix): lucky_buy/plinko_win/
            # crafting are MRKT's own game mechanics, unrelated to
            # ordinary trading -- explicitly skipped, counted SEPARATELY
            # from unknown_event_type_count (per spec: "не считать
            # неизвестными"). crafting in particular has isOnSale=true
            # but is a craft RESULT, not a listing -- never written to
            # `listings`.
            "events_game_skipped": 0,
            # Premarket lots: known and deliberately skipped, see
            # PREMARKET_EVENT_TYPES.
            "events_premarket_skipped": 0,
            "signals_sent": 0,
            "signals_send_failed": 0,
            "signals_stale": 0,
            "signals_suppressed_cooldown": 0,
            # Правка 5 -- same meaning and same names as poller.py's/
            # tonnel_poller.py's identically-named stats, for the
            # MRKT->{Portals,Tonnel} direction. Names match
            # cross_check.py's VERDICT_* values exactly.
            "sent_no_neighbour": 0,
            "sent_neighbour_higher": 0,
            "skipped_neighbour_cheaper": 0,
            "neighbour_thin": 0,
            "error": 0,
            # ДОПОЛНЕНИЕ (mrkt_error_count breakdown): mirrors Tonnel's
            # 403/429/5xx split (tonnel_poller.py) -- previously every
            # MrktError landed in one undifferentiated bucket, 12 of them
            # in one 20-minute run with no way to tell what they actually
            # were. mrkt_error_count now holds only the LEFTOVER cases
            # (network failure, non-JSON body, unexpected shape --
            # status_code is None for all of these, see mrkt_client.py).
            "mrkt_403_count": 0,
            "mrkt_429_count": 0,
            "mrkt_5xx_count": 0,
            "mrkt_error_count": 0,
            # Two-writer SQLite contention (see poller.py/tonnel_poller.py's
            # matching counter): must not be swallowed silently.
            "db_locked_count": 0,
        }

    def _classify_mrkt_error(self, exc: MrktError, context: str) -> None:
        """ДОПОЛНЕНИЕ (mrkt_error_count breakdown): classifies an MrktError
        by status_code, same discipline as tonnel_poller.py's 403/429/5xx
        split -- always logs the exception text and the call site
        (`context`) so a real recurring failure is diagnosable, not just
        a rising number with no shape.
        """
        if exc.status_code == 403:
            self.stats["mrkt_403_count"] += 1
        elif exc.status_code == 429:
            self.stats["mrkt_429_count"] += 1
        elif exc.status_code is not None and 500 <= exc.status_code < 600:
            self.stats["mrkt_5xx_count"] += 1
        else:
            self.stats["mrkt_error_count"] += 1
        logger.error("MRKT error (%s): %s", context, exc)

    # --- FAST PATH: feed collection --------------------------------------

    def poll_once(self) -> int:
        """Pages the event feed, newest-first, up to
        MRKT_MAX_PAGES_PER_ITERATION pages, stopping EARLY the moment a
        known (already-processed) event id is hit -- per spec, the feed
        is strictly chronological with zero overlap between cursor
        pages, so once a known event is seen, everything after it (same
        page or later pages) is already-processed too. Returns how many
        NEW events were actually processed this call.
        """
        cursor = ""
        new_events_processed = 0

        for _page in range(config.MRKT_MAX_PAGES_PER_ITERATION):
            try:
                items, next_cursor = self.mrkt_client.feed(count=20, cursor=cursor)
            except MrktError as exc:
                self._classify_mrkt_error(exc, "feed fetch")
                break

            self.stats["pages_fetched"] += 1
            if not items:
                break
            self.stats["items_seen_total"] += len(items)

            hit_known_event = False
            for event in items:
                event_id = event.get("id")
                if event_id is None:
                    logger.warning("dropping MRKT event with no id: %s", event)
                    continue
                event_id = str(event_id)
                if db.is_event_processed(self.conn, MARKETPLACE, event_id):
                    self.stats["items_already_known"] += 1
                    hit_known_event = True
                    break

                self._process_event(event)
                db.mark_event_processed(self.conn, MARKETPLACE, event_id, datetime.now(timezone.utc))
                new_events_processed += 1

            cursor = next_cursor
            if hit_known_event:
                break

        return new_events_processed

    # Правка (missing-event-types fix): MRKT's own game mechanics --
    # confirmed unrelated to ordinary trading (measured on 200 feed
    # events: lucky_buy 4, plinko_win 3, crafting 1). `crafting` in
    # particular has isOnSale=true on its gift, but that's the result of
    # a craft, not a listing -- explicitly never written to `listings`.
    GAME_EVENT_TYPES = {"lucky_buy", "plinko_win", "crafting"}

    # Premarket lots (gifts not yet handed out). Measured 2026-09-20 on
    # the live server: 50 premarket_listing + 2 premarket_sale in 12 h,
    # every one logged as an unknown type. They are a known, deliberately
    # skipped kind: mrkt_client's floor query already drops any lot with
    # premarketStatus != "None", so collecting them would mean tracking
    # lots that can never be part of a floor. Counted separately, never
    # written to `listings`, and no longer logged as a warning -- the noise
    # was burying real warnings.
    PREMARKET_EVENT_TYPES = {"premarket_listing", "premarket_sale"}

    def _process_event(self, event: dict) -> None:
        event_type = event.get("type")
        gift = event.get("gift") or {}
        amount = event.get("amount")
        now = datetime.now(timezone.utc)

        if event_type == "listing":
            self._handle_listing_event(gift, amount, now)
        elif event_type == "change_price":
            self._handle_change_price_event(gift, amount, now)
        elif event_type == "sale":
            self._handle_sale_event(gift, amount, now)
        elif event_type == "unlisting":
            self._handle_unlisting_event(gift, now)
        elif event_type == "return":
            self._handle_return_event(gift, now)
        elif event_type in self.PREMARKET_EVENT_TYPES:
            self.stats["events_premarket_skipped"] += 1
        elif event_type in self.GAME_EVENT_TYPES:
            # КАК ТЕСТИРОВАТЬ item 3: counted SEPARATELY from
            # unknown_event_type_count, per spec -- these are known,
            # explicitly-skipped types, not mystery ones.
            self.stats["events_game_skipped"] += 1
        else:
            # КАК ТЕСТИРОВАТЬ item 4: the FULL event is logged (not just
            # id/type) so a genuinely new type can actually be diagnosed
            # from the log, per spec.
            self.stats["unknown_event_type_count"] += 1
            logger.warning("unknown MRKT event type=%r: %s", event_type, event)

    def _amount_nano(self, amount) -> int | None:
        """`amount` is ALREADY nano-TON (an int, confirmed live) -- NEVER
        multiplied by anything, same fixed convention as
        mrkt_client.py's salePrice parsing.
        """
        if amount is None:
            return None
        try:
            return int(amount)
        except (TypeError, ValueError):
            return None

    def _handle_listing_event(self, gift: dict, amount, now: datetime) -> None:
        """КАК ТЕСТИРОВАТЬ item 2: writes a `listings` row with
        marketplace='mrkt', tg_id taken from gift.name AS-IS (never
        constructed -- see mrkt_parsing.py).
        """
        if not is_purchasable_mrkt_gift(gift):
            return
        try:
            listing = parse_mrkt_listing(gift)
        except UnparseableMrktListing as exc:
            self.stats["parse_errors"] += 1
            logger.error("unparseable MRKT listing event, raw=%s error=%s", gift, exc)
            return

        price_nano = self._amount_nano(amount)
        if price_nano is not None:
            listing.price_nano = price_nano

        if (
            listing.price_nano is not None
            and listing.price_nano < config.MRKT_COLLECT_MIN_PRICE_NANO
        ):
            self.stats["collect_filtered_count"] += 1
            return

        db.insert_listing(self.conn, listing)
        db.touch_listing_lifecycle(
            self.conn, MARKETPLACE, listing.external_id, listing.collection_id,
            listing.model_name, listing.backdrop_name, listing.price_nano, now,
        )
        self.stats["new_listings"] += 1

    def _handle_change_price_event(self, gift: dict, amount, now: datetime) -> None:
        """КАК ТЕСТИРОВАТЬ items 3/4: a change_price event carries NO old
        price (per spec) -- old_price comes from OUR OWN DB. If the
        listing isn't in our DB at all (a change_price for a gift this
        poller never saw get listed, e.g. a cold start mid-stream), it
        is written as a NEW listing instead -- there is nothing to
        compare against, so no price_history row is created for it.
        """
        try:
            listing = parse_mrkt_listing(gift)
        except UnparseableMrktListing as exc:
            self.stats["parse_errors"] += 1
            logger.error("unparseable MRKT change_price event, raw=%s error=%s", gift, exc)
            return

        new_price_nano = self._amount_nano(amount)
        if new_price_nano is None:
            new_price_nano = listing.price_nano
        if new_price_nano is None:
            return
        listing.price_nano = new_price_nano

        existing = db.get_listing_price_and_listed_at(self.conn, MARKETPLACE, listing.external_id)
        if existing is None:
            # КАК ТЕСТИРОВАТЬ item 4: unknown listing -> create it, don't
            # record a change (nothing to compare the new price against).
            if new_price_nano < config.MRKT_COLLECT_MIN_PRICE_NANO:
                self.stats["collect_filtered_count"] += 1
                return
            db.insert_listing(self.conn, listing)
            db.touch_listing_lifecycle(
                self.conn, MARKETPLACE, listing.external_id, listing.collection_id,
                listing.model_name, listing.backdrop_name, new_price_nano, now,
            )
            self.stats["new_listings"] += 1
            return

        old_price_nano, _old_listed_at = existing
        db.touch_listing_lifecycle(
            self.conn, MARKETPLACE, listing.external_id, listing.collection_id,
            listing.model_name, listing.backdrop_name, new_price_nano, now,
        )
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
            is_noise = abs(delta_pct) < config.MRKT_PRICE_DROP_MIN_PCT
            if not is_noise:
                self.stats["price_drops_above_threshold"] += 1
                refreshed = self._maybe_refresh_floor_snapshot(listing, now)
                if refreshed is not None:
                    floor_at_drop_nano, floor_listed_count_at_drop = refreshed
                    floor_fetched_at = now
                    floor_level_at_drop = "pair"
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
        """Правка 3: on a significant drop, re-query MRKT's own PAIR
        floor (collection+model+backdrop, self-excluded by number) and
        write it into floor_snapshots (marketplace='mrkt') -- the SAME
        table/columns signals.py's cascade already reads for the
        "snapshot" floor source, level="pair" (see
        db.upsert_mrkt_pair_floor_snapshot). ALSO returns (floor_nano,
        listed_count) when the query found a real, usable ("ok") floor,
        so the caller can ALSO write floor_at_drop_nano into the SAME
        price_history row -- one network call serving both writes, same
        discipline as tonnel_poller.py's mirror-image method.

        MRKT_FLOOR_MIN_LISTED_COUNT is applied HERE (not in
        mrkt_client.pair_floor(), which has no threshold opinion of its
        own) -- a real floor with too few listings backing it is
        recorded as pair_floor_status="thin_pair_book", NOT "ok", so
        signals.py's existing `pair_floor_status == "ok"` gate already
        excludes it -- no separate cascade stage needed.
        """
        if not (listing.collection_name and listing.model_name and listing.backdrop_name):
            return None
        try:
            floor = self.mrkt_client.pair_floor(
                collection_name=listing.collection_name,
                model_name=listing.model_name,
                backdrop_name=listing.backdrop_name,
                exclude_number=listing.gift_number,
            )
        except MrktError as exc:
            # The pair is named in the message on purpose: with only the
            # lot id it is impossible to tell whether the same pair keeps
            # failing (a request defect) or the failures are scattered
            # (MRKT being flaky) -- raised by the daily review 2026-09-21,
            # 14 HTTP 400s in 33 h.
            self._classify_mrkt_error(
                exc, f"at-drop pair floor for {listing.external_id} "
                     f"[{listing.collection_name} / {listing.model_name} / {listing.backdrop_name}]")
            return None

        if floor.status == "ok" and floor.listed_count < config.MRKT_FLOOR_MIN_LISTED_COUNT:
            status = "thin_pair_book"
        else:
            status = floor.status

        db.upsert_mrkt_pair_floor_snapshot(
            self.conn, listing.external_id, listing.model_name, listing.backdrop_name,
            floor.floor_nano, floor.listed_count, status, now,
        )

        if status != "ok":
            return None
        return floor.floor_nano, floor.listed_count

    def _handle_sale_event(self, gift: dict, amount, now: datetime) -> None:
        """КАК ТЕСТИРОВАТЬ item 5: disappeared_at filled, final_status=
        'sold', sold_price_nano recorded -- MRKT's "sale" event is the
        ONLY marketplace signal in this project giving an EXPLICIT sale
        confirmation (see db.record_sale's docstring / README).

        ПРАВКА 1 (sale-vs-floor delivery): ALSO queries the pair floor at
        the moment of sale (self-excluded by gift_num) and records it via
        db.record_sale_floor -- see that function's docstring and
        README's "sale-vs-floor" section for why (the profit formula
        assumes a sale happens at the pair floor; this has never been
        measured). The sale itself is ALWAYS recorded first, unconditionally
        -- a failed/skipped floor query must never lose the sale record
        (КАК ТЕСТИРОВАТЬ items 2/3/4).
        """
        gift_id = gift.get("id")
        if gift_id is None:
            logger.warning("MRKT sale event missing gift.id: %s", gift)
            return
        sold_price_nano = self._amount_nano(amount)
        db.record_sale(self.conn, MARKETPLACE, str(gift_id), sold_price_nano, now)
        self.stats["sales_recorded"] += 1

        # ПРАВКА 2: never query a floor for a sale we don't even collect
        # -- MRKT_COLLECT_MIN_PRICE-priced-and-cheaper sales are 85% of
        # all trades (measured), and we're only interested in the
        # segment this project actually signals on.
        if sold_price_nano is None or sold_price_nano < config.MRKT_COLLECT_MIN_PRICE_NANO:
            return

        try:
            listing = parse_mrkt_listing(gift)
        except UnparseableMrktListing as exc:
            logger.warning("MRKT sale event gift unparseable for floor query, gift_id=%s: %s", gift_id, exc)
            return
        if not (listing.collection_name and listing.model_name and listing.backdrop_name):
            return

        try:
            floor = self.mrkt_client.pair_floor(
                collection_name=listing.collection_name,
                model_name=listing.model_name,
                backdrop_name=listing.backdrop_name,
                exclude_number=listing.gift_number,
            )
        except MrktError as exc:
            self._classify_mrkt_error(
                exc, f"floor at sale for {gift_id} "
                     f"[{listing.collection_name} / {listing.model_name} / {listing.backdrop_name}]")
            return

        if floor.status != "ok":
            # Thin book / no comparable listings after self-exclusion --
            # NOT an error, just nothing usable to record (КАК
            # ТЕСТИРОВАТЬ item 4). The sale above is already recorded.
            return
        db.record_sale_floor(self.conn, MARKETPLACE, str(gift_id), floor.floor_nano, floor.listed_count, now)
        self.stats["sale_floor_recorded"] += 1

    def _handle_unlisting_event(self, gift: dict, now: datetime) -> None:
        """КАК ТЕСТИРОВАТЬ item 1: disappeared_at filled, final_status=
        'unlisted' -- NOT 'sold'. Confirmed live example: PrettyPosy-132026,
        salePrice=6426000000, isOnSale=false. Critical per spec: without
        this, an unlisted lot stays in the DB looking active and would
        keep participating in this project's OWN floor computation
        (mrkt_client.pair_floor()'s live query naturally excludes it via
        isOnSale=false -- see КАК ТЕСТИРОВАТЬ item 5 -- but our OWN
        `listings`/lifecycle bookkeeping needs to reflect reality too,
        for report.py/liquidity stats).
        """
        gift_id = gift.get("id")
        if gift_id is None:
            logger.warning("MRKT unlisting event missing gift.id: %s", gift)
            return
        db.record_lifecycle_status(self.conn, MARKETPLACE, str(gift_id), "unlisted", now)
        self.stats["unlistings_recorded"] += 1

    def _handle_return_event(self, gift: dict, now: datetime) -> None:
        """КАК ТЕСТИРОВАТЬ item 2: final_status='returned' -- a gift
        returned to its owner in Telegram, a DIFFERENT outcome from both
        'sold' and 'unlisted' (see db.py's DISAPPEARED_STATUSES comment
        on why these must never be blended into one status).
        """
        gift_id = gift.get("id")
        if gift_id is None:
            logger.warning("MRKT return event missing gift.id: %s", gift)
            return
        db.record_lifecycle_status(self.conn, MARKETPLACE, str(gift_id), "returned", now)
        self.stats["returns_recorded"] += 1

    # --- Telegram notification (opt-in via MRKT_NOTIFY_ENABLED) ----------

    def _check_cooldown(self, signal) -> bool:
        last = db.get_last_sent_alert_for_listing(self.conn, MARKETPLACE, signal.listing_external_id)
        if last is None:
            return True
        elapsed_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last["sent_at"])).total_seconds() / 60
        return elapsed_min >= config.MRKT_SIGNAL_COOLDOWN_MIN

    def _check_signal_still_fresh(self, signal) -> bool | None:
        """One targeted lookup by (collection_name, number) -- the same
        find_by_number() mechanism this client already has -- immediately
        before sending, catching a lot sold/delisted between detection
        and send. Same True/False/None contract as poller.py/
        tonnel_poller.py's equivalents: None means the check itself
        failed (network/API error), never treated as confirmed-stale.
        """
        if not signal.collection_name or signal.gift_number is None:
            return None
        try:
            item = self.mrkt_client.find_by_number(signal.collection_name, signal.gift_number)
        except MrktError as exc:
            self._classify_mrkt_error(exc, f"freshness check for {signal.listing_external_id}")
            return None
        return lot_check.mrkt_available(item, signal.new_price_nano)

    def _maybe_cross_check(self, signal) -> None:
        """Правка 5: a PURE PRE-SEND FILTER -- mirrors poller.py's/
        tonnel_poller.py's identically-named method, both call the SAME
        shared cross_check.cross_check(), which dispatches by
        signal.marketplace (here, "mrkt" -> neighbours Portals + Tonnel,
        never MRKT itself -- see cross_check.py).
        """
        if not config.CROSS_CHECK_ENABLED:
            return
        if not (signal.collection_name and signal.model_name and signal.backdrop_name):
            return

        now = datetime.now(timezone.utc)
        cross_check(
            self.conn, signal, portals_client=self.portals_client, tonnel_client=self.tonnel_client, now=now,
        )

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
        # NOTIFY_LEVELS is a Portals-oriented setting -- every MRKT
        # signal IS level="pair" (MRKT's own pair floor is real and
        # workable, see config.MRKT_FLOOR_MIN_LISTED_COUNT) so this gate
        # is naturally satisfied for the default {"pair"} value; unlike
        # Tonnel (always level="model", needs the gate bypassed
        # entirely), MRKT genuinely belongs in the same bucket Portals'
        # pair-level signals do, so NOTIFY_LEVELS is applied normally.
        candidates = [
            s for s in all_signals
            if s.floor_level in config.NOTIFY_LEVELS
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
                    "MRKT signal suppressed by cooldown: listing_external_id=%s observed_at=%s",
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
                    "MRKT signal stale, not sent: listing_external_id=%s observed_at=%s",
                    signal.listing_external_id, signal.observed_at,
                )
                continue

            self._maybe_cross_check(signal)

            if signal.cross_verdict in BLOCKING_VERDICTS:
                db.mark_alert_sent(
                    self.conn, MARKETPLACE, signal.listing_external_id, signal.observed_at,
                    datetime.now(timezone.utc), status="skipped_cross_worse",
                )
                logger.info(
                    "MRKT signal skipped, cross_verdict=%s: listing_external_id=%s observed_at=%s "
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
                    "MRKT signal not sent, will retry: listing_external_id=%s observed_at=%s",
                    signal.listing_external_id, signal.observed_at,
                )
        if extra:
            self.notifier.send_text(f"...и ещё {extra} сигналов MRKT, см. отчёт (--marketplace mrkt)")

    # --- run loop + summary ----------------------------------------------

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
                    self._maybe_notify()
                except sqlite3.OperationalError as exc:
                    if "database is locked" not in str(exc):
                        raise
                    self.stats["db_locked_count"] += 1
                    logger.error("database is locked, skipping this poll cycle: %s", exc)
                elapsed = time.monotonic() - start
                self._sleep(max(0.0, config.MRKT_POLL_INTERVAL_SEC - elapsed))
        except KeyboardInterrupt:
            pass
        finally:
            self.print_summary()

    def print_summary(self) -> None:
        duration = (time.monotonic() - self._start_mono) if self._start_mono is not None else 0.0
        print()
        print("=== MRKT collector run summary ===")
        print(f"duration_sec: {duration:.1f}")
        print(f"iterations: {self._iterations}")
        for key in (
            "pages_fetched", "items_seen_total", "items_already_known",
            "new_listings", "collect_filtered_count", "parse_errors", "unknown_event_type_count",
            "events_game_skipped", "events_premarket_skipped",
            "price_changes_seen", "price_drops", "price_raises", "price_drops_above_threshold",
            "sales_recorded", "sale_floor_recorded", "unlistings_recorded", "returns_recorded",
            "mrkt_403_count", "mrkt_429_count", "mrkt_5xx_count", "mrkt_error_count",
            "signals_sent", "signals_send_failed", "signals_stale", "signals_suppressed_cooldown",
            "sent_no_neighbour", "sent_neighbour_higher", "skipped_neighbour_cheaper", "neighbour_thin", "error",
            "db_locked_count",
        ):
            print(f"{key}: {self.stats[key]}")


def build_default_poller(dsn: str = config.DB_DSN) -> MrktPoller:
    conn = db.connect(dsn)
    db.verify_schema(conn)

    token = config.get_mrkt_access_token()
    if not token:
        # Unlike the optional cross-check use of MRKT elsewhere, THIS
        # poller's entire job is querying MRKT -- there is no meaningful
        # "run without it" mode, so this fails loudly at startup, same
        # discipline as poller.py's PORTALS_AUTH requirement.
        raise config.ConfigError("MRKT_ACCESS_TOKEN is required to run mrkt_poller.py")
    mrkt_client = MrktClient(token_provider=lambda: config.get_mrkt_access_token() or token,
                             request_delay_ms=config.MRKT_REQUEST_DELAY_MS)

    notifier = None
    if config.MRKT_NOTIFY_ENABLED:
        bot_token = config.get_telegram_bot_token()
        owner_id = config.get_telegram_owner_id()
        viewer_ids = config.get_telegram_viewer_ids()
        notifier = TelegramNotifier(bot_token, chat_id=owner_id, viewer_chat_ids=viewer_ids)

    portals_client = None
    if config.CROSS_CHECK_ENABLED:
        from .auth import AuthManager
        from .portals_client import PortalsClient
        portals_auth = AuthManager()
        portals_client = PortalsClient(auth_provider=portals_auth.get)

    tonnel_client = TonnelClient(request_delay_ms=config.TONNEL_REQUEST_DELAY_MS)
    journal_conn = journal_db.connect(journal_config.JOURNAL_DB_DSN) if journal_config.PAPER_JOURNAL_ENABLED else None

    return MrktPoller(
        conn, mrkt_client=mrkt_client, notifier=notifier,
        portals_client=portals_client, tonnel_client=tonnel_client, journal_conn=journal_conn,
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
    except config.ConfigError as exc:
        print(f"Configuration error, refusing to start: {exc}", file=sys.stderr)
        return 1
    poller.run_forever(run_seconds=args.run_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
