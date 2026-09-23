"""Offline, read-only diagnostic for is_noise correctness in
price_history. Confirmed live: over 4 hours, 48827 drops recorded, but
43072 (88%) came back is_noise=0 (significant) -- against a HISTORICAL
baseline of 482/11850 (4%) on the same PRICE_DROP_MIN_PCT=1.0 threshold.
Most real-world price changes are bot relister ticks of a few hundredths
of a percent (confirmed live: 0.04%, 0.16% steps); an 88% "significant"
rate is inconsistent with that and with the unchanged threshold.

Direct code-level review of poller.py's is_noise computation
(`abs(delta_pct) < config.PRICE_DROP_MIN_PCT`, called on every recorded
drop in `_process_known_items`) and db.py's `record_price_change`
(positional parameters match exactly) found NO bug: the formula, the
field, and the threshold source are all correct, and are exercised
byte-for-byte by test_price_history.py's
test_small_drop_below_threshold_is_flagged_noise (0.16% -> is_noise=1)
and test_real_drop_above_threshold_is_not_noise (2.92% -> is_noise=0),
both of which pass against the current code. This strongly suggests the
LIVE poller process that produced the measured 88% figure was running
OLDER code than what's in this repository (the same failure mode
flagged in an earlier delivery re: pair_floor.py sync) -- restarting the
deployed process onto the current code is the first thing to try. This
script exists so that can be confirmed (or ruled out) directly against
the real DB, without guessing.

Run: python -m gift_sniper.noise_diagnostic --db gift_sniper.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import config, db

BUCKET_ORDER = ["<0.1%", "0.1-0.5%", "0.5-1%", "1-3%", ">3%"]


def _bucket(abs_delta_pct: Decimal) -> str:
    if abs_delta_pct < Decimal("0.1"):
        return "<0.1%"
    if abs_delta_pct < Decimal("0.5"):
        return "0.1-0.5%"
    if abs_delta_pct < 1:
        return "0.5-1%"
    if abs_delta_pct < 3:
        return "1-3%"
    return ">3%"


def generate_diagnostic(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    db.verify_schema(conn)
    if now is None:
        now = datetime.now(timezone.utc)

    conn.row_factory = sqlite3.Row
    out: list[str] = []
    out.append("=== is_noise diagnostic ===")
    out.append(f"PRICE_DROP_MIN_PCT (config): {config.PRICE_DROP_MIN_PCT}")
    out.append("")

    # --- last hour: the exact sanity-check query from spec -----------
    hour_ago = now - timedelta(hours=1)
    hour_rows = conn.execute(
        "SELECT delta_pct, is_noise FROM price_history "
        "WHERE observed_at > ? AND new_price_nano < old_price_nano",
        (hour_ago.isoformat(),),
    ).fetchall()
    hour_total = len(hour_rows)
    hour_significant = sum(1 for r in hour_rows if not r["is_noise"])
    hour_pct = (hour_significant / hour_total * 100) if hour_total else 0.0
    out.append(f"last 1h: {hour_total} drops, {hour_significant} is_noise=0 (significant) -- {hour_pct:.1f}%")
    out.append(
        "  expect low single-digit percent; if this is anywhere near 88%, "
        "the live poller process is very likely running code OLDER than "
        "this repository's poller.py -- restart it onto the current code "
        "before assuming a code bug."
    )
    out.append("")

    # --- last 24h: delta_pct distribution among is_noise=0 rows -------
    day_ago = now - timedelta(hours=24)
    day_rows = conn.execute(
        "SELECT delta_pct FROM price_history "
        "WHERE observed_at > ? AND new_price_nano < old_price_nano AND is_noise = 0",
        (day_ago.isoformat(),),
    ).fetchall()
    dist = {b: 0 for b in BUCKET_ORDER}
    for r in day_rows:
        dist[_bucket(abs(Decimal(r["delta_pct"])))] += 1
    out.append(f"last 24h: {len(day_rows)} rows with is_noise=0 (significant), by |delta_pct|:")
    for b in BUCKET_ORDER:
        out.append(f"  {b:<10}{dist[b]:>8}")
    out.append(
        "  if <1% buckets dominate here, is_noise is being computed "
        "wrong (or read wrong) -- see the module docstring for what was "
        "already ruled out by code review."
    )

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)  # runs migrations -- NOT a bare sqlite3.connect()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to run diagnostic: {exc}", file=sys.stderr)
        return 1

    print(generate_diagnostic(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
