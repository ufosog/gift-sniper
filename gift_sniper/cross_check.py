"""Правка 3 (unified-notification delivery): cross-check is a PURE
PRE-SEND FILTER -- it decides only whether a signal gets sent at all,
never anything about how it's displayed (see notifier.py -- the header
is always "ЛИСТИНГ ✓" regardless of cross_verdict).

Правка 2 (MRKT third-neighbour delivery) + Правка 5 (MRKT full-signaller
delivery): checks a clean signal against EVERY available neighbour
marketplace, not just one -- for a Portals signal, neighbours are
Tonnel and MRKT; for a Tonnel signal, neighbours are Portals and MRKT;
for an MRKT signal (now a real signal source, not just a cross-check
neighbour), neighbours are Portals and Tonnel -- never MRKT itself. Let
P be the signal's own price and N a given
neighbour's floor price (already adjusted for whatever buyer-side fee
applies to buying FROM that neighbour -- Tonnel's confirmed 10% when
Tonnel is the neighbour, MRKT's salePrice taken AS-IS (its 2% fee is
already baked in, confirmed live, never re-applied), no fee at all when
Portals is the neighbour):

  N absent (no comparable neighbour listing)     -> that neighbour votes SEND ("sent_no_neighbour")
  N <= P * (1 + CROSS_MIN_GAP_PCT/100)            -> that neighbour votes DO NOT SEND ("skipped_neighbour_cheaper")
  N >  P * (1 + CROSS_MIN_GAP_PCT/100)            -> that neighbour votes SEND ("sent_neighbour_higher")

ONE neighbour voting "skipped_neighbour_cheaper" is enough to block the
whole signal -- per spec, "достаточно ОДНОГО такого соседа". Combining
rule across all queried neighbours (highest-priority match wins):
  1. any neighbour skipped_neighbour_cheaper -> overall skipped_neighbour_cheaper (BLOCKED)
  2. any neighbour sent_neighbour_higher     -> overall sent_neighbour_higher (an independent real confirmation exists)
  3. any neighbour neighbour_thin            -> overall neighbour_thin
  4. any neighbour error                     -> overall error
  5. any neighbour model_bound_inconclusive  -> overall model_bound_inconclusive
  6. otherwise                               -> overall sent_no_neighbour

ПРАВКА 3 (realization-rate delivery): a neighbour with NO listing of the
pair (not an error) is asked for its MODEL floor, a proven lower bound
on its pair price. The rule is ASYMMETRIC -- it can only disprove a
profit, never confirm one: price >= model floor -> skipped_neighbour_cheaper
(blocks); price < model floor -> model_bound_inconclusive (sends, no ✓).
See _decide_model_bound. Never extend it into a confirmation.
A neighbour that was never queried at all (client not configured, e.g.
no MRKT_ACCESS_TOKEN) simply isn't in the list -- it does not count as
"no_neighbour" or "error" for that direction, it's just absent.

CROSS_MIN_GAP_PCT (env, default 10) is the minimum gap that makes THIS
signal worth sending over what's available at a given neighbour.

CROSS_MIN_NEIGHBOUR_COUNT (env, default 3) protects against a lonely
neighbour listing being mistaken for a real market price -- the SAME
class of bug already fixed for Portals' own pair floor and Tonnel's own
model floor (see pair_floor.py / tonnel_poller.py's
TONNEL_MODEL_MIN_LISTED_COUNT). Below this threshold, a neighbour's
price is NOT used in that neighbour's own vote -- treated exactly like
"absent" for that neighbour ("neighbour_thin").

ДЕФЕКТ 5 (systemic-check delivery): CROSS_MIN_NEIGHBOUR_COUNT=3 was
measured live to be UNREACHABLE -- 102 real snapshots, no neighbour
EVER had 3+ listings. Depth is not the only way to trust a price: if
2+ neighbours' floors agree within CROSS_AGREEMENT_PCT of each other
(default 25%), that agreement is its OWN vote (via _decide, with the
depth check forced to pass), added alongside the normal per-neighbour
votes before combining -- see cross_check() below. A single thin
neighbour, alone, is unaffected -- still votes neighbour_thin.

A neighbour query failure (network/API error) votes "error" for THAT
neighbour only -- it never blocks the signal by itself, and never stops
the other neighbours from being queried (see cross_check() below: each
neighbour is tried independently, one's exception is caught before
moving to the next).

Only the per-marketplace network fetch differs -- the three marketplaces'
APIs are genuinely different (Tonnel's tonnel_client.pair_floor(),
Portals' portals_client.PortalsClient.search_pair_floor() +
pair_floor.py's response parsing, MRKT's mrkt_client.pair_floor()) --
there is nothing left to share there without inventing a fake
abstraction over three unrelated wire protocols. The verdict math
itself (_decide below) IS shared, unchanged, across every neighbour and
both signal directions.

ВАЖНО ПРО ВАЛЮТЫ: Portals prices are in GRAM, Tonnel and MRKT prices are
in TON. Confirmed 1:1 (GRAM is a renamed TON, same underlying value) --
so no numeric conversion is needed, but fields are still never compared
without going through this module's explicit rate assumption: see
_RATE_GRAM_PER_TON below. If that assumption is ever wrong, this is the
one place to fix it.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from . import config, db
from .errors import PortalsError
from .mrkt_client import MrktError
from .pair_floor import _floor_from_response
from .signals import Signal
from .tonnel_client import TonnelError

logger = logging.getLogger("gift_sniper.cross_check")

# Confirmed: GRAM (Portals) is a renamed TON (Tonnel/MRKT) -- same token,
# 1:1. Kept as an explicit named constant (not a bare "1") so a
# comparison is never accidentally read as "these are obviously the same
# unit" -- it's an assumption, recorded once, here.
_RATE_GRAM_PER_TON = Decimal(1)

# Правка 5: the only verdict values that exist -- every one of these is
# recorded, and only "skipped_neighbour_cheaper" ever blocks a send.
VERDICT_SENT_NO_NEIGHBOUR = "sent_no_neighbour"
VERDICT_SENT_NEIGHBOUR_HIGHER = "sent_neighbour_higher"
VERDICT_SKIPPED_NEIGHBOUR_CHEAPER = "skipped_neighbour_cheaper"
VERDICT_NEIGHBOUR_THIN = "neighbour_thin"
VERDICT_ERROR = "error"
# ПРАВКА 3 (realization-rate delivery): the neighbour has no listing of the
# PAIR, but its MODEL floor is below-or-above our price in a way that
# proves nothing -- see _decide_model_bound. Never blocks, never confirms.
VERDICT_MODEL_BOUND_INCONCLUSIVE = "model_bound_inconclusive"

# Verdicts that block the send -- everything else sends. Kept as an
# explicit set (not "== VERDICT_SKIPPED_NEIGHBOUR_CHEAPER" scattered
# across callers) so the "what blocks a send" decision lives in one
# place, matching the rest of this project's discipline.
BLOCKING_VERDICTS = {VERDICT_SKIPPED_NEIGHBOUR_CHEAPER}

# Priority order used to combine multiple neighbours' votes into one
# overall Signal.cross_verdict -- see module docstring's combining rule.
# Index 0 = highest priority (checked first).
_VERDICT_PRIORITY = [
    VERDICT_SKIPPED_NEIGHBOUR_CHEAPER,
    VERDICT_SENT_NEIGHBOUR_HIGHER,
    VERDICT_NEIGHBOUR_THIN,
    VERDICT_ERROR,
    VERDICT_MODEL_BOUND_INCONCLUSIVE,
    VERDICT_SENT_NO_NEIGHBOUR,
]


def _buy_cost_nano(signal: Signal) -> int:
    """What a buyer really pays for the SIGNAL lot, on the same basis as
    the neighbour floors (Tonnel neighbours are taken with the 10% buyer
    fee, MRKT salePrice already has its fee, Portals has no buyer fee).
    Measured 2026-09-19: comparing the raw Tonnel price instead let 10 of
    28 cross-checked Tonnel signals through that the neighbours undercut
    (example: Bonded Ring #7588 at 40.25, cost 44.28, MRKT model floor
    42.13, MRKT sales of the collection at 40-42)."""
    if signal.marketplace == "tonnel":
        return int(Decimal(signal.new_price_nano) * Decimal("1.1"))
    return signal.new_price_nano


def _decide(price_nano: int, neighbour_floor_nano: int | None, neighbour_listed_count: int) -> str:
    """Pure function -- no network, no I/O. One neighbour's vote. See
    module docstring for the full decision table.
    """
    if neighbour_floor_nano is None:
        return VERDICT_SENT_NO_NEIGHBOUR
    if neighbour_listed_count < config.CROSS_MIN_NEIGHBOUR_COUNT:
        return VERDICT_NEIGHBOUR_THIN
    threshold = Decimal(price_nano) * (1 + config.CROSS_MIN_GAP_PCT / 100)
    if Decimal(neighbour_floor_nano) <= threshold:
        return VERDICT_SKIPPED_NEIGHBOUR_CHEAPER
    return VERDICT_SENT_NEIGHBOUR_HIGHER


def _decide_model_bound(price_nano: int, model_floor_nano: int | None) -> str:
    """Pure function. One neighbour's vote from its MODEL floor, used only
    when that neighbour has no listing of the PAIR at all.

    Measured on 821 rows with both floors known: model floor was NEVER
    above the pair floor (it's a minimum over all backdrops, the pair
    floor a minimum over one). So the model floor is a LOWER BOUND on
    the neighbour's pair price, and the rule is deliberately ASYMMETRIC:
      price >= model floor -> the neighbour certainly can offer this
                              model at or below our price -> block
      price <  model floor -> the neighbour's pair price is higher, but
                              by an unknown amount -> NOT a confirmation,
                              only "inconclusive" (never a checkmark).
    Never extend this to confirm a signal -- a lower bound cannot prove
    the neighbour's pair is expensive enough to matter.
    """
    if model_floor_nano is None:
        return VERDICT_SENT_NO_NEIGHBOUR
    if Decimal(price_nano) >= Decimal(model_floor_nano):
        return VERDICT_SKIPPED_NEIGHBOUR_CHEAPER
    return VERDICT_MODEL_BOUND_INCONCLUSIVE


def _combine(votes: list[str]) -> str:
    """Combines every queried neighbour's vote into one overall verdict,
    per the priority order in the module docstring. `votes` empty (no
    neighbour was even queried, e.g. all clients absent) -> sent_no_neighbour.
    """
    vote_set = set(votes)
    for verdict in _VERDICT_PRIORITY:
        if verdict in vote_set:
            return verdict
    return VERDICT_SENT_NO_NEIGHBOUR


def cross_check(
    conn: sqlite3.Connection,
    signal: Signal,
    *,
    portals_client=None,
    tonnel_client=None,
    mrkt_client=None,
    now: datetime | None = None,
) -> None:
    """Mutates `signal` in place: cross_verdict (the COMBINED verdict
    across every queried neighbour) and the neighbour_* fields (set to
    whichever neighbour's vote decided the combined verdict, for
    display/debugging -- the authoritative per-neighbour record is
    ALWAYS the cross_check_snapshots rows, one per neighbour, written
    regardless). Never raises -- a neighbour-side failure votes "error"
    for that neighbour only and never prevents the other neighbours from
    being queried (see module docstring).

    When CROSS_CHECK_ENABLED is false, or the signal lacks a full
    (collection, model, backdrop) triple to compare, this is a no-op --
    signal.cross_verdict stays its "not_checked" default, which callers
    must treat as "send" (not_checked is not in BLOCKING_VERDICTS).
    """
    if not config.CROSS_CHECK_ENABLED:
        return
    if not (signal.collection_name and signal.model_name and signal.backdrop_name):
        return

    now = now or datetime.now(timezone.utc)

    if signal.marketplace == "portals":
        neighbour_specs = [
            ("tonnel", tonnel_client, _query_tonnel_neighbour, _query_tonnel_neighbour_model),
            ("mrkt", mrkt_client, _query_mrkt_neighbour, _query_mrkt_neighbour_model),
        ]
    elif signal.marketplace == "tonnel":
        neighbour_specs = [
            ("portals", portals_client, _query_portals_neighbour, _query_portals_neighbour_model),
            ("mrkt", mrkt_client, _query_mrkt_neighbour, _query_mrkt_neighbour_model),
        ]
    else:  # signal.marketplace == "mrkt" -- Правка 5, MRKT full-signaller
        # delivery: neighbours are Portals and Tonnel, NEVER MRKT itself.
        neighbour_specs = [
            ("portals", portals_client, _query_portals_neighbour, _query_portals_neighbour_model),
            ("tonnel", tonnel_client, _query_tonnel_neighbour, _query_tonnel_neighbour_model),
        ]

    results = []  # (checked_marketplace, floor_nano, listed_count, status, verdict)
    for checked_marketplace, client, query_fn, model_query_fn in neighbour_specs:
        if not config.MRKT_CROSS_CHECK_ENABLED and checked_marketplace == "mrkt":
            continue
        if client is None:
            continue

        try:
            floor_nano, listed_count, status = query_fn(conn, client, signal)
        except (TonnelError, PortalsError, MrktError) as exc:
            logger.warning(
                "cross-check (%s signal -> %s neighbour) failed for %s: %s",
                signal.marketplace, checked_marketplace, signal.listing_external_id, exc,
            )
            floor_nano, listed_count, status = None, 0, "error"

        usable_floor = floor_nano if status == "ok" else None
        verdict = VERDICT_ERROR if status == "error" else _decide(_buy_cost_nano(signal), usable_floor, listed_count)
        snapshot_floor = usable_floor

        # ПРАВКА 3: neighbour has no listing of the PAIR (not an error) ->
        # ask for its MODEL floor as a lower bound. Only in this case --
        # a neighbour that has the pair is never asked (one extra request
        # only when needed).
        if status not in ("ok", "error"):
            try:
                model_floor_nano, model_count, model_status = model_query_fn(conn, client, signal)
            except (TonnelError, PortalsError, MrktError) as exc:
                logger.warning(
                    "cross-check model bound (%s signal -> %s neighbour) failed for %s: %s",
                    signal.marketplace, checked_marketplace, signal.listing_external_id, exc,
                )
                model_floor_nano, model_count, model_status = None, 0, "error"
            if model_status == "error":
                verdict, status = VERDICT_ERROR, "error"
            elif model_status == "ok" and model_floor_nano is not None:
                verdict = _decide_model_bound(_buy_cost_nano(signal), model_floor_nano)
                status, listed_count, snapshot_floor = "model_bound", model_count, model_floor_nano

        # usable_floor stays the PAIR floor only -- a model bound never
        # takes part in the neighbour-agreement vote below.
        results.append((checked_marketplace, usable_floor, listed_count, status, verdict, snapshot_floor))

        db.record_cross_check_snapshot(
            conn, signal.marketplace, checked_marketplace, signal.listing_external_id,
            signal.collection_name, signal.model_name, signal.backdrop_name,
            snapshot_floor, listed_count, verdict, now,
        )

    if not results:
        return  # no neighbour configured at all -- stays "not_checked"

    # ДЕФЕКТ 5 (systemic-check delivery): two neighbours' floors that
    # AGREE (within CROSS_AGREEMENT_PCT of each other) form their own
    # vote, bypassing CROSS_MIN_NEIGHBOUR_COUNT for that vote alone --
    # see config.CROSS_AGREEMENT_PCT's comment for the measured
    # justification. Added as an EXTRA vote into the same priority-
    # ordered combine, never a full override -- if some OTHER neighbour
    # already independently cleared CROSS_MIN_NEIGHBOUR_COUNT with a
    # higher-priority verdict (e.g. skipped_neighbour_cheaper), that
    # still wins, unchanged.
    priced = [r for r in results if r[3] == "ok" and r[1] is not None]
    if len(priced) >= 2:
        cheapest = min(priced, key=lambda r: r[1])
        agreeing = [
            r for r in priced
            if r is not cheapest and (Decimal(r[1] - cheapest[1]) / Decimal(cheapest[1])) * 100 < config.CROSS_AGREEMENT_PCT
        ]
        if agreeing:
            consensus_verdict = _decide(_buy_cost_nano(signal), cheapest[1], config.CROSS_MIN_NEIGHBOUR_COUNT)
            results.append((
                f"{cheapest[0]}+{'+'.join(r[0] for r in agreeing)} (agreement)",
                cheapest[1], cheapest[2], "ok_agreement", consensus_verdict, cheapest[1],
            ))

    overall = _combine([r[4] for r in results])
    signal.cross_verdict = overall
    # Deciding neighbour: the first (in the same priority order) whose
    # vote matches the overall verdict -- for display/debugging only,
    # the real per-neighbour record is the snapshots written above.
    deciding = next(r for r in results if r[4] == overall)
    signal.neighbour_marketplace = deciding[0]
    signal.neighbour_floor_nano = deciding[5]
    signal.neighbour_listed_count = deciding[2]
    signal.neighbour_status = deciding[3]


def _query_tonnel_neighbour(conn, tonnel_client, signal: Signal) -> tuple[int | None, int, str]:
    """Buyer-side conversion: Tonnel's confirmed 10% BUYER fee --
    floor.floor_with_fee_nano already has it baked in (see
    tonnel_client.TonnelFloor), so N = floor_with_fee_nano.
    """
    floor = tonnel_client.pair_floor(
        gift_name=signal.collection_name,
        model=signal.model_name,
        backdrop=signal.backdrop_name,
        exclude_gift_num=signal.gift_number,
    )
    floor_nano = floor.floor_with_fee_nano if floor.status == "ok" else None
    return floor_nano, floor.listed_count, floor.status


def _query_portals_neighbour(conn, portals_client, signal: Signal) -> tuple[int | None, int, str]:
    """Portals has no buyer-side fee (confirmed: search_pair_floor's
    price IS what a buyer pays) -- N = the neighbour floor AS-IS.

    Requires a Portals collection_id to query search_pair_floor (which
    REQUIRES it to actually filter, see portals_client.py) -- a Tonnel
    listing's own collection_id is always None, so it's looked up by
    collection_name from whatever Portals has already collected (see
    db.get_portals_collection_id_by_name). No lookup possible (we've
    never seen this collection on Portals) is treated exactly like "no
    comparable neighbour" -- status "no_data", never raising.
    """
    collection_id = db.get_portals_collection_id_by_name(conn, signal.collection_name)
    if collection_id is None:
        return None, 0, "no_data"

    resp = portals_client.search_pair_floor(collection_id, signal.model_name, signal.backdrop_name, limit=20, offset=0)

    # Tonnel's own external_id (a gift_num-derived integer) never
    # collides with a Portals listing id (a UUID string) -- passing it as
    # the "exclude" id is a no-op exclusion, reusing pair_floor.py's
    # response parsing (self-exclusion machinery) purely for its correct
    # floor/count computation, not because self-exclusion is actually
    # needed across two different marketplaces' listings.
    parsed = _floor_from_response(resp, exclude_external_id=signal.listing_external_id)
    floor_nano = parsed.floor_excluding_self_nano  # GRAM-nano, no buyer fee -- Portals has none
    listed_count = parsed.listed_count_excluding_self
    status = "ok" if floor_nano is not None else "no_data"
    return floor_nano, listed_count, status


def _query_tonnel_neighbour_model(conn, tonnel_client, signal: Signal) -> tuple[int | None, int, str]:
    """Tonnel MODEL floor (no backdrop), buyer fee baked in -- same
    conversion as _query_tonnel_neighbour.
    """
    floor = tonnel_client.model_floor(
        gift_name=signal.collection_name,
        model=signal.model_name,
        exclude_gift_num=signal.gift_number,
    )
    floor_nano = floor.floor_with_fee_nano if floor.status == "ok" else None
    return floor_nano, floor.listed_count, floor.status


def _query_portals_neighbour_model(conn, portals_client, signal: Signal) -> tuple[int | None, int, str]:
    """Portals MODEL floor (no backdrop), no buyer fee -- same parsing as
    _query_portals_neighbour.
    """
    collection_id = db.get_portals_collection_id_by_name(conn, signal.collection_name)
    if collection_id is None:
        return None, 0, "no_data"
    resp = portals_client.search_model_floor(collection_id, signal.model_name, limit=50, offset=0)
    parsed = _floor_from_response(resp, exclude_external_id=signal.listing_external_id)
    floor_nano = parsed.floor_excluding_self_nano
    status = "ok" if floor_nano is not None else "no_data"
    return floor_nano, parsed.listed_count_excluding_self, status


def _query_mrkt_neighbour_model(conn, mrkt_client, signal: Signal) -> tuple[int | None, int, str]:
    """MRKT MODEL floor (no backdrop), salePrice AS-IS."""
    floor = mrkt_client.model_floor(
        collection_name=signal.collection_name,
        model_name=signal.model_name,
        exclude_number=signal.gift_number,
    )
    floor_nano = floor.floor_nano if floor.status == "ok" else None
    return floor_nano, floor.listed_count, floor.status


def _query_mrkt_neighbour(conn, mrkt_client, signal: Signal) -> tuple[int | None, int, str]:
    """MRKT's salePrice is taken AS-IS -- its 2% fee is already baked in
    (confirmed live: salePrice/salePriceWithoutFee = 1.0200 on every lot
    checked), never re-applied. `exclude_number` uses the signal's own
    gift_number -- meaningful only for a Tonnel/Portals signal that
    happens to also exist on MRKT under the same number, a genuine
    self-exclusion (unlike the Portals-direction's no-op exclusion by a
    foreign id shape).
    """
    floor = mrkt_client.pair_floor(
        collection_name=signal.collection_name,
        model_name=signal.model_name,
        backdrop_name=signal.backdrop_name,
        exclude_number=signal.gift_number,
    )
    floor_nano = floor.floor_nano if floor.status == "ok" else None
    return floor_nano, floor.listed_count, floor.status
