"""Offline audit: how much does the MODEL-level floor (fallback, no
backdrop filter -- see pair_floor.py) disagree with the PAIR-level floor
(the precise, model+backdrop comparison)? Read-only, no network calls, no
schema changes, no changes to report.py's filter logic -- this is
measurement only.

WHY: of ~200 clean price-drop signals, ~180 are computed against the
MODEL floor and only ~20 against the PAIR floor (model floor is used far
more often because most (model, backdrop) pairs have exactly one active
listing -- see pair_floor.py). Model floor is coarser: different
backdrops within one model price very differently (measured previously:
Emperor/Ice Cream's Black backdrop at 22.49 vs. a 4.10 median across
backdrops -- a 5x spread). A "discount vs. model floor" can therefore be
nothing more than a cheap backdrop, not a real arbitrage opportunity.
This script measures whether, and under what conditions, the model level
can be trusted.

Run: python -m gift_sniper.model_level_audit --db gift_sniper.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import config, db
from .report import _floor_and_level, _percentile

CROSS_CHECK_WINDOW_SEC = 3600  # "within 1 hour of the drop" per spec

SPREAD_BUCKET_ORDER = ["<1.5", "1.5-3", "3-10", ">10"]


def _spread_bucket(max_over_min: Decimal) -> str:
    if max_over_min < Decimal("1.5"):
        return "<1.5"
    if max_over_min < 3:
        return "1.5-3"
    if max_over_min < 10:
        return "3-10"
    return ">10"


# --- shared read-only helpers (deliberately NOT imported from poller.py/ ---
# --- report.py's mutating backfill passes -- this script never writes to ---
# --- the DB, so ladder/bulk-update detection is recomputed in memory ---
# --- instead of relying on persisted is_ladder / DB UPDATEs). Logic ---
# --- mirrors report.py's cascade exactly; report.py itself is untouched ---
# --- per spec ("Никаких изменений в poller.py, report.py"). ---


def _ladder_listings(conn: sqlite3.Connection, now: datetime) -> set[str]:
    window_start = now - timedelta(hours=config.LADDER_WINDOW_HOURS)
    rows = conn.execute(
        """
        SELECT listing_external_id, COUNT(*) FROM price_history
        WHERE new_price_nano < old_price_nano
          AND is_noise = 0
          AND observed_at >= ?
        GROUP BY listing_external_id
        """,
        (window_start.isoformat(),),
    ).fetchall()
    return {ext_id for ext_id, drop_count in rows if drop_count >= config.LADDER_MIN_DROPS}


def _find_bulk_update_ids(rows_subset) -> set[tuple[str, str]]:
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows_subset:
        groups[(r["collection_name"], str(r["delta_pct"]))].append(r)

    bulk_keys: set[tuple[str, str]] = set()
    for group_rows in groups.values():
        group_rows.sort(key=lambda r: r["observed_at"])
        times = [datetime.fromisoformat(r["observed_at"]) for r in group_rows]
        cluster = [group_rows[0]]
        for i in range(1, len(group_rows)):
            if (times[i] - times[i - 1]).total_seconds() <= config.SAME_SECOND_WINDOW:
                cluster.append(group_rows[i])
            else:
                if len({r["listing_external_id"] for r in cluster}) >= 2:
                    bulk_keys.update((r["listing_external_id"], r["observed_at"]) for r in cluster)
                cluster = [group_rows[i]]
        if len({r["listing_external_id"] for r in cluster}) >= 2:
            bulk_keys.update((r["listing_external_id"], r["observed_at"]) for r in cluster)
    return bulk_keys


def _floor_and_source(r) -> tuple[Decimal | None, str | None, str | None, int]:
    """Same hierarchy as report.py's floor_and_source: at_drop first (the
    fresh, moment-of-drop query, with its level recorded in
    floor_level_at_drop), snapshot fallback otherwise (current
    floor_snapshots row, pair first then model -- _floor_and_level).
    """
    if r["floor_at_drop_nano"] not in (None, 0):
        level = r["floor_level_at_drop"] or "pair"
        listed_count = r["floor_listed_count_at_drop"] or 0
        return Decimal(r["floor_at_drop_nano"]), "at_drop", level, listed_count
    floor, level = _floor_and_level(r, 1)
    if floor is not None:
        listed_count = r["pair_listed_count_excl_self"] if level == "pair" else r["model_listed_count_excl_self"]
        return floor, "snapshot", level, listed_count
    return None, None, None, 0


def clean_signals(conn: sqlite3.Connection) -> list[dict]:
    """Reproduces report.py's price-drops filter cascade (is_anomaly ->
    noise -> is_ladder -> floor no_data -> thin-book -> is_implausible ->
    is_bulk_update) purely in memory, read-only. Returns one dict per
    surviving signal, at ANY level (pair or model) -- callers filter by
    level as needed.
    """
    now = datetime.now(timezone.utc)
    ladder_listings = _ladder_listings(conn, now)

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ph.*, l.collection_name, l.model_name, l.backdrop_name, l.gift_number,
               f.pair_floor_excl_self_nano, f.pair_floor_status, f.pair_listed_count_excl_self,
               f.model_floor_excl_self_nano, f.model_floor_status, f.model_listed_count_excl_self,
               f.floor_fetched_at AS f_floor_fetched_at
        FROM price_history ph
        JOIN listings l ON l.external_id = ph.listing_external_id
        LEFT JOIN floor_snapshots f ON f.listing_external_id = ph.listing_external_id
        """
    ).fetchall()

    remaining = [r for r in rows if Decimal(r["delta_pct"]) < 0]
    remaining = [r for r in remaining if not r["is_anomaly"]]
    remaining = [r for r in remaining if not r["is_noise"]]
    remaining = [r for r in remaining if r["listing_external_id"] not in ladder_listings]
    remaining = [r for r in remaining if _floor_and_source(r)[0] is not None]
    remaining = [r for r in remaining if _floor_and_source(r)[3] >= config.FLOOR_MIN_LISTED_COUNT]

    def _ratio(r) -> Decimal | None:
        floor, *_ = _floor_and_source(r)
        new_price = Decimal(r["new_price_nano"])
        if new_price == 0:
            return None
        return floor / new_price

    remaining = [r for r in remaining if not ((_ratio(r) or Decimal(0)) > config.FLOOR_MAX_RATIO_TO_PRICE)]

    bulk_ids = _find_bulk_update_ids(remaining)
    remaining = [r for r in remaining if (r["listing_external_id"], r["observed_at"]) not in bulk_ids]

    signals = []
    for r in remaining:
        floor, source, level, listed_count = _floor_and_source(r)
        new_price = Decimal(r["new_price_nano"])
        discount = Decimal(1) - new_price / floor
        signals.append(
            {
                "row": r,
                "floor": floor,
                "source": source,
                "level": level,
                "listed_count": listed_count,
                "discount": discount,
            }
        )
    return signals


# --- Part 1: direct level comparison -----------------------------------


def _part1_direct_comparison(conn: sqlite3.Connection) -> tuple[str, float | None]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT pair_floor_excl_self_nano, model_floor_excl_self_nano
        FROM floor_snapshots
        WHERE pair_floor_excl_self_nano IS NOT NULL AND pair_floor_excl_self_nano != 0
          AND model_floor_excl_self_nano IS NOT NULL AND model_floor_excl_self_nano != 0
        """
    ).fetchall()

    out = ["=== Part 1: direct pair vs. model floor comparison ==="]
    n = len(rows)
    out.append(f"rows with BOTH pair and model floor filled: {n}")
    if n == 0:
        out.append("(nothing to compare -- no row has both levels filled)")
        out.append("")
        return "\n".join(out), None

    ratios = sorted(
        Decimal(r["model_floor_excl_self_nano"]) / Decimal(r["pair_floor_excl_self_nano"]) for r in rows
    )
    ratios_f = [float(x) for x in ratios]
    median = _percentile(ratios_f, 0.5)
    p10 = _percentile(ratios_f, 0.10)
    p25 = _percentile(ratios_f, 0.25)
    p75 = _percentile(ratios_f, 0.75)
    p90 = _percentile(ratios_f, 0.90)
    out.append(f"ratio = model_floor / pair_floor:")
    out.append(f"  median={median:.3f}  p10={p10:.3f}  p25={p25:.3f}  p75={p75:.3f}  p90={p90:.3f}")

    over = sum(1 for x in ratios if x > Decimal("1.5"))
    under = sum(1 for x in ratios if x < Decimal("1") / Decimal("1.5"))
    out.append(
        f"ratio > 1.5 (model floor overstates by 50%+): {over} ({over/n*100:.1f}%)"
    )
    out.append(
        f"ratio < 0.67 (model floor understates by 33%+): {under} ({under/n*100:.1f}%)"
    )
    out.append("(ratio > 1 means the model floor is HIGHER than the pair floor -- a model-level signal overstates the discount)")
    out.append("")
    return "\n".join(out), median


# --- Part 2: what would happen to model-level signals -------------------


def _part2_signal_impact(conn: sqlite3.Connection) -> tuple[str, dict]:
    signals = clean_signals(conn)
    model_signals = [s for s in signals if s["level"] == "model"]

    out = ["=== Part 2: model-level signals checked against pair floor ==="]
    out.append(f"clean model-level signals: {len(model_signals)}")

    with_cross_check = []
    for s in model_signals:
        r = s["row"]
        if r["pair_floor_status"] != "ok":
            continue
        if r["pair_floor_excl_self_nano"] in (None, 0):
            continue
        if not r["f_floor_fetched_at"]:
            continue
        fetched_at = datetime.fromisoformat(r["f_floor_fetched_at"])
        observed_at = datetime.fromisoformat(r["observed_at"])
        if abs((fetched_at - observed_at).total_seconds()) > CROSS_CHECK_WINDOW_SEC:
            continue

        new_price = Decimal(r["new_price_nano"])
        pair_floor = Decimal(r["pair_floor_excl_self_nano"])
        discount_pair = Decimal(1) - new_price / pair_floor
        with_cross_check.append(
            {
                "row": r,
                "discount_model": s["discount"],
                "discount_pair": discount_pair,
                "would_survive": discount_pair > Decimal("0.10"),
            }
        )

    out.append(
        f"of which, have a pair floor to cross-check against "
        f"(within {CROSS_CHECK_WINDOW_SEC // 60} min of the drop): {len(with_cross_check)}"
    )

    survived = [c for c in with_cross_check if c["would_survive"]]
    disappeared = [c for c in with_cross_check if not c["would_survive"]]
    out.append(f"  would REMAIN a signal (discount vs. pair floor still > 10%): {len(survived)}")
    out.append(f"  would DISAPPEAR (discount vs. pair floor <= 10%): {len(disappeared)}")
    out.append("")

    if disappeared:
        model_discounts = sorted(float(c["discount_model"]) for c in disappeared)
        pair_discounts = sorted(float(c["discount_pair"]) for c in disappeared)
        out.append("of the disappeared signals, distribution of discount by level:")
        out.append(
            f"  model discount: median={_percentile(model_discounts, 0.5):.2%} "
            f"p10={_percentile(model_discounts, 0.10):.2%} p90={_percentile(model_discounts, 0.90):.2%}"
        )
        out.append(
            f"  pair  discount: median={_percentile(pair_discounts, 0.5):.2%} "
            f"p10={_percentile(pair_discounts, 0.10):.2%} p90={_percentile(pair_discounts, 0.90):.2%}"
        )
        out.append("")

    if with_cross_check:
        out.append(f"up to 15 examples (line: collection, model, backdrop, price, model_floor, pair_floor, disc_model, disc_pair):")
        for c in sorted(with_cross_check, key=lambda c: c["discount_model"], reverse=True)[:15]:
            r = c["row"]
            price = Decimal(r["new_price_nano"]) / config.NANO
            model_floor = None
            for s in model_signals:
                if s["row"]["listing_external_id"] == r["listing_external_id"] and s["row"]["observed_at"] == r["observed_at"]:
                    model_floor = s["floor"] / config.NANO
                    break
            pair_floor = Decimal(r["pair_floor_excl_self_nano"]) / config.NANO
            tag = "REMAINS" if c["would_survive"] else "DISAPPEARS"
            out.append(
                f"  {(r['collection_name'] or '')[:15]}, {(r['model_name'] or '')} + "
                f"{(r['backdrop_name'] or '')}: price={price:.2f} model_floor={model_floor:.2f} "
                f"pair_floor={pair_floor:.2f} disc_model={float(c['discount_model']):.1%} "
                f"disc_pair={float(c['discount_pair']):.1%} [{tag}]"
            )
    out.append("")

    fraction_disappeared = (len(disappeared) / len(with_cross_check)) if with_cross_check else None
    return "\n".join(out), {
        "with_cross_check": with_cross_check,
        "fraction_disappeared": fraction_disappeared,
    }


# --- Part 3: does reliability depend on backdrop spread ------------------


def _backdrop_spread_for_model(raw_model_block: dict) -> tuple[Decimal, Decimal] | None:
    """Returns (max/min, coefficient_of_variation) over the backdrop
    prices in raw_model_block ({"<backdrop>": "<price>"}), or None if
    there isn't enough data (empty block, or fewer than 2 positive
    prices) -- callers must skip such models, never crash on them.
    """
    prices = []
    for value in raw_model_block.values():
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if d > 0:
            prices.append(d)
    if len(prices) < 2:
        return None

    max_p = max(prices)
    min_p = min(prices)
    mean_p = sum(prices) / len(prices)
    variance = sum((p - mean_p) ** 2 for p in prices) / len(prices)
    stddev = variance.sqrt()
    cv = stddev / mean_p if mean_p != 0 else Decimal(0)
    return max_p / min_p, cv


def _model_spread_buckets(conn: sqlite3.Connection) -> dict[str, str]:
    """One representative (most-recently-fetched, non-empty raw_model_block)
    row per model_name -> its max/min spread bucket.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT model_name, raw_model_block, floor_fetched_at FROM floor_snapshots "
        "WHERE model_name IS NOT NULL AND model_name != '' "
        "ORDER BY floor_fetched_at DESC"
    ).fetchall()

    bucket_by_model: dict[str, str] = {}
    for r in rows:
        model_name = r["model_name"]
        if model_name in bucket_by_model:
            continue
        try:
            block = json.loads(r["raw_model_block"]) if r["raw_model_block"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if not block:
            continue
        spread = _backdrop_spread_for_model(block)
        if spread is None:
            continue
        max_over_min, _cv = spread
        bucket_by_model[model_name] = _spread_bucket(max_over_min)
    return bucket_by_model


def _part3_spread_dependency(conn: sqlite3.Connection, part2_data: dict) -> tuple[str, dict]:
    out = ["=== Part 3: does model-level reliability depend on backdrop spread ==="]

    bucket_by_model = _model_spread_buckets(conn)
    out.append(f"models with usable backdrop-price data (>= 2 backdrops in raw_model_block): {len(bucket_by_model)}")

    with_cross_check = part2_data["with_cross_check"]
    by_bucket: dict[str, list[bool]] = {b: [] for b in SPREAD_BUCKET_ORDER}
    for c in with_cross_check:
        model_name = c["row"]["model_name"]
        bucket = bucket_by_model.get(model_name)
        if bucket is None:
            continue
        by_bucket[bucket].append(not c["would_survive"])  # True = disappeared (not confirmed)

    out.append("")
    out.append(f"{'spread max/min':<16}{'n':>6}{'not confirmed by pair':>24}")
    fraction_by_bucket: dict[str, float | None] = {}
    for bucket in SPREAD_BUCKET_ORDER:
        flags = by_bucket[bucket]
        n = len(flags)
        frac = (sum(flags) / n) if n else None
        fraction_by_bucket[bucket] = frac
        frac_str = f"{frac:.1%}" if frac is not None else "n/a"
        out.append(f"{bucket:<16}{n:>6}{frac_str:>24}")
    out.append("")
    return "\n".join(out), {"fraction_by_bucket": fraction_by_bucket}


# --- Part 4: summary ------------------------------------------------------


def _part4_summary(median_ratio, fraction_disappeared, fraction_by_bucket: dict) -> str:
    out = ["=== Part 4: summary ==="]

    if median_ratio is None:
        out.append("median level disagreement: n/a (no rows with both levels filled)")
    else:
        out.append(f"median level disagreement (model/pair ratio): {median_ratio:.3f}")

    if fraction_disappeared is None:
        out.append("fraction of model-level signals not confirmed by pair floor: n/a (no cross-checkable signals)")
    else:
        out.append(f"fraction of model-level signals not confirmed by pair floor: {fraction_disappeared:.1%}")

    # Heuristic, NOT a measured statistical test -- see README: compares
    # the lowest-spread bucket with data against the highest-spread
    # bucket with data. A gap of >= 15 percentage points is treated as
    # "a dependency exists"; this threshold is itself a judgment call,
    # not a measured cutoff, and should be revisited once more data
    # accumulates.
    populated = [(b, f) for b, f in fraction_by_bucket.items() if f is not None]
    if len(populated) < 2:
        out.append("dependency on backdrop spread: n/a (not enough populated buckets to compare)")
    else:
        lowest_bucket, lowest_frac = populated[0]
        highest_bucket, highest_frac = populated[-1]
        gap = highest_frac - lowest_frac
        if gap >= 0.15:
            out.append(
                f"dependency on backdrop spread: YES -- {lowest_bucket} bucket "
                f"{lowest_frac:.1%} not-confirmed vs. {highest_bucket} bucket "
                f"{highest_frac:.1%} (threshold suggestion: trust model level "
                f"only below max/min spread {highest_bucket.split('-')[0].lstrip('>')})"
            )
        else:
            out.append(
                f"dependency on backdrop spread: NO -- {lowest_bucket} bucket "
                f"{lowest_frac:.1%} vs. {highest_bucket} bucket {highest_frac:.1%} "
                f"(gap under 15 points)"
            )

    return "\n".join(out)


def generate_audit(conn: sqlite3.Connection) -> str:
    db.verify_schema(conn)

    part1_text, median_ratio = _part1_direct_comparison(conn)
    part2_text, part2_data = _part2_signal_impact(conn)
    part3_text, part3_data = _part3_spread_dependency(conn, part2_data)
    part4_text = _part4_summary(median_ratio, part2_data["fraction_disappeared"], part3_data["fraction_by_bucket"])

    return "\n".join([part1_text, part2_text, part3_text, part4_text])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)  # runs migrations -- NOT a bare sqlite3.connect()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to run audit: {exc}", file=sys.stderr)
        return 1

    print(generate_audit(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
