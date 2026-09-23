"""Do username resellers actually make money? Prices read from the chain.

Established already (2026-09-22/23): the Telegram Usernames collection
sees ~6,100 ownership changes a day and 40% of the names involved change
hands twice within two days -- unlike gifts, where a pair traded once in
nine days. What is still unknown, and what decides everything, is the
PRICE of those repeat trades.

Method, entirely on-chain (toncenter):
  1. recent transfers of the collection -> items that moved 2+ times;
  2. for each of those transfers, its transaction;
  3. the sale price = the largest TON value moved in that transaction
     (the payment to the seller; escrow hops carry no such value);
  4. gain = later price * (1 - Fragment's 5% seller fee) - earlier price.

A transfer with no payment (a plain move between wallets, or an escrow
hop) is skipped, not counted as a zero-price trade.

Read-only, no keys. Usage: python diag/username_resale_prices.py --items 40
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import defaultdict
from decimal import Decimal

from curl_cffi import requests as cr

COLLECTION = "EQCA14o1-VWhS2efqoh_9M1b_A9DtKTuoqfmkn83AbJzwnPi"
TRANSFERS = "https://toncenter.com/api/v3/nft/transfers"
TRANSACTIONS = "https://toncenter.com/api/v3/transactions"
NANO = Decimal(10) ** 9
SELLER_FEE = Decimal("0.05")
# Below this a "payment" is gas, not a price.
MIN_PRICE_TON = Decimal("1")


def get(session, url: str, params: dict, tries: int = 3):
    for attempt in range(tries):
        r = session.get(url, params=params, impersonate="chrome", timeout=45)
        if r.status_code == 200:
            return r.json()
        time.sleep(1.5 * (attempt + 1))
    return None


def sale_price_ton(session, tx_hash: str) -> Decimal | None:
    """Largest TON value moved in the transaction, in TON."""
    data = get(session, TRANSACTIONS, {"hash": tx_hash, "limit": 1})
    if not data or not data.get("transactions"):
        return None
    tx = data["transactions"][0]
    values = []
    for message in [tx.get("in_msg")] + list(tx.get("out_msgs") or []):
        if not message:
            continue
        try:
            values.append(Decimal(int(message.get("value") or 0)) / NANO)
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    best = max(values)
    return best if best >= MIN_PRICE_TON else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Realised gains on username resales, from the chain")
    parser.add_argument("--pages", type=int, default=6, help="transfer pages of 1000 to scan")
    parser.add_argument("--items", type=int, default=40, help="items with repeat trades to price")
    parser.add_argument("--delay", type=float, default=1.05)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    session = cr.Session()
    moves: dict[str, list[tuple[int, str]]] = defaultdict(list)
    offset = 0
    for _ in range(args.pages):
        data = get(session, TRANSFERS, {"collection_address": COLLECTION, "limit": 1000,
                                        "offset": offset, "sort": "desc"})
        rows = (data or {}).get("nft_transfers") or []
        if not rows:
            break
        for row in rows:
            moves[row["nft_address"]].append((int(row["transaction_now"]), row["transaction_hash"]))
        offset += len(rows)
        time.sleep(args.delay)

    repeats = {a: sorted(v) for a, v in moves.items() if len(v) >= 2}
    print(f"просмотрено переходов: {offset}, имён с повторными сделками: {len(repeats)}")

    gains: list[float] = []
    holds: list[float] = []
    priced_pairs = 0
    examples: list[str] = []
    for address, events in list(repeats.items())[:args.items]:
        prices = []
        for when, tx_hash in events[-3:]:  # the most recent few moves
            price = sale_price_ton(session, tx_hash)
            time.sleep(args.delay)
            if price is not None:
                prices.append((when, price))
        for (t1, p1), (t2, p2) in zip(prices, prices[1:]):
            priced_pairs += 1
            gain = (p2 * (1 - SELLER_FEE) - p1) * 100 / p1
            gains.append(float(gain))
            holds.append((t2 - t1) / 3600)
            if len(examples) < 8:
                examples.append(f"{p1:.0f} -> {p2:.0f} TON за {(t2 - t1) / 3600:.1f} ч ({gain:.0f}%)")

    if not gains:
        print("оплаченных повторных сделок не нашлось — большинство переходов без платежа")
        return 0

    gains.sort()
    wins = sum(1 for g in gains if g > 0)
    print(f"\nПЕРЕПРОДАЖИ С ИЗВЕСТНЫМИ ЦЕНАМИ: n={priced_pairs}")
    print(f"  в плюсе {wins} ({wins * 100 // len(gains)}%)")
    print(f"  медиана {statistics.median(gains):.1f}% | среднее {statistics.mean(gains):.1f}%")
    print(f"  четверти: 25% {gains[len(gains) // 4]:.1f}% | 75% {gains[3 * len(gains) // 4]:.1f}%")
    print(f"  держали: медиана {statistics.median(holds):.1f} ч")
    print("\nпримеры:")
    for e in examples:
        print("  ", e)
    print("\nВАЖНО: цена = крупнейший платёж в транзакции перехода; часть переходов")
    print("без платежа (подарок, перевод между своими кошельками) сюда не попала.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
