"""Offline analysis: how does an MRKT sale's confirmed price actually
compare to the pair floor at the moment of sale? Read-only, no network
calls, no schema changes -- pure measurement over listing_lifecycle's
floor_at_sale_* columns (schema v19, see db.py's _migration_18_to_19 /
mrkt_poller.py's _handle_sale_event).

WHY: the profit formula (signals.compute_profit_nano) assumes a listing
sells AT the pair floor. That has never been measured -- MRKT's "sale"
event is the only confirmed-sale signal in this project. Early samples
(sale price vs. average floor over all time, sale price vs. floor at
the moment of sale) both suggest sales happen BELOW the floor, but the
"floor at moment of sale" sample was only 6 pairs before this delivery
-- not enough to change anything on. This script is the measurement
tool that grows that sample; see README's "sale-vs-floor" section for
the running state of the question. It does NOT change the profit
formula -- per spec, that decision waits for a real sample size.

Run: python -m gift_sniper.sale_vs_floor --db gift_sniper.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from decimal import Decimal

from . import config, db
from .report import _percentile

# A ratio outside this range is treated as an outlier -- shown separately,
# not folded into the main distribution (per spec, "аномалия"). Chosen
# generously wide (a "normal" sale is expected somewhere well inside this)
# so a genuinely broken data point (e.g. a stale/wrong floor) stands out
# rather than quietly skewing the median.
OUTLIER_LOW = Decimal("0.2")
OUTLIER_HIGH = Decimal("1.5")

DEPTH_BUCKET_ORDER = ["1", "2-3", "4-9", "10+"]
PRICE_BUCKET_ORDER = ["<30", "30-100", ">100"]


def _depth_bucket(listed_count: int) -> str:
    if listed_count <= 1:
        return "1"
    if listed_count <= 3:
        return "2-3"
    if listed_count <= 9:
        return "4-9"
    return "10+"


def _price_bucket(price_ton: Decimal) -> str:
    if price_ton < 30:
        return "<30"
    if price_ton <= 100:
        return "30-100"
    return ">100"


def _fetch_sales_with_floor(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT listing_external_id, model_name, backdrop_name, sold_price_nano,
               floor_at_sale_nano, floor_listed_count_at_sale, floor_fetched_at_sale,
               disappeared_at
        FROM listing_lifecycle
        WHERE marketplace = 'mrkt' AND final_status = 'sold'
        ORDER BY disappeared_at
        """
    ).fetchall()


def _dist_block(title: str, ratios: list[Decimal]) -> list[str]:
    out = [title]
    if not ratios:
        out.append("  (нет данных)")
        return out
    floats = sorted(float(r) for r in ratios)
    out.append(
        f"  n={len(floats)}  median={_percentile(floats, 0.5):.3f}  "
        f"p10={_percentile(floats, 0.10):.3f}  p25={_percentile(floats, 0.25):.3f}  "
        f"p75={_percentile(floats, 0.75):.3f}  p90={_percentile(floats, 0.90):.3f}  "
        f"min={floats[0]:.3f}  max={floats[-1]:.3f}"
    )
    return out


def generate_report(conn: sqlite3.Connection) -> str:
    out: list[str] = []
    out.append("=== sale vs. floor (MRKT confirmed sales) ===")

    all_sold = conn.execute(
        "SELECT COUNT(*) FROM listing_lifecycle WHERE marketplace='mrkt' AND final_status='sold'"
    ).fetchone()[0]
    rows = _fetch_sales_with_floor(conn)
    with_floor = [r for r in rows if r["floor_at_sale_nano"] not in (None, 0) and r["sold_price_nano"] not in (None, 0)]

    out.append(f"total confirmed MRKT sales: {all_sold}")
    out.append(f"sales with floor_at_sale_nano: {len(with_floor)}")
    out.append("")

    pairs = []  # (ratio, row)
    for r in with_floor:
        ratio = Decimal(r["sold_price_nano"]) / Decimal(r["floor_at_sale_nano"])
        pairs.append((ratio, r))

    normal = [(ratio, r) for ratio, r in pairs if OUTLIER_LOW <= ratio <= OUTLIER_HIGH]
    outliers = [(ratio, r) for ratio, r in pairs if not (OUTLIER_LOW <= ratio <= OUTLIER_HIGH)]

    out += _dist_block(
        f"distribution of sold_price/floor_at_sale (excluding outliers outside [{OUTLIER_LOW}, {OUTLIER_HIGH}]):",
        [ratio for ratio, _r in normal],
    )
    out.append("")

    out.append(f"outliers (ratio outside [{OUTLIER_LOW}, {OUTLIER_HIGH}]): {len(outliers)}")
    for ratio, r in sorted(outliers, key=lambda t: t[0])[:10]:
        sold = Decimal(r["sold_price_nano"]) / config.NANO
        floor = Decimal(r["floor_at_sale_nano"]) / config.NANO
        out.append(
            f"  {(r['model_name'] or '?')} / {(r['backdrop_name'] or '?')}: "
            f"sold {sold:.2f}, floor {floor:.2f}, ratio {ratio:.3f}"
        )
    out.append("")

    # --- by book depth (floor_listed_count_at_sale) -----------------------
    out.append("by floor depth at sale (floor_listed_count_at_sale):")
    by_depth: dict[str, list[Decimal]] = {b: [] for b in DEPTH_BUCKET_ORDER}
    for ratio, r in normal:
        count = r["floor_listed_count_at_sale"]
        if count is None:
            continue
        by_depth[_depth_bucket(count)].append(ratio)
    for bucket in DEPTH_BUCKET_ORDER:
        vals = by_depth[bucket]
        if not vals:
            out.append(f"  {bucket}: n=0")
            continue
        floats = sorted(float(v) for v in vals)
        out.append(f"  {bucket}: n={len(floats)}  median={_percentile(floats, 0.5):.3f}")
    out.append("")

    # --- by price segment ---------------------------------------------------
    out.append("by sale price segment (TON):")
    by_price: dict[str, list[Decimal]] = {b: [] for b in PRICE_BUCKET_ORDER}
    for ratio, r in normal:
        price_ton = Decimal(r["sold_price_nano"]) / config.NANO
        by_price[_price_bucket(price_ton)].append(ratio)
    for bucket in PRICE_BUCKET_ORDER:
        vals = by_price[bucket]
        if not vals:
            out.append(f"  {bucket}: n=0")
            continue
        floats = sorted(float(v) for v in vals)
        out.append(f"  {bucket}: n={len(floats)}  median={_percentile(floats, 0.5):.3f}")
    out.append("")

    # --- examples -----------------------------------------------------------
    out.append(f"examples (up to 15, sorted by sale time):")
    for ratio, r in pairs[:15]:
        sold = Decimal(r["sold_price_nano"]) / config.NANO
        floor = Decimal(r["floor_at_sale_nano"]) / config.NANO
        out.append(
            f"  {r['disappeared_at']}  {(r['model_name'] or '?')} / {(r['backdrop_name'] or '?')}: "
            f"sold {sold:.2f} TON, floor {floor:.2f} TON (depth={r['floor_listed_count_at_sale']}), "
            f"ratio {ratio:.3f}"
        )
    if not pairs:
        out.append("  (нет данных)")

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)  # runs migrations -- NOT a bare sqlite3.connect()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to run: {exc}", file=sys.stderr)
        return 1

    print(generate_report(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
