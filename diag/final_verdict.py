"""The last cheap chance to disprove "gift flipping does not work".

Everything measured so far covered the cheap segment and a 1-2 day
horizon. Two gaps were left open on purpose, and both can be closed from
data already collected:

  1. THE EXPENSIVE SEGMENT. The journal capped a position at 60 TON and
     collection starts at 15 TON, so the conclusion rests on cheap lots.
     Maybe rare, expensive gifts trade differently: fewer sales, wider
     spreads.
  2. THE LONGER HORIZON. Everything was judged at 24-48 h. Confirmed MRKT
     sales span ~2.5 weeks, enough to ask whether waiting longer helps.

For each price bucket this reports: how many confirmed sales happened,
how many pairs they are spread over, how often a pair sells twice, and --
the decisive one -- how a lot bought at the moment of a sale would have
fared if sold into the NEXT confirmed sale of the same pair, whenever that
came.

Read-only, no network. Run on a copy: python diag/final_verdict.py --db /tmp/bt.db
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config
from gift_sniper.paper_journal import fees

NANO = Decimal(10) ** 9
BUCKETS = ((0, 30), (30, 100), (100, 300), (300, 10 ** 9))

SALES_SQL = """
SELECT l.collection_name, l.model_name, l.backdrop_name,
       lc.sold_price_nano, lc.disappeared_at
FROM listing_lifecycle lc
JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
WHERE lc.marketplace = 'mrkt' AND lc.final_status = 'sold'
  AND lc.sold_price_nano IS NOT NULL AND lc.disappeared_at IS NOT NULL
  AND l.collection_name IS NOT NULL AND l.model_name IS NOT NULL AND l.backdrop_name IS NOT NULL
ORDER BY lc.disappeared_at
"""


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def bucket_of(price_ton: Decimal) -> tuple[int, int]:
    for low, high in BUCKETS:
        if low <= price_ton < high:
            return (low, high)
    return BUCKETS[-1]


def label(bucket: tuple[int, int]) -> str:
    low, high = bucket
    return f"{low}-{high} TON" if high < 10 ** 9 else f"{low}+ TON"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Expensive segment and longer horizon")
    parser.add_argument("--db", default=config.DB_DSN)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute(SALES_SQL).fetchall()
    _fee_buy, fee_sell, _network = fees("mrkt")

    per_pair: dict[tuple, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for collection, model, backdrop, price_nano, sold_at in rows:
        per_pair[(collection, model, backdrop)].append((_dt(sold_at), Decimal(price_nano) / NANO))

    print(f"подтверждённых сделок: {len(rows)}, пар: {len(per_pair)}")
    print("\n=== 1. ДОРОГОЙ СЕГМЕНТ ПРОТИВ ДЕШЁВОГО ===")
    print(f"{'сегмент':>12} | {'сделок':>7} | {'пар':>6} | {'пар с 2+':>9} | {'повтор через':>13}")

    gains_by_bucket: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for bucket in BUCKETS:
        sales_here = 0
        pairs_here = set()
        pairs_repeat = 0
        waits: list[float] = []
        for pair, sales in per_pair.items():
            sales.sort()
            in_bucket = [s for s in sales if bucket_of(s[1]) == bucket]
            if not in_bucket:
                continue
            sales_here += len(in_bucket)
            pairs_here.add(pair)
            if len(sales) >= 2:
                pairs_repeat += 1
            # buy at one sale's price, sell into the NEXT sale of the pair
            for (t1, p1), (t2, p2) in zip(sales, sales[1:]):
                if bucket_of(p1) != bucket:
                    continue
                waits.append((t2 - t1).total_seconds() / 3600)
                pnl = p2 * (1 - fee_sell) - p1
                gains_by_bucket[bucket].append((float(pnl), float(pnl * 100 / p1)))
        wait_text = f"{statistics.median(waits):.0f} ч" if waits else "—"
        print(f"{label(bucket):>12} | {sales_here:7} | {len(pairs_here):6} | {pairs_repeat:9} | {wait_text:>13}")

    print("\n=== 2. ПОКУПКА ПО ЦЕНЕ СДЕЛКИ И ПРОДАЖА В СЛЕДУЮЩУЮ, СКОЛЬКО БЫ НИ ЖДАТЬ ===")
    all_gains: list[tuple[float, float]] = []
    for bucket in BUCKETS:
        gains = gains_by_bucket[bucket]
        all_gains += gains
        if not gains:
            print(f"{label(bucket):>12} | повторных сделок нет")
            continue
        pcts = [g[1] for g in gains]
        wins = sum(1 for _p, pct in gains if pct > 0)
        print(f"{label(bucket):>12} | n={len(gains):4} | в плюсе {wins:3} ({wins * 100 // len(gains):3}%) | "
              f"медиана {statistics.median(pcts):7.1f}% | сумма {sum(g[0] for g in gains):9.1f} TON")
    if all_gains:
        pcts = [g[1] for g in all_gains]
        wins = sum(1 for _p, pct in all_gains if pct > 0)
        print(f"{'ИТОГО':>12} | n={len(all_gains):4} | в плюсе {wins:3} ({wins * 100 // len(all_gains):3}%) | "
              f"медиана {statistics.median(pcts):7.1f}% | сумма {sum(g[0] for g in all_gains):9.1f} TON")
    print("\nЭто ВЕРХНЯЯ граница: покупка ровно по цене реальной сделки, продажа")
    print("ровно в следующую реальную сделку, без конкуренции и без ожидания впустую.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
