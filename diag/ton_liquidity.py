"""How liquid is a TON NFT collection, really? Measured from the chain.

The gift marketplaces turned out too thin to flip: 2352 pairs, a median of
ONE confirmed sale each in 9.5 days (diag/liquid_segment.py). Before
building anything for another market, measure the only thing that decides
it -- how often items there actually change hands.

Fragment's usernames and numbers settle on TON, so every sale is public
and already recorded. No collection period is needed: this reads the chain
through tonapi.io.

Method: sample random items of the collection, read each item's history,
count real purchases (NftPurchase / sale events) in the last N days, and
report how many items trade and at what prices.

Usage: python diag/ton_liquidity.py --sample 60 --days 30
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter

from curl_cffi import requests as cr

API = "https://tonapi.io/v2"
COLLECTIONS = {
    # Public, well-known collection addresses.
    "Telegram Usernames": "EQCA14o1-VWhS2efqoh_9M1b_A9DtKTuoqfmkn83AbJzwnPi",
    "Anonymous Numbers": "EQAOQdwdw8kGftJCSFgOErM1mBjYPe4DBPq8-AhF6vr9si5N",
}
NANO = 10 ** 9


def get(session, path: str, params: dict | None = None, tries: int = 3):
    for attempt in range(tries):
        r = session.get(f"{API}{path}", params=params, impersonate="chrome", timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:  # free tier: 1 request per second
            time.sleep(1.5 * (attempt + 1))
            continue
        return None
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TON collection liquidity, measured on-chain")
    parser.add_argument("--sample", type=int, default=60, help="items to sample per collection")
    parser.add_argument("--days", type=float, default=30.0)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    session = cr.Session()
    since = time.time() - args.days * 86400

    for name, address in COLLECTIONS.items():
        info = get(session, f"/nfts/collections/{address}")
        total = (info or {}).get("next_item_index")
        print(f"\n=== {name}: всего предметов {total}")
        items = get(session, f"/nfts/collections/{address}/items",
                    {"limit": args.sample, "offset": 0})
        addresses = [i["address"] for i in (items or {}).get("nft_items", [])]
        if not addresses:
            print("  список предметов недоступен")
            continue

        traded, sales, prices = 0, 0, []
        kinds: Counter = Counter()
        for address_item in addresses:
            # /nfts/items/{addr}/history is 404: the account events endpoint
            # is the one that carries NFT actions (checked 2026-09-22).
            history = get(session, f"/accounts/{address_item}/events", {"limit": 50})
            events = (history or {}).get("events", [])
            item_sales = 0
            for event in events:
                if event.get("timestamp", 0) < since:
                    continue
                for action in event.get("actions", []):
                    kinds[action.get("type")] += 1
                    if action.get("type") in ("NftPurchase", "AuctionBid"):
                        item_sales += 1 if action.get("type") == "NftPurchase" else 0
                        amount = (action.get(action["type"]) or {}).get("amount")
                        if amount:
                            try:
                                prices.append(int(amount) / NANO)
                            except (TypeError, ValueError):
                                pass
            sales += item_sales
            traded += 1 if item_sales else 0
            time.sleep(1.05)  # free tier

        n = len(addresses)
        print(f"  выборка: {n} предметов")
        print(f"  торговались за {args.days:.0f} дн: {traded} ({traded * 100 // n}%), всего сделок {sales}")
        if total and n:
            print(f"  оценка сделок по всей коллекции: {sales * total // n} за {args.days:.0f} дн "
                  f"(~{sales * total // n / args.days:.0f} в сутки)")
        if prices:
            prices.sort()
            print(f"  цены TON: мин {prices[0]:.0f}, медиана {prices[len(prices) // 2]:.0f}, "
                  f"макс {prices[-1]:.0f}")
        print(f"  типы событий: {dict(kinds.most_common(6))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
