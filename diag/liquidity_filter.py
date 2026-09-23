"""Does past liquidity of a pair predict whether a signal can be resold?

For every past MRKT clean signal: was there a confirmed sale of the SAME
pair in the LOOKBACK days BEFORE the signal (information we would have had
at signal time), and did a sale of that pair happen AFTER it, inside the
hold window (what we needed to exit)?

Read-only, no network. Usage: python diag/liquidity_filter.py --db <copy.db>
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from datetime import timedelta
from decimal import Decimal

from gift_sniper import config
from gift_sniper.paper_journal import fees
from gift_sniper.signals import clean_signals

SQL = """
SELECT lc.sold_price_nano
FROM listing_lifecycle lc
JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
WHERE lc.marketplace = 'mrkt' AND lc.final_status = 'sold' AND lc.sold_price_nano IS NOT NULL
  AND l.collection_name IS ? AND l.model_name IS ? AND l.backdrop_name IS ?
  AND lc.disappeared_at > ? AND lc.disappeared_at <= ?
  -- OUR OWN lot's sale is not an exit: it would sell at our own entry
  -- price, which measures liquidity of the lot, not a flip.
  AND lc.listing_external_id != ?
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--hold-hours", type=float, default=48.0)
    parser.add_argument("--lookback-days", type=float, default=7.0)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    signals = clean_signals(conn, marketplace="mrkt")
    hold = timedelta(hours=args.hold_hours)
    look = timedelta(days=args.lookback_days)
    _b, fee_sell, _n = fees("mrkt")

    groups = {True: [], False: []}  # liquid before -> list of (sellable, pnl_pct)
    for s in signals:
        key = (s.collection_name, s.model_name, s.backdrop_name)
        before = conn.execute(SQL, (*key, (s.observed_at - look).isoformat(),
                                    s.observed_at.isoformat(), s.listing_external_id)).fetchall()
        after = conn.execute(SQL, (*key, s.observed_at.isoformat(),
                                   (s.observed_at + hold).isoformat(), s.listing_external_id)).fetchall()
        pnl_pct = None
        if after:
            exit_nano = Decimal(int(statistics.median([int(r[0]) for r in after])))
            entry = Decimal(s.new_price_nano)
            pnl_pct = float((exit_nano * (1 - fee_sell) - entry) * 100 / entry)
        groups[bool(before)].append((bool(after), pnl_pct))

    for liquid, rows in ((True, groups[True]), (False, groups[False])):
        label = "пара уже продавалась" if liquid else "продаж пары не было"
        if not rows:
            print(f"{label}: сигналов 0")
            continue
        sellable = [p for ok, p in rows if ok]
        print(f"{label:24} сигналов {len(rows):4} | нашёлся покупатель {len(sellable):3} "
              f"({len(sellable) * 100 // len(rows):3}%)", end="")
        if sellable:
            wins = sum(1 for p in sellable if p > 0)
            print(f" | медиана {statistics.median(sellable):7.2f}% | в плюсе {wins}/{len(sellable)}")
        else:
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
