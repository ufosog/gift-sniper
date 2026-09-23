"""Do people actually EARN reselling Telegram usernames?

Not "is there a spread" and not "is it liquid" -- the gifts market looked
fine on both of those and still lost money. This measures the only thing
that settles it: what real resellers actually got.

Method:
  1. recent ownership changes of the Telegram Usernames collection, from
     the chain (toncenter), so the sample is what the market really traded;
  2. each item's username, from tonapi metadata;
  3. that username's price history from its Fragment page, which lists the
     prices it changed hands at;
  4. for every consecutive pair of sales: gain = later / earlier, minus
     Fragment's 5% seller fee.

No network writes, no keys, read-only. Run anywhere with internet.
Usage: python diag/username_flips.py --sample 60
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from decimal import Decimal

from curl_cffi import requests as cr

COLLECTION = "EQCA14o1-VWhS2efqoh_9M1b_A9DtKTuoqfmkn83AbJzwnPi"
TONCENTER = "https://toncenter.com/api/v3/nft/transfers"
TONCENTER_ITEM = "https://toncenter.com/api/v3/nft/items"
FRAGMENT_ITEM = "https://fragment.com/username/{}"
# Fragment's cut on a sale, from its own terms.
SELLER_FEE = Decimal("0.05")

PRICE_RE = re.compile(r'class="table-cell-value tm-value[^"]*"[^>]*>([\d,]+)</div>')
NAME_RE = re.compile(r"^@?([A-Za-z0-9_]+)")


def recent_items(session, limit: int) -> list[str]:
    r = session.get(TONCENTER, params={"collection_address": COLLECTION, "limit": limit, "sort": "desc"},
                    impersonate="chrome", timeout=45)
    if r.status_code != 200:
        return []
    seen, out = set(), []
    for t in r.json().get("nft_transfers", []):
        address = t.get("nft_address")
        if address and address not in seen:
            seen.add(address)
            out.append(address)
    return out


def username_of(session, address: str) -> str | None:
    """The username itself, from the item's on-chain content.

    tonapi answers 404 for these items in every address spelling tried
    (checked 2026-09-22); toncenter carries content.domain = "name.t.me".
    """
    r = session.get(TONCENTER_ITEM, params={"address": address}, impersonate="chrome", timeout=40)
    if r.status_code != 200:
        return None
    items = r.json().get("nft_items") or []
    if not items:
        return None
    domain = ((items[0].get("content") or {}).get("domain") or "").strip()
    m = NAME_RE.match(domain.split(".")[0])
    return m.group(1) if m else None


def price_history(session, username: str) -> list[int]:
    """Prices the username changed hands at, newest first, in TON."""
    r = session.get(FRAGMENT_ITEM.format(username), impersonate="chrome", timeout=30)
    if r.status_code != 200:
        return []
    return [int(p.replace(",", "")) for p in PRICE_RE.findall(r.text)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Realised gains of username resellers")
    parser.add_argument("--sample", type=int, default=60)
    parser.add_argument("--delay", type=float, default=1.1)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    session = cr.Session()
    items = recent_items(session, max(args.sample * 3, 200))[:args.sample]
    print(f"предметов из недавних сделок: {len(items)}")

    resold, gains, singles, no_data = 0, [], 0, 0
    examples = []
    for address in items:
        username = username_of(session, address)
        time.sleep(args.delay)
        if not username:
            no_data += 1
            continue
        prices = price_history(session, username)
        time.sleep(args.delay)
        if len(prices) < 2:
            singles += 1
            continue
        # newest first: prices[0] is the latest price, prices[1] the one before
        latest, previous = Decimal(prices[0]), Decimal(prices[1])
        if previous <= 0:
            no_data += 1
            continue
        resold += 1
        gain = (latest * (1 - SELLER_FEE) - previous) * 100 / previous
        gains.append(float(gain))
        if len(examples) < 8:
            examples.append(f"@{username}: {previous} -> {latest} TON ({gain:.0f}%)")

    print(f"  с историей минимум двух сделок: {resold}")
    print(f"  только одна цена (не перепродавались): {singles}")
    print(f"  без данных: {no_data}")
    if not gains:
        print("перепродаж в выборке нет — измерять нечего")
        return 0

    gains.sort()
    wins = sum(1 for g in gains if g > 0)
    print(f"\nРЕЗУЛЬТАТ ПЕРЕПРОДАЖ (после комиссии Fragment {SELLER_FEE * 100:.0f}%): n={len(gains)}")
    print(f"  в плюсе {wins} ({wins * 100 // len(gains)}%)")
    print(f"  медиана {statistics.median(gains):.1f}% | среднее {statistics.mean(gains):.1f}%")
    print(f"  четверти: 25% {gains[len(gains) // 4]:.1f}% | 75% {gains[3 * len(gains) // 4]:.1f}%")
    print("\nпримеры:")
    for e in examples:
        print("  ", e)
    print("\nВАЖНО: это результат ТЕХ, КТО УЖЕ ТОРГУЕТ, а не наш прогноз.")
    print("Он показывает верхнюю границу: мы бы конкурировали с этими же людьми.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
