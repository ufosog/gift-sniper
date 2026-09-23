"""What would past signals have earned? Answered from collected history,
with no waiting and no network, for ALL THREE marketplaces.

The exit price is the problem: only MRKT confirms a sale price, so for
Portals and Tonnel the exit has to be reconstructed. This script rebuilds
the ORDER BOOK of a pair (collection + model + backdrop) at a past moment
from data already in gift_sniper.db:

  visible at T : listing_lifecycle.first_seen_at <= T
                 AND (disappeared_at IS NULL OR disappeared_at > T)
  price at T   : new_price_nano of the last price_history row <= T;
                 else old_price_nano of the FIRST row (the price before any
                 change); else listings.price_nano (never changed)

`own_floors.own_combo_floor` cannot be used: it filters by first_seen_at
but reads the CURRENT price and status, so it values a lot that has since
been repriced or withdrawn with today's numbers.

Exit rule (the strict one the owner chose): at `observed_at + hold` take
the cheapest competing lot of the same pair across ALL marketplaces, at
buyer cost (Tonnel x 1.1, Portals and MRKT as-is -- the same conversion
paper_journal._cross_market_cap does), convert back to our marketplace's
listing basis, and undercut it by UNDERCUT_PCT. No competitor at all means
the lot did not sell: it is reported separately and never counted as
profit.

UNDERCUT_PCT is an ASSUMPTION (default 1%), not a measurement -- run
--undercut-pct 0 / 1 / 3 and check the conclusion holds for all three.

Step 0 of every run checks the reconstruction itself against MRKT's
confirmed sales: if the rebuilt cheapest competitor at the moment of a real
sale does not land near the real sale price, the whole method is unusable
and the run says so instead of printing numbers.

Read-only in effect, but `clean_signals` writes `is_ladder`, so run it on a
COPY of the database, never on the live file.

Usage: python diag/backtest_book.py --db /tmp/bt.db [--hold-hours 24]
"""
from __future__ import annotations

import argparse
import statistics
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config
from gift_sniper.paper_journal import TONNEL_BUYER_FEE_RATE, fees, pnl_nano
from gift_sniper.signals import clean_signals

NANO = config.NANO
MARKETPLACES = ("portals", "tonnel", "mrkt")
# Buyer cost of a listed price, per marketplace (see paper_journal.fees):
# Tonnel adds 10% on top, MRKT already includes its 2%, Portals has none.
BUYER_FEE = {"portals": Decimal(0), "mrkt": Decimal(0), "tonnel": TONNEL_BUYER_FEE_RATE}
VALIDATION_MAX_DEVIATION = Decimal("0.10")  # 10%: above this the method is rejected


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- book reconstruction --------------------------------------------------

COMPETITORS_SQL = """
SELECT l.marketplace,
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
  AND NOT (l.marketplace = :own_marketplace AND l.external_id = :own_id)
  AND lc.first_seen_at <= :at
  AND (lc.disappeared_at IS NULL OR lc.disappeared_at > :at)
"""


class Book:
    """Cheapest competing lot of a pair at a past moment.

    The whole computation runs INSIDE SQLite, one query per signal, so the
    memory used does not grow with history. The first version built the
    book in Python and cached it: 1.36 GB on a 2 GB server, and a 500 MB
    cap killed it outright on Portals (measured 2026-09-22).

    price at T = last change at or before T; else the price before the
    first change; else the lot's current price (it never changed).
    """

    def __init__(self, conn: sqlite3.Connection, cache_size: int = 0):
        self._conn = conn  # cache_size kept for the CLI, no cache is needed now

    def cheapest_competitor(self, signal, at: datetime) -> tuple[int, str] | None:
        """(buyer cost in nano, marketplace), or None when the pair has no
        other lot on sale at that moment."""
        rows = self._conn.execute(COMPETITORS_SQL, {
            "at": at.isoformat(),
            "collection": signal.collection_name,
            "model": signal.model_name,
            "backdrop": signal.backdrop_name,
            "own_marketplace": signal.marketplace,
            "own_id": signal.listing_external_id,
        }).fetchall()
        best: tuple[int, str] | None = None
        for marketplace, price in rows:
            if not price:
                continue
            cost = int(Decimal(price) * (1 + BUYER_FEE[marketplace]))
            if best is None or cost < best[0]:
                best = (cost, marketplace)
        return best


def pair_had_a_real_sale(conn, signal, hold: timedelta) -> bool:
    """Did ANY lot of this pair actually change hands inside the window?
    Confirmed sales exist on MRKT only, so this is a lower bound for the
    other marketplaces. Without it the backtest silently assumes our lot
    always finds a buyer at the price we list -- measured 2026-09-21: only
    7% of MRKT signals had a confirmed sale of the pair within 48 h."""
    row = conn.execute(
        """
        SELECT 1 FROM listing_lifecycle lc
        JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
        WHERE lc.final_status = 'sold' AND lc.sold_price_nano IS NOT NULL
          AND lc.listing_external_id != ?
          AND l.collection_name IS ? AND l.model_name IS ? AND l.backdrop_name IS ?
          AND lc.disappeared_at > ? AND lc.disappeared_at <= ?
        LIMIT 1
        """,
        (signal.listing_external_id, signal.collection_name, signal.model_name, signal.backdrop_name,
         signal.observed_at.isoformat(), (signal.observed_at + hold).isoformat())).fetchone()
    return row is not None


def exit_price_nano(signal, competitor_cost: int, undercut: Decimal) -> int:
    """Competitor's buyer cost -> what WE would list at on our own
    marketplace, undercutting by `undercut`."""
    own_basis = Decimal(competitor_cost) / (1 + BUYER_FEE[signal.marketplace])
    return int(own_basis * (1 - undercut))


# --- step 0: is the reconstruction trustworthy? ---------------------------

def validate(conn: sqlite3.Connection, book: Book, limit: int = 400) -> tuple[bool, str]:
    """Compare the rebuilt cheapest competitor against MRKT's CONFIRMED sale
    prices at the same moment. A sale happens at the cheapest offer, so the
    ratio sale/rebuilt should sit near 1.0."""
    rows = conn.execute(
        """
        SELECT l.collection_name, l.model_name, l.backdrop_name, l.external_id,
               lc.sold_price_nano, lc.disappeared_at
        FROM listing_lifecycle lc
        JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
        WHERE lc.marketplace = 'mrkt' AND lc.final_status = 'sold'
          AND lc.sold_price_nano IS NOT NULL AND lc.disappeared_at IS NOT NULL
          AND l.collection_name IS NOT NULL AND l.model_name IS NOT NULL AND l.backdrop_name IS NOT NULL
        ORDER BY lc.disappeared_at DESC LIMIT ?
        """, (limit,)).fetchall()

    class _S:  # the shape cheapest_competitor needs
        pass

    ratios = []
    for collection, model, backdrop, external_id, sold_nano, sold_at in rows:
        s = _S()
        s.collection_name, s.model_name, s.backdrop_name = collection, model, backdrop
        s.marketplace, s.listing_external_id = "mrkt", external_id
        # A moment BEFORE the sale: at the sale itself the lot is already gone.
        best = book.cheapest_competitor(s, _dt(sold_at) - timedelta(minutes=5))
        if best is None:
            continue
        ratios.append(float(Decimal(sold_nano) / Decimal(best[0])))
    if len(ratios) < 30:
        return False, f"проверка невозможна: сопоставимых продаж всего {len(ratios)} (нужно 30+)"
    median = Decimal(str(statistics.median(ratios))).quantize(Decimal("0.001"))
    ok = abs(median - 1) <= VALIDATION_MAX_DEVIATION
    text = (f"проверка восстановления: n={len(ratios)} подтверждённых продаж MRKT, "
            f"медиана факт/восстановлено = {median} "
            f"(p25 {statistics.quantiles(ratios, n=4)[0]:.3f}, p75 {statistics.quantiles(ratios, n=4)[2]:.3f})")
    return ok, text


# --- signals --------------------------------------------------------------

def stream_signals(conn: sqlite3.Connection, marketplace: str, window_hours: int):
    """Yields signals window by window. Never builds a list of the whole
    history: an unbounded cascade load alone takes 1.7 GB."""
    bounds = conn.execute(
        "SELECT MIN(observed_at), MAX(observed_at) FROM price_history WHERE marketplace = ?",
        (marketplace,)).fetchone()
    if not bounds or not bounds[0]:
        return
    start, end = _dt(bounds[0]), _dt(bounds[1])
    step = timedelta(hours=window_hours)
    seen: set = set()
    cursor = start
    while cursor <= end:
        window_end = min(cursor + step, end + timedelta(seconds=1))
        for s in clean_signals(conn, since=cursor, now=window_end, marketplace=marketplace):
            key = (s.listing_external_id, s.observed_at.isoformat())
            if key not in seen:
                seen.add(key)
                yield s
        cursor = window_end


# --- report ---------------------------------------------------------------

def report(name: str, subset: list) -> None:
    if not subset:
        return
    pnls = [t[0] for t in subset]
    pcts = [t[1] for t in subset]
    wins = sum(1 for p in pnls if p > 0)
    print(f"  {name:24} n={len(subset):5} | в плюсе {wins:4} ({wins * 100 // len(subset):3}%) | "
          f"медиана {statistics.median(pcts):8.2f}% | сумма {sum(pnls):10.1f} TON | "
          f"среднее {statistics.mean(pcts):8.2f}%")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Backtest on the reconstructed order book")
    parser.add_argument("--db", default=config.DB_DSN, help="ALWAYS a copy: clean_signals writes is_ladder")
    parser.add_argument("--hold-hours", type=float, default=24.0)
    parser.add_argument("--undercut-pct", type=Decimal, default=Decimal("1"))
    parser.add_argument("--window-hours", type=int, default=24, help="cascade window, memory guard")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--marketplaces", default=",".join(MARKETPLACES),
                        help="run them one at a time on a small server")
    parser.add_argument("--cache-pairs", type=int, default=256, help="bounded pair cache (memory guard)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    book = Book(conn, cache_size=args.cache_pairs)
    hold = timedelta(hours=args.hold_hours)
    undercut = args.undercut_pct / 100

    if not args.skip_validation:
        ok, text = validate(conn, book)
        print(text)
        if not ok:
            print("ВОССТАНОВЛЕНИЕ НЕ ПРОШЛО ПРОВЕРКУ — числам ниже верить нельзя, вывод прекращён.")
            return 1
        print()

    print(f"окно удержания {args.hold_hours:.0f} ч, продаём на {args.undercut_pct}% дешевле "
          f"самого дешёвого конкурента (ДОПУЩЕНИЕ)\n")

    trades: list[tuple] = []          # (pnl_ton, pnl_pct, level, marketplace, depth, ratio)
    no_competitor: dict[str, int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)
    for marketplace in args.marketplaces.split(","):
        marketplace = marketplace.strip()
        for s in stream_signals(conn, marketplace, args.window_hours):
            totals[marketplace] += 1
            best = book.cheapest_competitor(s, s.observed_at + hold)
            if best is None:
                no_competitor[marketplace] += 1
                continue
            entry = s.new_price_nano
            pnl = pnl_nano(marketplace, exit_price_nano(s, best[0], undercut), entry)
            trades.append((float(Decimal(pnl) / NANO), float(Decimal(pnl) * 100 / entry),
                           s.floor_level, marketplace, s.listed_count or 0,
                           float(s.ratio) if s.ratio is not None else None,
                           pair_had_a_real_sale(conn, s, hold)))

    print("чистых сигналов за всю историю (до межбиржевой сверки):")
    for m in totals:
        gone = no_competitor[m]
        n = totals[m]
        print(f"  {m:8} {n:5} | без конкурента через {args.hold_hours:.0f} ч (не продан): "
              f"{gone:5} ({gone * 100 // max(n, 1):3}%)")
    if not trades:
        print("\nизмеримых выходов нет")
        return 0

    print(f"\nРЕЗУЛЬТАТ ПО ИЗМЕРИМЫМ ВЫХОДАМ (n={len(trades)}):")
    report("все", trades)
    for level in ("pair", "model"):
        report(f"уровень {level}", [t for t in trades if t[2] == level])
    for m in totals:
        report(f"площадка {m}", [t for t in trades if t[3] == m])
    for label, lo, hi in (("глубина 2-3", 2, 3), ("глубина 4-9", 4, 9), ("глубина 10+", 10, 10**6)):
        report(label, [t for t in trades if lo <= t[4] <= hi])
    for label, lo, hi in (("ratio < 1.2", 0, 1.2), ("ratio 1.2-1.5", 1.2, 1.5), ("ratio > 1.5", 1.5, 10**6)):
        report(label, [t for t in trades if t[5] is not None and lo <= t[5] < hi])
    sellable = [t for t in trades if t[6]]
    print(f"\nТО ЖЕ, НО ТОЛЬКО ТАМ, ГДЕ ПОКУПАТЕЛЬ РЕАЛЬНО НАШЁЛСЯ "
          f"(подтверждённая сделка по этой паре в окне): n={len(sellable)} из {len(trades)}")
    report("все", sellable)
    for level in ("pair", "model"):
        report(f"уровень {level}", [t for t in sellable if t[2] == level])
    for m in totals:
        report(f"площадка {m}", [t for t in sellable if t[3] == m])

    print("\nВАЖНО: сигналы ДО межбиржевой сверки; живая система отправляет меньше.")
    print("ВАЖНО: верхний блок считает, что мы продаём наверняка по своей цене.")
    print("Нижний блок — только там, где сделка по паре действительно была;")
    print("подтверждённые сделки есть лишь на MRKT, поэтому для Portals и Tonnel")
    print("это заниженная оценка, а не истина.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
