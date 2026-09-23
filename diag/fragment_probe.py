"""Is Fragment liquid enough to be worth measuring properly?

Two weeks of data showed the gift marketplaces are too thin to flip: 2352
pairs, a median of ONE confirmed sale each (diag/liquid_segment.py). Before
building anything for Fragment, check the only thing that matters first --
how many lots actually change hands there, and at what prices.

Fragment publishes sold lots, so this needs no collection period at all.
Read-only, network only to fragment.com.

Usage: python diag/fragment_probe.py
"""
from __future__ import annotations

import re
import sys
from collections import Counter

from curl_cffi import requests as cr

PAGES = {
    "номера, продано": "https://fragment.com/numbers?filter=sold",
    "юзернеймы, продано": "https://fragment.com/username?filter=sold",
    "номера, в продаже": "https://fragment.com/numbers?filter=sale",
    "юзернеймы, в продаже": "https://fragment.com/username?filter=sale",
}

ROW_RE = re.compile(
    r'href="/(?P<kind>number|username)/(?P<id>[^"]+)".*?'
    r'(?P<price>\d[\d\s,]*)\s*</div>.*?'
    r'(?:(?P<when>\w{3}\s\d{1,2},\s\d{4}|\d+\s\w+\sago))?',
    re.S)
PRICE_RE = re.compile(r'<div class="table-cell-value tm-value icon-before icon-ton">([\d\s,]+)</div>')
ITEM_RE = re.compile(r'href="/(number|username)/([^"?]+)"')
DATE_RE = re.compile(r'<div class="table-cell-desc">([^<]{3,30})</div>')


def fetch(session, url: str) -> str:
    return session.get(url, impersonate="chrome", timeout=30).text


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    session = cr.Session()
    for label, url in PAGES.items():
        try:
            html = fetch(session, url)
        except Exception as exc:
            print(f"{label}: ошибка {type(exc).__name__}")
            continue
        prices = [int(p.replace(",", "").replace(" ", "")) for p in PRICE_RE.findall(html)]
        items = {i for _kind, i in ITEM_RE.findall(html)}
        dates = DATE_RE.findall(html)
        print(f"\n=== {label}")
        print(f"  лотов на странице: {len(items)}, цен: {len(prices)}")
        if prices:
            prices.sort()
            mid = prices[len(prices) // 2]
            print(f"  цены TON: мин {prices[0]}, медиана {mid}, макс {prices[-1]}")
        if dates:
            print(f"  отметки времени (примеры): {dates[:6]}")
            print(f"  распределение: {Counter(dates).most_common(5)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
