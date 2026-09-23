"""Offline report over the accumulated DB. No network calls.

Run: python -m gift_sniper.report --db gift_sniper.db --usd-rate 3.15
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import config, db, signals
from .signals import _floor_and_level  # re-exported: used by the main distribution below


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def backfill_name_collisions(conn: sqlite3.Connection) -> None:
    """Idempotent. A model_name is a collision if it appears under more
    than one distinct collection_name across listings. Runs BEFORE any
    other calculation in this report, per spec.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT l.model_name, l.collection_name
        FROM listings l
        JOIN floor_snapshots f ON f.listing_external_id = l.external_id
        WHERE l.model_name IS NOT NULL
        """
    ).fetchall()

    collections_by_model: dict[str, set[str]] = defaultdict(set)
    for model_name, collection_name in rows:
        collections_by_model[model_name].add(collection_name)

    colliding_models = {m for m, cols in collections_by_model.items() if len(cols) > 1}

    with conn:
        conn.execute("UPDATE floor_snapshots SET name_collision = 0")
        if colliding_models:
            placeholders = ",".join("?" for _ in colliding_models)
            conn.execute(
                f"UPDATE floor_snapshots SET name_collision = 1 "
                f"WHERE model_name IN ({placeholders})",
                tuple(colliding_models),
            )
    return colliding_models


def _bucket(discount: Decimal) -> str:
    pct = discount * 100
    if pct < 0:
        return "<0%"
    if pct < 10:
        return "0-10%"
    if pct < 20:
        return "10-20%"
    if pct < 35:
        return "20-35%"
    return ">35%"


BUCKET_ORDER = ["<0%", "0-10%", "10-20%", "20-35%", ">35%"]


def _marketplace_breakdown_block(conn: sqlite3.Connection) -> str:
    """Правка 5: "разбивка по источникам" -- always shows BOTH
    marketplaces regardless of the --marketplace filter (that filter
    controls what comes AFTER this block, see generate_report).
    """
    out = ["=== listings by marketplace ==="]
    rows = conn.execute(
        "SELECT marketplace, COUNT(*), MIN(first_seen_at), MAX(first_seen_at) "
        "FROM listings GROUP BY marketplace ORDER BY marketplace"
    ).fetchall()
    if not rows:
        out.append("  (no listings collected yet)")
        return "\n".join(out)
    for mp, count, first, last in rows:
        out.append(f"  {mp}: {count} listings, {first} .. {last}")
    return "\n".join(out)


def _fetch_joined(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Portals-only, explicitly (schema v13, Tonnel full-collector
    delivery): floor_snapshots is never written for Tonnel listings (see
    tonnel_poller.py / db.insert_listing -- Tonnel's own pair floor comes
    from a live query, never a stored snapshot), so the JOIN alone would
    already exclude every Tonnel row -- the WHERE clause makes that
    intentional rather than incidental.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT l.*, f.api_combo_floor_nano, f.name_collision, f.floor_skip_reason,
               f.own_combo_floor_nano, f.own_sample_size, f.own_confidence,
               f.floor_sanity, f.pair_floor_nano, f.pair_listed_count,
               f.pair_floor_status, f.pair_floor_age_sec,
               f.pair_floor_excl_self_nano, f.pair_listed_count_excl_self,
               f.pair_self_was_floor,
               f.model_floor_excl_self_nano, f.model_listed_count_excl_self,
               f.model_floor_status,
               f.backdrop_name AS f_backdrop_name
        FROM listings l
        JOIN floor_snapshots f ON f.listing_external_id = l.external_id
        WHERE l.marketplace = 'portals'
        """
    ).fetchall()


def _pair_listed_count_bucket(n: int) -> str:
    if n <= 1:
        return "1"
    if n <= 3:
        return "2-3"
    if n <= 9:
        return "4-9"
    return "10+"


PAIR_COUNT_BUCKET_ORDER = ["1", "2-3", "4-9", "10+"]


def generate_report(
    conn: sqlite3.Connection,
    usd_rate: Decimal,
    db_path: str | None = None,
    drops_now: datetime | None = None,
    marketplace: str = "all",
) -> str:
    """`marketplace`: "all" (default) | "portals" | "tonnel" -- Правка 5
    (Tonnel full-collector delivery). The "=== listings by marketplace
    ===" breakdown always shows both sources, regardless of this
    argument (that IS the breakdown). Everything past it -- name
    collisions, blocked gifts, price drops, cross-market verification --
    is Portals-only content this delivery (signals/cross-check are
    этап 2 for Tonnel, see README): printed when marketplace is "all" or
    "portals", skipped with a one-line note when marketplace is "tonnel"
    (there is nothing Tonnel-specific there yet to print).
    """
    # Catches a stale/unmigrated DB HERE, with an actionable message, not
    # as a bare sqlite3.OperationalError from the middle of a SELECT deep
    # in this function. This is the same OperationalError class of bug
    # that hit api_combo_floor_nano at v1->v2 -- it happened again at
    # v4->v5 because report.py's CLI entry point used a raw
    # sqlite3.connect() instead of db.connect(), so migrations never ran
    # for this path even though db.py had the migration. verify_schema()
    # here is the backstop: it catches it regardless of how `conn` was
    # opened, including a caller who (still) bypasses db.connect().
    db.verify_schema(conn)

    colliding_models = backfill_name_collisions(conn)
    rows = _fetch_joined(conn)

    out: list[str] = []
    now = datetime.now().isoformat()
    period = conn.execute("SELECT MIN(first_seen_at), MAX(first_seen_at) FROM listings").fetchone()

    out.append("=== Gift Sniper report ===")
    out.append(f"generated_at: {now}")
    out.append(f"data period (first_seen_at): {period[0]} .. {period[1]}")
    out.append(f"usd_rate: {usd_rate}")
    out.append("")
    out.append(_marketplace_breakdown_block(conn))
    out.append("")

    if marketplace in ("tonnel", "mrkt"):
        # Правка 5 (Tonnel) / Правка 3 (MRKT full-signaller delivery):
        # SAME cascade/clean-signals presentation as Portals
        # (_price_drops_block, parameterized by marketplace) -- everything
        # ELSE in this function (name collisions, blocked gifts, the
        # api/own/pair three-way floor comparison) is Portals-API-specific
        # (collection_floor_nano/unlocks_at/api_combo_floor_nano have no
        # Tonnel/MRKT equivalent) and stays out of scope, per spec.
        out.append(_price_drops_block(conn, usd_rate, now=drops_now, marketplace=marketplace))
        return "\n".join(out)

    if db_path and db_path != ":memory:" and os.path.exists(db_path):
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        out.append(f"db file size: {size_mb:.2f} MB")
    else:
        out.append("db file size: n/a (in-memory or path not given)")
    raw_count = conn.execute("SELECT COUNT(*) FROM listings WHERE raw IS NOT NULL").fetchone()[0]
    out.append(f"listings with raw stored: {raw_count} / {conn.execute('SELECT COUNT(*) FROM listings').fetchone()[0]}")
    out.append(
        f"MARKETPLACE_FEE_RATE: {config.MARKETPLACE_FEE_RATE} (assumption, "
        f"see README: field confirmed via /market/config, interpretation as "
        f"sale-time fee is unconfirmed)"
    )
    out.append(
        f"WITHDRAWAL_FEE_FLAT: {config.WITHDRAWAL_FEE_FLAT} "
        f"({config.WITHDRAWAL_FEE_FLAT_NANO} nano) (assumption, see README: "
        f"staleTime/withdrawal_fee field confirmed, flat-per-withdrawal "
        f"interpretation is an assumption)"
    )
    out.append(f"FLOOR_MIN_PRICE: {config.FLOOR_MIN_PRICE} (floor lookups skipped below this price)")

    config_rows = conn.execute(
        "SELECT fetched_at, commission, offer_fee, withdrawal_fee, user_cashback, usd_course "
        "FROM market_config_snapshots ORDER BY fetched_at ASC"
    ).fetchall()
    out.append(f"market_config snapshots observed in this period: {len(config_rows)}")
    for row in config_rows:
        out.append(f"  {row[0]}: commission={row[1]} offer_fee={row[2]} withdrawal_fee={row[3]} "
                    f"user_cashback={row[4]} usd_course={row[5]}")
    if len(config_rows) > 1:
        out.append(
            "  WARNING: market_config changed during the collection period. "
            "The profit calculation below uses a single MARKETPLACE_FEE_RATE/ "
            "WITHDRAWAL_FEE_FLAT value from env for the whole period -- actual "
            "fees varied, so profit numbers are approximate, not exact."
        )

    total = len(rows)
    blocked = [r for r in rows if r["unlocks_at"] and r["unlocks_at"] > r["first_seen_at"]]
    blocked_ids = {r["external_id"] for r in blocked}
    unblocked = [r for r in rows if r["external_id"] not in blocked_ids]
    out.append(f"total listings: {total}")
    out.append(
        f"blocked at listing time (unlocks_at > first_seen_at): "
        f"{len(blocked)} ({(len(blocked)/total*100) if total else 0:.1f}%)"
    )
    out.append("")

    delays = []
    for r in unblocked:
        if r["listed_at"] and r["first_seen_at"]:
            d = (datetime.fromisoformat(r["first_seen_at"]) - datetime.fromisoformat(r["listed_at"])).total_seconds()
            delays.append(d)
    delays.sort()
    out.append(
        f"detection delay (unblocked): median={_percentile(delays, 0.5)} "
        f"p90={_percentile(delays, 0.9)} n={len(delays)}"
    )

    below_threshold = [r for r in unblocked if r["floor_skip_reason"] == "below_price_threshold"]
    not_returned = [r for r in unblocked if r["floor_skip_reason"] == "model_not_returned"]
    out.append(
        f"cut by FLOOR_MIN_PRICE threshold (API/pair floor lookups skipped; own floor still computed): "
        f"{len(below_threshold)} ({(len(below_threshold)/len(unblocked)*100) if unblocked else 0:.1f}% of unblocked)"
    )
    out.append(f"model_not_returned (server dropped model from API batch, twice): {len(not_returned)}")
    out.append("")

    # --- three methods side by side: quantifying how wrong the two ---
    # --- retired sources are, now that pair_floor is the ground truth ---
    # Confirmed live: api_combo_floor_nano is scoped by model name only,
    # globally across all collections (Berry Box 9.35 -> 200.0; Liberty
    # Figure 4.39 -> 65-97). own_combo_floor_nano is confirmed
    # systematically too high (~2x) because the poller only observes
    # newly-listed items, missing older still-active cheap listings.
    all_three_known = [
        r for r in rows
        if r["api_combo_floor_nano"] is not None
        and r["own_combo_floor_nano"] not in (None, 0)
        and r["pair_floor_nano"] not in (None, 0)
    ]
    api_pair_ratios = sorted(
        Decimal(r["api_combo_floor_nano"]) / Decimal(r["pair_floor_nano"]) for r in all_three_known
    )
    own_pair_ratios = sorted(
        Decimal(r["own_combo_floor_nano"]) / Decimal(r["pair_floor_nano"]) for r in all_three_known
    )
    out.append("three methods side by side (rows where api, own, AND pair floor are all known):")
    out.append(f"  n: {len(all_three_known)}")
    out.append(f"  median(api/pair): {_percentile([float(x) for x in api_pair_ratios], 0.5)}")
    out.append(f"  median(own/pair): {_percentile([float(x) for x in own_pair_ratios], 0.5)}")
    frac_api_over_2 = (
        sum(1 for x in api_pair_ratios if x > 2) / len(api_pair_ratios) * 100
    ) if api_pair_ratios else 0.0
    frac_own_over_2 = (
        sum(1 for x in own_pair_ratios if x > 2) / len(own_pair_ratios) * 100
    ) if own_pair_ratios else 0.0
    out.append(f"  fraction with api/pair > 2: {frac_api_over_2:.1f}%")
    out.append(f"  fraction with own/pair > 2: {frac_own_over_2:.1f}%")
    out.append("")

    # --- comparison-level hierarchy: the only gate on the main ---
    # --- distribution now. pair first, model as fallback -- see ---
    # --- _floor_and_level / Правка 3. Never both in one distribution. ---
    excluded_no_level = [
        r for r in unblocked if _floor_and_level(r)[1] is None
    ]
    out.append(
        f"excluded from main distribution -- neither pair nor model floor usable "
        f"(alone in pair AND alone at model level, or no_data/error at both): "
        f"{len(excluded_no_level)} ({(len(excluded_no_level)/len(unblocked)*100) if unblocked else 0:.1f}% of unblocked)"
    )
    self_was_floor_count = sum(1 for r in unblocked if r["pair_self_was_floor"])
    out.append(
        f"of which, the listing itself WAS the pre-exclusion pair floor "
        f"(the exact self-comparison bug this excludes): {self_was_floor_count}"
    )
    out.append("")

    # --- thin-book filter (Правка 1, FLOOR_MIN_LISTED_COUNT): confirmed ---
    # --- live, 18/20 manually-reviewed "clean" signals had ---
    # --- listed_count_excl_self == 1 -- one OTHER listing's price taken ---
    # --- as "the floor", frequently a mispriced/unsold lot rather than a ---
    # --- market fact. Applied identically to pair and model level. See ---
    # --- config.py / README for the measured liquidity breakdown this ---
    # --- threshold is based on. ---
    def _level(r):
        return _floor_and_level(r, config.FLOOR_MIN_LISTED_COUNT)

    def _raw_level(r):
        return _floor_and_level(r, 1)

    thin_book = [
        r for r in unblocked
        if r["price_nano"] is not None and _raw_level(r)[1] is not None and _level(r)[1] is None
    ]

    def _thin_book_count(r) -> int:
        _floor, raw_level = _raw_level(r)
        if raw_level == "pair":
            return r["pair_listed_count_excl_self"]
        if raw_level == "model":
            return r["model_listed_count_excl_self"]
        return 0

    thin_book_cnt1 = sum(1 for r in thin_book if _thin_book_count(r) == 1)
    thin_book_cnt2 = sum(1 for r in thin_book if _thin_book_count(r) == 2)
    out.append(
        f"thin-book (excluded) -- floor found but listed_count_excl_self < "
        f"FLOOR_MIN_LISTED_COUNT ({config.FLOOR_MIN_LISTED_COUNT}): {len(thin_book)}"
    )
    out.append(f"  of which cnt=1: {thin_book_cnt1}  cnt=2: {thin_book_cnt2}")
    out.append("")

    # --- main distribution: pair floor, SELF-EXCLUDED, is the FIRST ---
    # --- source used for discount/profit; model floor (also self- ---
    # --- excluded) is the fallback when pair is alone_in_pair. Confirmed ---
    # --- live (manual review of 20 top price drops, 17/20 artifacts): ---
    # --- using pair_floor_nano (includes self) compares a lot against ---
    # --- its own price whenever it's alone in its pair or currently the ---
    # --- cheapest -- see pair_floor.py. Model floor is a deliberately ---
    # --- coarser comparison (different backdrops price differently ---
    # --- within one model) -- NEVER combined with pair-level signals in ---
    # --- one distribution; each row's floor_level says which it used. ---
    # --- Both levels also gated by FLOOR_MIN_LISTED_COUNT (Правка 1). ---
    eligible = []
    for r in unblocked:
        if r["price_nano"] is None:
            continue
        floor, level = _level(r)
        if floor is None:
            continue
        eligible.append(r)

    def discount_distribution(rows_subset) -> dict[str, int]:
        buckets = {b: 0 for b in BUCKET_ORDER}
        for r in rows_subset:
            floor, _level_name = _level(r)
            discount = Decimal(1) - Decimal(r["price_nano"]) / floor
            buckets[_bucket(discount)] += 1
        return buckets

    eligible_pair = [r for r in eligible if _level(r)[1] == "pair"]
    eligible_model = [r for r in eligible if _level(r)[1] == "model"]

    pair_dist = discount_distribution(eligible_pair)
    out.append(f"discount distribution vs PAIR floor, self-excluded (n={len(eligible_pair)}, level=pair):")
    for b in BUCKET_ORDER:
        out.append(f"  {b:<8}{pair_dist[b]:>8}")
    out.append("")

    model_dist = discount_distribution(eligible_model)
    out.append(
        f"discount distribution vs MODEL floor, self-excluded (n={len(eligible_model)}, level=model -- "
        f"COARSER, never combined with the pair-level distribution above):"
    )
    for b in BUCKET_ORDER:
        out.append(f"  {b:<8}{model_dist[b]:>8}")
    out.append("")

    out.append("discount distribution by pair_listed_count_excl_self (liquidity), vs PAIR floor, level=pair only:")
    out.append(f"{'count':<8}{'n':>6}" + "".join(f"{b:>10}" for b in BUCKET_ORDER))
    for count_bucket in PAIR_COUNT_BUCKET_ORDER:
        subset = [r for r in eligible_pair if _pair_listed_count_bucket(r["pair_listed_count_excl_self"]) == count_bucket]
        dist = discount_distribution(subset)
        out.append(f"{count_bucket:<8}{len(subset):>6}" + "".join(f"{dist[b]:>10}" for b in BUCKET_ORDER))
    out.append("")

    profitable = []
    for r in eligible:
        floor, level = _level(r)
        discount = Decimal(1) - Decimal(r["price_nano"]) / floor
        if discount <= Decimal("0.10"):
            continue
        price = Decimal(r["price_nano"])
        depth = r["pair_listed_count_excl_self"] if level == "pair" else r["model_listed_count_excl_self"]
        profit_before_withdrawal_nano, profit_nano = signals.compute_profit_nano("portals", floor, price, depth)
        profitable.append(
            {
                "external_id": r["external_id"],
                "model_name": r["model_name"],
                "backdrop_name": r["backdrop_name"],
                "pair_listed_count": r["pair_listed_count_excl_self"],
                "floor_level": level,
                "discount": discount,
                "profit_nano": profit_nano,
                "profit_before_withdrawal_nano": profit_before_withdrawal_nano,
                "profit_usd": (Decimal(profit_nano) / config.NANO) * usd_rate,
            }
        )

    out.append(f"signals with discount > 10% (pair or model floor self-excluded, not blocked): {len(profitable)}")
    profitable_pair = sum(1 for p in profitable if p["floor_level"] == "pair")
    profitable_model = sum(1 for p in profitable if p["floor_level"] == "model")
    out.append(f"  of which level=pair: {profitable_pair}  level=model: {profitable_model}")
    gt5 = sum(1 for p in profitable if p["profit_usd"] > 5)
    gt20 = sum(1 for p in profitable if p["profit_usd"] > 20)
    out.append(f"  profit > $5 : {gt5}")
    out.append(f"  profit > $20: {gt20}")
    out.append("")

    pair_counts: dict[tuple[str, str], int] = defaultdict(int)
    for r in eligible:
        if r["model_name"] and r["f_backdrop_name"]:
            pair_counts[(r["model_name"], r["f_backdrop_name"])] += 1
    top_pairs = sorted(pair_counts.items(), key=lambda kv: -kv[1])[:20]
    out.append("top 20 model+backdrop pairs by signal count (eligible rows):")
    for (model, backdrop), count in top_pairs:
        out.append(f"  {model} + {backdrop}: {count}")
    out.append("")

    out.append(
        "name collisions detected (diagnostic only -- pair floor is scoped by "
        "collection_id and immune to these by construction):"
    )
    if colliding_models:
        model_to_collections: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            if r["model_name"] in colliding_models:
                model_to_collections[r["model_name"]].add(r["collection_name"])
        for model, cols in model_to_collections.items():
            out.append(f"  {model}: {sorted(cols)}")
    else:
        out.append("  none")
    out.append("")

    out.append("blocked gifts (unlocks_at in the future at first_seen_at):")
    out.append(f"  count: {len(blocked)}")
    blocked_dist = discount_distribution(
        [r for r in blocked if _level(r)[0] is not None and r["price_nano"] is not None]
    )
    for b in BUCKET_ORDER:
        out.append(f"  {b}: {blocked_dist[b]}")
    out.append("")

    out.append(_price_drops_block(conn, usd_rate, now=drops_now))
    out.append("")
    out.append(_cross_direction_block(conn))

    return "\n".join(out)


DROP_BUCKET_ORDER = ["1-3%", "3-10%", "10-25%", ">25%"]


def _drop_bucket(abs_delta_pct: Decimal) -> str | None:
    if abs_delta_pct < 1:
        return None  # below any reporting bucket -- caller already filters noise separately
    if abs_delta_pct < 3:
        return "1-3%"
    if abs_delta_pct < 10:
        return "3-10%"
    if abs_delta_pct < 25:
        return "10-25%"
    return ">25%"


backfill_ladder = signals.backfill_ladder  # re-exported: single source of truth, see signals.py


def _price_drops_block(
    conn: sqlite3.Connection, usd_rate: Decimal, now: datetime | None = None, marketplace: str = "portals"
) -> str:
    """Second signal type, alongside "new listing below floor": a price
    CUT on an already-known listing. Confirmed live: listed_at updates on
    a no-op "touch" and is not a signal of anything -- only price
    comparison (never listed_at) decides whether something changed; see
    poller.py.

    The filter cascade itself (is_anomaly -> noise -> is_ladder -> floor
    no_data -> thin-book -> is_implausible -> is_bulk_update -> CLEAN)
    lives in signals.run_cascade() -- the SAME function notifier.py's
    Telegram alerts are built on (via signals.clean_signals()), so the
    two can never drift apart. This function is purely presentation: it
    turns signals.CascadeResult into the printed diagnostic block.

    `marketplace` (Tonnel signals delivery, Правка 5): selects both the
    rows (run_cascade's own marketplace filter) AND the threshold values
    printed alongside each stage (Tonnel has its own TONNEL_* thresholds,
    see signals._thresholds_for) -- SAME cascade, SAME presentation code,
    never a duplicated Tonnel version of this function.

    ДЕФЕКТ (min-1.7-signals-per-lot delivery): run_cascade's OWN
    selection no longer depends on backfill_ladder() (see
    signals._ladder_listings_live -- a plain SELECT, no side effect).
    run_cascade DOES still call backfill_ladder() itself, internally, on
    every invocation -- restored after removing it broke external code
    that read price_history.is_ladder directly, expecting it current;
    see README. report.py doesn't need its own separate call any more.
    """
    cascade = signals.run_cascade(conn, now=now, marketplace=marketplace)
    thresholds = signals._thresholds_for(marketplace)
    rows = cascade.rows

    out: list[str] = []
    out.append(f"=== price drops ({marketplace}) ===" if marketplace != "portals" else "=== price drops ===")

    out.append(f"total price changes recorded: {len(rows)}")
    out.append(f"drops: {len(cascade.drops)}  raises: {len(cascade.raises)}")
    out.append("")

    out.append("filter cascade (each stage's count is drawn from the REMAINDER of the prior stage):")
    out.append(f"  total drops: {len(cascade.drops)}")

    remaining_after_anomaly = len(cascade.drops) - len(cascade.anomalies)
    out.append(f"  - is_anomaly (single-step > {config.PRICE_DROP_MAX_PCT}%, or burst within "
               f"{config.PRICE_DROP_BURST_SEC}s): {len(cascade.anomalies)} removed, {remaining_after_anomaly} remain")

    remaining_after_noise = remaining_after_anomaly - len(cascade.noise)
    # Label kept EXACTLY "PRICE_DROP_MIN_PCT" for marketplace="portals"
    # (existing tests/tooling match this literal string) -- Tonnel/MRKT
    # print their OWN threshold name/value instead, never Portals' env
    # var name attached to another marketplace's number.
    noise_label = {"portals": "PRICE_DROP_MIN_PCT", "tonnel": "TONNEL_PRICE_DROP_MIN_PCT"}.get(
        marketplace, "MRKT_PRICE_DROP_MIN_PCT"
    )
    out.append(f"  - below {noise_label} ({thresholds['price_drop_min_pct']}%, bot noise): "
               f"{len(cascade.noise)} removed, {remaining_after_noise} remain")

    remaining_after_ladder = remaining_after_noise - len(cascade.ladders)
    out.append(f"  - is_ladder (relister-bot walk-down, >= {config.LADDER_MIN_DROPS} SIGNIFICANT "
               f"drops / {config.LADDER_WINDOW_HOURS}h): {len(cascade.ladders)} removed, {remaining_after_ladder} remain")

    remaining_after_floor = remaining_after_ladder - len(cascade.no_floor)
    out.append(f"  - floor no_data (lot alone in its pair, post self-exclusion; "
               f"or no floor available at all, at_drop or snapshot): {len(cascade.no_floor)} removed, {remaining_after_floor} remain")

    remaining_after_stale = remaining_after_floor - len(cascade.stale_floor)
    out.append(f"  - stale_floor (snapshot-sourced floor older than MAX_SNAPSHOT_AGE_MIN="
               f"{config.MAX_SNAPSHOT_AGE_MIN}min relative to the drop; at_drop floors exempt by "
               f"construction): {len(cascade.stale_floor)} removed, {remaining_after_stale} remain")

    remaining_after_own_floor = remaining_after_stale - len(cascade.price_above_own_floor)
    out.append(f"  - price_above_own_floor (new_price_nano >= floor_nano, no discount at all -- "
               f"measured 18/25=72% of Portals model-level signals): {len(cascade.price_above_own_floor)} removed, "
               f"{remaining_after_own_floor} remain")

    remaining_after_profit = remaining_after_own_floor - len(cascade.below_min_profit)
    out.append(f"  - below_min_profit (profit < MIN_SIGNAL_PROFIT_TON={config.MIN_SIGNAL_PROFIT_TON} TON, "
               f"computed at BOTH floor levels via signals.compute_profit_nano -- measured live, 548/1379=40% "
               f"of Portals' prior 'clean' signals had ratio < 1.05): {len(cascade.below_min_profit)} removed, "
               f"{remaining_after_profit} remain")

    remaining_after_profit -= len(cascade.unstable_floor)
    out.append(f"  - unstable_floor (pair floor max/min >= FLOOR_MAX_INSTABILITY={config.FLOOR_MAX_INSTABILITY} "
               f"within {config.FLOOR_STABILITY_WINDOW_HOURS}h, needs 3+ snapshots): "
               f"{len(cascade.unstable_floor)} removed, {remaining_after_profit} remain")

    listed_count_label = {"portals": "FLOOR_MIN_LISTED_COUNT", "tonnel": "TONNEL_FLOOR_MIN_LISTED_COUNT"}.get(
        marketplace, "MRKT_FLOOR_MIN_LISTED_COUNT"
    )
    remaining_after_thin = remaining_after_profit - len(cascade.thin_book)
    out.append(f"  - thin-book (listed_count_excl_self < {listed_count_label}="
               f"{thresholds['floor_min_listed_count']}): {len(cascade.thin_book)} removed "
               f"(cnt=1: {cascade.thin_book_cnt1}, cnt=2: {cascade.thin_book_cnt2}), {remaining_after_thin} remain")

    ratio_label = {"portals": "FLOOR_MAX_RATIO_TO_PRICE", "tonnel": "TONNEL_FLOOR_MAX_RATIO_TO_PRICE"}.get(
        marketplace, "MRKT_FLOOR_MAX_RATIO_TO_PRICE"
    )
    remaining_after_implausible = remaining_after_thin - len(cascade.implausible)
    out.append(f"  - is_implausible (floor/price > {ratio_label}="
               f"{thresholds['floor_max_ratio_to_price']}): {len(cascade.implausible)} removed, {remaining_after_implausible} remain")

    remaining_after_bulk = remaining_after_implausible - len(cascade.bulk_updates)
    out.append(f"  - is_bulk_update (>= 2 different listings, same collection, same delta_pct, "
               f"within SAME_SECOND_WINDOW={config.SAME_SECOND_WINDOW}s): {len(cascade.bulk_updates)} removed, "
               f"{remaining_after_bulk} remain")

    remaining_after_no_floor_at_send = remaining_after_bulk - len(cascade.no_floor_at_send)
    out.append(f"  - no_floor_at_send (hardcoded backstop, ignores every threshold -- floor missing or "
               f"listed_count < 1 at send time; confirmed live, 6 sent Tonnel signals had "
               f"floor_level_at_drop=NULL and floor_listed_count_at_drop=0): "
               f"{len(cascade.no_floor_at_send)} removed, {remaining_after_no_floor_at_send} remain")
    out.append("")

    clean = cascade.clean
    out.append(f"CLEAN signals: {len(clean)}")
    dist = {b: 0 for b in DROP_BUCKET_ORDER}
    for r in clean:
        bucket = _drop_bucket(abs(Decimal(r["delta_pct"])))
        if bucket:
            dist[bucket] += 1
    out.append("distribution by drop size (clean signals only):")
    for b in DROP_BUCKET_ORDER:
        out.append(f"  {b:<8}{dist[b]:>8}")
    out.append("")

    sig_list = [signals._signal_from_row(r, usd_rate, marketplace=marketplace) for r in clean]

    at_drop_count = sum(1 for s in sig_list if s.floor_source == "at_drop")
    snapshot_count = sum(1 for s in sig_list if s.floor_source == "snapshot")
    out.append(f"clean signal floor source: at_drop={at_drop_count}  snapshot(backfilled)={snapshot_count}")
    level_pair_count = sum(1 for s in sig_list if s.floor_level == "pair")
    level_model_count = sum(1 for s in sig_list if s.floor_level == "model")
    out.append(f"clean signal floor level: pair={level_pair_count}  model={level_model_count} (never combined -- see discount distribution above)")

    # ДОПОЛНЕНИЕ (min-profit-threshold delivery): profit_usd is no longer
    # None at level="model" -- it's computed everywhere now (see
    # compute_profit_nano / Signal.profit_is_estimate) and every
    # surviving row already cleared MIN_SIGNAL_PROFIT_TON via the
    # below_min_profit cascade stage above. Split CONFIRMED (level=pair,
    # a real achievable sale price) from ESTIMATED (level=model, a
    # different backdrop's price -- see Signal's docstring) so the two
    # are never silently blended into one number.
    confirmed_sig_list = [s for s in sig_list if not s.profit_is_estimate]
    estimated_sig_list = [s for s in sig_list if s.profit_is_estimate]
    gt5 = sum(1 for s in confirmed_sig_list if s.profit_usd is not None and s.profit_usd > 5)
    gt20 = sum(1 for s in confirmed_sig_list if s.profit_usd is not None and s.profit_usd > 20)
    out.append(
        f"clean signal profit, CONFIRMED (level=pair, n={len(confirmed_sig_list)}): "
        f"profit > $5: {gt5}  profit > $20: {gt20}"
    )
    est_gt5 = sum(1 for s in estimated_sig_list if s.profit_usd is not None and s.profit_usd > 5)
    out.append(
        f"clean signal profit, ESTIMATED (level=model, n={len(estimated_sig_list)}, "
        f"different backdrop's floor -- not a guaranteed sale price): profit > $5: {est_gt5}"
    )
    out.append("")

    if cascade.implausible:
        out.append(f"implausible floor/price examples (up to 10, of {len(cascade.implausible)}):")
        for r in cascade.implausible[:10]:
            floor, _source, _level, _cnt, _fetched_at = signals._floor_and_source(r)
            new_price = Decimal(r["new_price_nano"]) / config.NANO
            floor_units = floor / config.NANO
            ratio = floor / Decimal(r["new_price_nano"])
            out.append(
                f"  {(r['collection_name'] or '')[:15]}, {(r['model_name'] or '')} + "
                f"{(r['backdrop_name'] or '')}: price {new_price:.2f}, floor {floor_units:.2f}, "
                f"ratio {ratio:.1f}"
            )
        out.append("")

    out.append(f"up to 20 clean signals (agregates hide artifacts -- this is what showed them):")
    out.append(
        f"{'collection':<16}{'model':<14}{'backdrop':<12}{'#':>7}"
        f"{'old':>10}{'new':>10}{'floor':>10}{'ratio':>7}{'profit_ton':>11}{'src':>10}{'level':>7}{'cnt':>5}  observed_at"
    )
    for s in sorted(sig_list, key=lambda s: -s.discount)[:20]:
        old_price = Decimal(s.old_price_nano) / config.NANO
        new_price = Decimal(s.new_price_nano) / config.NANO
        floor_units = Decimal(s.floor_nano) / config.NANO
        # ДОПОЛНЕНИЕ (min-profit-threshold delivery): profit_ton, with a
        # trailing "~" for level="model" rows -- profit_is_estimate,
        # never shown with the same confidence as a real pair-level
        # number, see Signal's docstring.
        profit_ton = Decimal(s.profit_nano) / config.NANO if s.profit_nano is not None else None
        profit_str = f"{profit_ton:.2f}{'~' if s.profit_is_estimate else ''}" if profit_ton is not None else "n/a"
        out.append(
            f"{(s.collection_name or '')[:15]:<16}{(s.model_name or '')[:13]:<14}"
            f"{(s.backdrop_name or '')[:11]:<12}{s.gift_number or '':>7}"
            f"{old_price:>10.2f}{new_price:>10.2f}{floor_units:>10.2f}{s.ratio:>7.1f}{profit_str:>11}{s.floor_source:>10}"
            f"{s.floor_level:>7}{s.listed_count:>5}  {s.observed_at.isoformat()}"
        )
    out.append("")

    out.append(f"anomalous drops (is_anomaly): {len(cascade.anomalies)}")
    out.append(f"ladder-down listings (is_ladder rows): {len(cascade.ladders)} rows across {len(cascade.ladder_listings)} listings")
    if cascade.ladder_listings:
        out.append("ladder detail (steps, total drop %, avg step %, avg interval):")
        for listing_id in sorted(cascade.ladder_listings):
            steps = sorted(
                (r for r in rows if r["listing_external_id"] == listing_id and Decimal(r["delta_pct"]) < 0),
                key=lambda r: r["observed_at"],
            )
            if len(steps) < 2:
                continue
            step_pcts = [abs(Decimal(r["delta_pct"])) for r in steps]
            first_price = Decimal(steps[0]["old_price_nano"])
            last_price = Decimal(steps[-1]["new_price_nano"])
            total_drop_pct = (Decimal(1) - last_price / first_price) * 100
            times = [datetime.fromisoformat(r["observed_at"]) for r in steps]
            intervals = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
            avg_interval = sum(intervals) / len(intervals) if intervals else 0
            out.append(
                f"  {listing_id}: {len(steps)} steps, total {total_drop_pct:.1f}%, "
                f"avg step {sum(step_pcts) / len(step_pcts):.1f}%, "
                f"avg interval {avg_interval / 60:.1f} min"
            )

    return "\n".join(out)


def _cross_direction_block(conn: sqlite3.Connection) -> str:
    """Правка 3/5 (unified-notification delivery) + Правка 3 (MRKT
    third-neighbour delivery): cross-check is a pure pre-send filter --
    this block shows the breakdown for EVERY (signal, neighbour)
    direction pair SEPARATELY (Portals->Tonnel, Portals->MRKT,
    Tonnel->Portals, Tonnel->MRKT), by cross_check.py's verdict values
    (sent_no_neighbour/sent_neighbour_higher/skipped_neighbour_cheaper/
    neighbour_thin/error), reading the direction-agnostic
    cross_check_snapshots table (now keyed by signal_marketplace AND
    checked_marketplace -- a signal can have snapshots for MULTIPLE
    neighbours at once, so grouping by signal_marketplace alone is no
    longer enough to isolate one direction). Only
    "skipped_neighbour_cheaper" ever blocked a send -- see
    cross_check.BLOCKING_VERDICTS -- so that's the list shown with an
    overpay ratio, up to 10, worst first.
    """
    from .cross_check import BLOCKING_VERDICTS

    out: list[str] = []
    out.append("=== cross-market verification (all directions) ===")

    rows = db.latest_cross_check_snapshots(conn)
    if not rows:
        out.append("  (no cross-checks recorded yet)")
        return "\n".join(out)

    by_direction: dict[tuple[str, str], list] = {}
    for row in rows:
        key = (row["signal_marketplace"], row["checked_marketplace"])
        by_direction.setdefault(key, []).append(row)

    verdict_order = (
        "sent_no_neighbour", "sent_neighbour_higher", "skipped_neighbour_cheaper", "neighbour_thin", "error",
    )

    for signal_marketplace, checked_marketplace, label in (
        ("portals", "tonnel", "Portals -> Tonnel"),
        ("portals", "mrkt", "Portals -> MRKT"),
        ("tonnel", "portals", "Tonnel -> Portals"),
        ("tonnel", "mrkt", "Tonnel -> MRKT"),
        ("mrkt", "portals", "MRKT -> Portals"),
        ("mrkt", "tonnel", "MRKT -> Tonnel"),
    ):
        direction_rows = by_direction.get((signal_marketplace, checked_marketplace), [])
        out.append(f"-- {label} --")
        out.append(f"signals checked: {len(direction_rows)}")
        if not direction_rows:
            out.append("  (none)")
            out.append("")
            continue

        cascade = signals.run_cascade(conn, marketplace=signal_marketplace)
        clean_by_id = {r["listing_external_id"]: r for r in cascade.clean}

        counts = {v: 0 for v in verdict_order}
        skipped_list: list[tuple] = []
        for row in direction_rows:
            verdict = row["verdict"]
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict in BLOCKING_VERDICTS:
                price_row = clean_by_id.get(row["listing_external_id"])
                if price_row is not None and row["neighbour_floor_nano"]:
                    price_nano = int(price_row["new_price_nano"])
                    overpay_ratio = Decimal(row["neighbour_floor_nano"]) / Decimal(price_nano)
                    skipped_list.append((row, price_nano, overpay_ratio))

        total = len(direction_rows)
        share_line = "  ".join(
            f"{v}: {counts[v]} ({counts[v] / total * 100:.0f}%)" for v in verdict_order if counts[v] or v in counts
        )
        out.append(share_line)
        out.append(f"skipped (verdict=skipped_neighbour_cheaper, up to 10 of {len(skipped_list)}):")
        if not skipped_list:
            out.append("  none")
        for row, price_nano, overpay_ratio in sorted(skipped_list, key=lambda t: -t[2])[:10]:
            price = Decimal(price_nano) / config.NANO
            neighbour_floor = Decimal(row["neighbour_floor_nano"]) / config.NANO
            out.append(
                f"  {(row['collection_name'] or '')[:15]}, {(row['model_name'] or '')} + {(row['backdrop_name'] or '')}: "
                f"{signal_marketplace} {price:.2f}, {checked_marketplace} {neighbour_floor:.2f}, "
                f"разрыв x{overpay_ratio:.2f}"
            )
        out.append("")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--usd-rate", required=True, type=str, help="Required, no default.")
    parser.add_argument(
        "--marketplace", choices=["portals", "tonnel", "mrkt", "all"], default="all",
        help="Правка 5: which source's detail sections to print (the marketplace breakdown "
        "block always shows both, regardless of this flag).",
    )
    args = parser.parse_args(argv)

    try:
        usd_rate = Decimal(args.usd_rate)
    except Exception:
        parser.error(f"--usd-rate must be a decimal number, got {args.usd_rate!r}")
        return 2
    if usd_rate <= 0:
        parser.error("--usd-rate must be > 0")
        return 2

    try:
        conn = db.connect(args.db)  # runs migrations -- NOT a bare sqlite3.connect()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to generate report: {exc}", file=sys.stderr)
        return 1

    print(generate_report(conn, usd_rate, db_path=args.db, marketplace=args.marketplace))
    return 0


if __name__ == "__main__":
    sys.exit(main())
