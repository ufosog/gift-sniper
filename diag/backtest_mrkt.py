"""What would past MRKT signals have earned? Measured on history already
in the database, so it needs no waiting.

Why MRKT only: it is the one marketplace with a confirmed `sale` event
carrying a price (`listing_lifecycle.sold_price_nano`). Portals and Tonnel
only ever show a bare disappearance, so an exit cannot be priced there.

Method, per clean signal (the SAME cascade the pollers use):
  entry = the signal's own price (what we would have paid)
  exit  = the MEDIAN confirmed sale price of OTHER lots of the same pair
          (collection + model + backdrop) inside the hold window after the
          signal. A real buyer paid that for the same thing, in that
          window -- no floor assumption anywhere.
  pnl   = exit * (1 - sell fee) - entry   (MRKT's 2% fee is already inside
          both prices, see paper_journal.fees)

The lot's OWN later sale price is deliberately not used as the exit: that
is simply our entry price again (measured 2026-09-21: it gave a median of
exactly -2.00%, i.e. the sell fee), which says the lot was liquid, not
what a flip would earn.

LIMITS, on purpose:
- The cross-check filter cannot be replayed (it needed live neighbour
  prices at that moment), so these are signals BEFORE cross-check. The
  live system sends fewer.
- Our own sale would compete with the lots measured here and could need a
  lower price, so the result is, if anything, optimistic.
- Sales the collector never saw (below MRKT_COLLECT_MIN_PRICE) are absent.

Read-only. Usage: python diag/backtest_mrkt.py --db <copy.db> [--hold-hours 24]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config
from gift_sniper.paper_journal import fees
from gift_sniper.signals import clean_signals

NANO = config.NANO


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def pair_sales(conn, signal, hold: timedelta) -> list[int]:
    """Confirmed sale prices of OTHER lots of the same pair inside the
    window after the signal."""
    rows = conn.execute(
        """
        SELECT lc.sold_price_nano
        FROM listing_lifecycle lc
        JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
        WHERE lc.marketplace = 'mrkt' AND lc.final_status = 'sold'
          AND lc.sold_price_nano IS NOT NULL
          AND lc.listing_external_id != ?
          AND l.collection_name IS ? AND l.model_name IS ? AND l.backdrop_name IS ?
          AND lc.disappeared_at > ? AND lc.disappeared_at <= ?
        """,
        (signal.listing_external_id, signal.collection_name, signal.model_name,
         signal.backdrop_name, signal.observed_at.isoformat(),
         (signal.observed_at + hold).isoformat())).fetchall()
    return [int(r[0]) for r in rows]


def own_outcome(conn, signal, hold: timedelta) -> str:
    row = conn.execute(
        "SELECT disappeared_at, final_status FROM listing_lifecycle "
        "WHERE marketplace = 'mrkt' AND listing_external_id = ?",
        (signal.listing_external_id,)).fetchone()
    if row is None or row["disappeared_at"] is None:
        return "still_listed"
    gone_at = _dt(row["disappeared_at"])
    if gone_at <= signal.observed_at:
        return "gone_before_signal"
    if gone_at - signal.observed_at > hold:
        return "outlived_hold"
    return row["final_status"] or "gone_unknown"


def report(name: str, subset: list[tuple]) -> None:
    if not subset:
        return
    pnls = [t[0] for t in subset]
    pcts = [t[1] for t in subset]
    wins = sum(1 for p in pnls if p > 0)
    print(f"  {name:22} n={len(subset):4} | в плюсе {wins:3} ({wins * 100 // len(subset):3}%) | "
          f"медиана {statistics.median(pcts):8.2f}% | сумма {sum(pnls):9.2f} TON | "
          f"среднее {statistics.mean(pcts):8.2f}%")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--hold-hours", type=float, default=24.0)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    hold = timedelta(hours=args.hold_hours)
    conn = sqlite3.connect(args.db)  # clean_signals writes is_ladder: use a COPY
    conn.row_factory = sqlite3.Row
    signals = clean_signals(conn, marketplace="mrkt")
    print(f"чистых сигналов MRKT за всю историю: {len(signals)} (до межбиржевой сверки)")

    _fee_buy, fee_sell, _network = fees("mrkt")
    by_outcome: dict[str, int] = defaultdict(int)
    no_comparable = 0
    trades: list[tuple] = []  # (pnl_ton, pnl_pct, level, depth, ratio)
    for s in signals:
        by_outcome[own_outcome(conn, s, hold)] += 1
        sales = pair_sales(conn, s, hold)
        if not sales:
            no_comparable += 1
            continue
        entry = Decimal(s.new_price_nano)
        exit_nano = Decimal(int(statistics.median(sales)))
        pnl = exit_nano * (1 - fee_sell) - entry
        trades.append((float(pnl / NANO), float(pnl * 100 / entry), s.floor_level,
                       s.listed_count or 0, float(s.ratio) if s.ratio is not None else None))

    print("\nчто стало с САМИМ лотом после сигнала:")
    for name, n in sorted(by_outcome.items(), key=lambda kv: -kv[1]):
        print(f"  {name:20} {n:5}  ({n * 100 // max(len(signals), 1):3}%)")
    print(f"\nсигналов без единой подтверждённой продажи такой же пары в окне: {no_comparable}")

    if not trades:
        print("измеримых выходов нет — вывод невозможен")
        return 0

    print(f"\nПЕРЕПРОДАЖА по реальной цене сделок той же пары внутри {args.hold_hours:.0f} ч:")
    report("все", trades)
    for level in ("pair", "model"):
        report(f"уровень {level}", [t for t in trades if t[2] == level])
    for label, lo, hi in (("глубина 2-3", 2, 3), ("глубина 4-9", 4, 9), ("глубина 10+", 10, 10**6)):
        report(label, [t for t in trades if lo <= t[3] <= hi])
    for label, lo, hi in (("ratio < 1.2", 0, 1.2), ("ratio 1.2-1.5", 1.2, 1.5), ("ratio > 1.5", 1.5, 10**6)):
        report(label, [t for t in trades if t[4] is not None and lo <= t[4] < hi])
    print(f"\nсигналов с измеримым выходом: {len(trades)} из {len(signals)}")
    print("ВАЖНО: это сигналы ДО межбиржевой сверки; живая система отправляет меньше.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
