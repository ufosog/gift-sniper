"""A signal anchored to CONFIRMED SALES instead of the floor.

Measured 2026-09-22 (diag/liquid_segment.py): the floor is what sellers
ask, not what buyers pay -- 2352 pairs produced 2939 sales in 9.5 days,
a median of ONE sale per pair, and only 27% of sales had any same-pair lot
on offer 24 h earlier. Anchoring to the floor therefore fires on lots
nobody buys.

This script measures the alternative rule, on history, before any of it
reaches the live system:

    BUY  when a lot's price is at least MARGIN below the median CONFIRMED
         sale price of the same pair over the previous LOOKBACK days
         (at least MIN_SALES sales, so the median means something)
    SELL into a later confirmed sale of that pair within HOLD hours

Both sides use only what was knowable at the time: the sales window ends
at the moment the candidate appears.

Every candidate lot is taken from `listings` + `price_history`, not from
the current cascade -- the point is to see how a different criterion would
behave, including on lots today's cascade never flags.

Confirmed sale prices exist on MRKT only (see db.record_sale), so the
anchor is MRKT's; the lot itself may be bought on any marketplace, at its
own buyer fee.

Read-only, no network, constant memory. Run on a COPY of the database.
Usage: python diag/sale_anchored.py --db /tmp/bt.db
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

# Candidate = a lot that appeared, or changed price, at some moment.
CANDIDATES_SQL = """
SELECT l.marketplace, l.external_id, l.collection_name, l.model_name, l.backdrop_name,
       lc.first_seen_at AS at, l.price_nano AS price
FROM listings l
JOIN listing_lifecycle lc ON lc.marketplace = l.marketplace AND lc.listing_external_id = l.external_id
WHERE l.collection_name IS NOT NULL AND l.model_name IS NOT NULL AND l.backdrop_name IS NOT NULL
  AND l.price_nano IS NOT NULL AND lc.first_seen_at IS NOT NULL
UNION ALL
SELECT p.marketplace, p.listing_external_id, l.collection_name, l.model_name, l.backdrop_name,
       p.observed_at AS at, p.new_price_nano AS price
FROM price_history p
JOIN listings l ON l.marketplace = p.marketplace AND l.external_id = p.listing_external_id
WHERE p.is_noise = 0 AND p.new_price_nano IS NOT NULL
  AND l.collection_name IS NOT NULL AND l.model_name IS NOT NULL AND l.backdrop_name IS NOT NULL
"""

SALES_BEFORE_SQL = """
SELECT lc.sold_price_nano FROM listing_lifecycle lc
JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
WHERE lc.marketplace = 'mrkt' AND lc.final_status = 'sold' AND lc.sold_price_nano IS NOT NULL
  AND l.collection_name IS :collection AND l.model_name IS :model AND l.backdrop_name IS :backdrop
  AND lc.disappeared_at > :since AND lc.disappeared_at <= :at
"""

SALES_AFTER_SQL = """
SELECT lc.sold_price_nano FROM listing_lifecycle lc
JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
WHERE lc.marketplace = 'mrkt' AND lc.final_status = 'sold' AND lc.sold_price_nano IS NOT NULL
  AND l.collection_name IS :collection AND l.model_name IS :model AND l.backdrop_name IS :backdrop
  AND lc.listing_external_id != :own_id
  AND lc.disappeared_at > :at AND lc.disappeared_at <= :until
"""


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sale-anchored signal, measured on history")
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--lookback-days", type=float, default=7.0)
    parser.add_argument("--min-sales", type=int, default=2, help="sales needed before the median counts")
    parser.add_argument("--margin-pct", type=Decimal, default=Decimal("15"), help="how far below the median")
    parser.add_argument("--hold-hours", type=float, default=48.0)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    lookback = timedelta(days=args.lookback_days)
    hold = timedelta(hours=args.hold_hours)
    margin = args.margin_pct / 100
    _fee_buy, fee_sell, _network = fees("mrkt")

    seen = 0
    with_history = 0
    signals = 0
    sold: list[tuple[float, float]] = []   # (pnl_ton, pnl_pct)
    unsold = 0
    by_marketplace: Counter = Counter()

    for marketplace, external_id, collection, model, backdrop, at_iso, price in conn.execute(CANDIDATES_SQL):
        seen += 1
        at = _dt(at_iso)
        prior = [r[0] for r in conn.execute(SALES_BEFORE_SQL, {
            "collection": collection, "model": model, "backdrop": backdrop,
            "since": (at - lookback).isoformat(), "at": at_iso})]
        if len(prior) < args.min_sales:
            continue
        with_history += 1
        anchor = Decimal(int(statistics.median(prior)))
        buy_cost = Decimal(price) * (1 + BUYER_FEE[marketplace])
        if buy_cost > anchor * (1 - margin):
            continue
        signals += 1
        by_marketplace[marketplace] += 1

        later = [r[0] for r in conn.execute(SALES_AFTER_SQL, {
            "collection": collection, "model": model, "backdrop": backdrop,
            "own_id": external_id, "at": at_iso, "until": (at + hold).isoformat()})]
        if not later:
            unsold += 1
            continue
        exit_nano = Decimal(int(statistics.median(later))) * (1 - fee_sell)
        pnl = exit_nano - buy_cost
        sold.append((float(pnl / NANO), float(pnl * 100 / buy_cost)))

    print(f"кандидатов (появление лота или смена цены): {seen}")
    print(f"  у пары есть {args.min_sales}+ подтверждённых продаж за {args.lookback_days:.0f} дн: {with_history} "
          f"({with_history * 100 // max(seen, 1)}%)")
    print(f"  и цена на {args.margin_pct}%+ ниже медианы этих продаж: {signals} "
          f"({signals * 100 // max(with_history, 1)}% от них)")
    print(f"  где: {dict(by_marketplace)}")
    if not signals:
        print("сигналов нет — критерий слишком строгий на этих данных")
        return 0

    print(f"\nиз {signals} сигналов продажа по паре случилась за {args.hold_hours:.0f} ч: "
          f"{len(sold)} ({len(sold) * 100 // signals}%), не случилась: {unsold}")
    if sold:
        pnls = [s[0] for s in sold]
        pcts = [s[1] for s in sold]
        wins = sum(1 for p in pnls if p > 0)
        print(f"  из проданных в плюсе {wins} ({wins * 100 // len(sold)}%), "
              f"медиана {statistics.median(pcts):.1f}%, сумма {sum(pnls):.0f} TON")
        # The honest bottom line: unsold lots are not free.
        print(f"  если непроданное слить по цене покупки минус 30%: "
              f"итог {sum(pnls) - unsold * 0.30 * statistics.median([abs(p) for p in pnls] or [0]):.0f} TON "
              f"(грубая оценка)")
    print("\nВАЖНО: подтверждённые сделки есть только на MRKT, поэтому и якорь, и выход — по MRKT.")
    print("ВАЖНО: покупателю достаётся самый дешёвый лот; здесь это не моделируется.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
