"""Is there a segment where trading actually happens, and could we have
earned in it? Measured backwards, from real trades.

Every earlier measurement started from a signal and then looked for a
buyer -- and the buyer was missing 93-95% of the time. This one starts
from a CONFIRMED sale (so a buyer provably existed, at a known price) and
asks the opposite question:

    could we have bought the same pair cheaper shortly before that sale?

For every confirmed MRKT sale of pair P at price S and time T:
  - look at every lot of P listed anywhere in the window [T - lookback, T);
  - its price at that moment is reconstructed exactly as in
    diag/backtest_book.py (last change at or before the moment, else the
    price before the first change, else the current price);
  - buying cost uses the lot's own marketplace fee (Tonnel x 1.1);
  - selling at S on MRKT pays MRKT's 2% (paper_journal.fees), so
    pnl = S * (1 - 0.02) - buy_cost.

This says nothing about whether WE would have been the seller that buyer
picked -- our lot would have to be the cheapest one. That is why the
report also counts how many competing lots were cheaper than ours.

Section 2 measures the segment itself: how sales are spread across pairs,
and how much of the volume sits in pairs that trade repeatedly.

Read-only, no network, constant memory. Run on a COPY of the database.
Usage: python diag/liquid_segment.py --db /tmp/bt.db [--lookback-hours 24]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config
from gift_sniper.paper_journal import TONNEL_BUYER_FEE_RATE, fees

NANO = config.NANO
BUYER_FEE = {"portals": Decimal(0), "mrkt": Decimal(0), "tonnel": TONNEL_BUYER_FEE_RATE}

SALES_SQL = """
SELECT l.collection_name, l.model_name, l.backdrop_name,
       lc.listing_external_id, lc.sold_price_nano, lc.disappeared_at
FROM listing_lifecycle lc
JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
WHERE lc.marketplace = 'mrkt' AND lc.final_status = 'sold'
  AND lc.sold_price_nano IS NOT NULL AND lc.disappeared_at IS NOT NULL
  AND l.collection_name IS NOT NULL AND l.model_name IS NOT NULL AND l.backdrop_name IS NOT NULL
ORDER BY lc.disappeared_at
"""

# Lots of the pair on sale at :at, with their price at that moment.
OFFERS_SQL = """
SELECT l.marketplace, l.external_id,
       COALESCE(
           (SELECT p.new_price_nano FROM price_history p
             WHERE p.marketplace = l.marketplace AND p.listing_external_id = l.external_id
               AND p.observed_at <= :at ORDER BY p.observed_at DESC LIMIT 1),
           (SELECT p2.old_price_nano FROM price_history p2
             WHERE p2.marketplace = l.marketplace AND p2.listing_external_id = l.external_id
             ORDER BY p2.observed_at ASC LIMIT 1),
           l.price_nano) AS price_at
FROM listings l
JOIN listing_lifecycle lc
  ON lc.marketplace = l.marketplace AND lc.listing_external_id = l.external_id
WHERE l.collection_name IS :collection AND l.model_name IS :model AND l.backdrop_name IS :backdrop
  AND l.external_id != :sold_id
  AND lc.first_seen_at <= :at
  AND (lc.disappeared_at IS NULL OR lc.disappeared_at > :at)
"""


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def segment_stats(sales: list) -> None:
    """How concentrated is the trading? A pair that trades once in two
    weeks cannot be flipped, however cheap it looks."""
    per_pair = Counter((s[0], s[1], s[2]) for s in sales)
    if not sales:
        print("подтверждённых сделок нет")
        return
    first, last = _dt(sales[0][5]), _dt(sales[-1][5])
    days = max((last - first).total_seconds() / 86400, 1)
    print(f"подтверждённых сделок MRKT: {len(sales)} за {days:.1f} дней, пар: {len(per_pair)}")
    counts = sorted(per_pair.values(), reverse=True)
    print(f"  сделок на пару: медиана {statistics.median(counts):.0f}, максимум {counts[0]}")
    for threshold in (1, 2, 3, 5, 10):
        pairs = [c for c in counts if c >= threshold]
        share_sales = sum(pairs) * 100 // len(sales)
        print(f"  пар с {threshold}+ сделками: {len(pairs):5} ({len(pairs) * 100 // len(per_pair):3}% пар) "
              f"— на них {share_sales:3}% всех сделок")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Is there a liquid segment worth trading?")
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--lookback-hours", type=float, default=24.0,
                        help="how long before the sale we could have bought")
    parser.add_argument("--min-margin-pct", type=Decimal, default=Decimal("10"),
                        help="a buy counts only if it beats the sale price by this much")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)  # no cascade here: read-only is safe
    sales = conn.execute(SALES_SQL).fetchall()

    print("=== 1. НАСКОЛЬКО ВООБЩЕ ИДЁТ ТОРГОВЛЯ ===")
    segment_stats(sales)

    _fee_buy, fee_sell, _network = fees("mrkt")
    lookback = timedelta(hours=args.lookback_hours)
    margin = args.min_margin_pct / 100

    print(f"\n=== 2. МОЖНО ЛИ БЫЛО КУПИТЬ ДЕШЕВЛЕ ЗА {args.lookback_hours:.0f} ч ДО СДЕЛКИ ===")
    reachable = 0          # sales where any lot of the pair was on sale before
    profitable = 0         # ... and cheap enough to clear the margin
    pnls: list[float] = []
    pcts: list[float] = []
    cheaper_rivals: list[int] = []
    by_marketplace: Counter = Counter()
    for collection, model, backdrop, sold_id, sold_nano, sold_at in sales:
        at = _dt(sold_at) - lookback
        offers = conn.execute(OFFERS_SQL, {
            "at": at.isoformat(), "collection": collection, "model": model,
            "backdrop": backdrop, "sold_id": sold_id}).fetchall()
        costs = []
        for marketplace, _external_id, price in offers:
            if price:
                costs.append((int(Decimal(price) * (1 + BUYER_FEE[marketplace])), marketplace))
        if not costs:
            continue
        reachable += 1
        costs.sort()
        buy_cost, marketplace = costs[0]
        exit_nano = Decimal(sold_nano) * (1 - fee_sell)
        pnl = exit_nano - buy_cost
        if pnl <= 0 or pnl * 100 / buy_cost < args.min_margin_pct:
            continue
        profitable += 1
        by_marketplace[marketplace] += 1
        pnls.append(float(pnl / NANO))
        pcts.append(float(pnl * 100 / buy_cost))
        cheaper_rivals.append(sum(1 for c, _m in costs[1:] if c < buy_cost * (1 + margin)))

    print(f"сделок всего: {len(sales)}")
    print(f"  из них лот той же пары был в продаже за {args.lookback_hours:.0f} ч до: {reachable} "
          f"({reachable * 100 // max(len(sales), 1)}%)")
    print(f"  из них дешевле цены сделки минимум на {args.min_margin_pct}%: {profitable} "
          f"({profitable * 100 // max(len(sales), 1)}% всех сделок)")
    if pnls:
        print(f"\n  прибыль на такой сделке: медиана {statistics.median(pcts):.1f}% "
              f"({statistics.median(pnls):.2f} TON), сумма {sum(pnls):.0f} TON на {len(pnls)} сделках")
        print(f"  где покупать: {dict(by_marketplace)}")
        print(f"  конкурентов в пределах {args.min_margin_pct}% от нашей цены: "
              f"медиана {statistics.median(cheaper_rivals):.0f} "
              f"(если их много, покупателя заберут они, а не мы)")
    print("\nВАЖНО: это верхняя граница. Покупателю достаётся САМЫЙ дешёвый лот,")
    print("поэтому реальная доля достанется нам только там, где конкурентов нет.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
