"""Diagnostic script (ДОПОЛНЕНИЕ, Правка 2): answers ONE specific
question the user asked to check live -- does Tonnel's pageGifts
endpoint return a lot that no longer matches the standard BASE_FILTER's
{"buyer": {"$exists": false}} / {"refunded": {"$ne": true}} conditions,
if those two conditions are dropped from the filter? If yes, a positive
"this lot sold" signal might exist after all, and the lifecycle
limitation recorded in README would need revisiting. If no (the lot is
still absent), the limitation is confirmed for good.

Takes one gift_id already known to be gone from the ordinary
(BASE_FILTER-restricted) search -- i.e. a listing this project's own
listing_lifecycle table has seen recorded via
lifecycle_not_returned/record_lifecycle_check_missing -- and re-queries
for that exact gift_id with buyer/refunded removed from the filter.
Prints whether it comes back, and the raw item if so.

Read-only, one request, no writes. Requires live network access, so it
is NOT run by the automated test suite; the user runs it directly.

Run: python -m gift_sniper.tonnel_sold_lot_probe --gift-id 572646
"""
from __future__ import annotations

import argparse
import json
import sys

from .tonnel_client import HEADERS, TONNEL_API_URL, TonnelClient, TonnelError


def probe_gift_id_without_buyer_refunded_filter(client: TonnelClient, gift_id: int) -> list[dict]:
    """Bypasses TonnelClient.search()'s BASE_FILTER on purpose -- this
    is the one place in the whole project that deliberately queries
    WITHOUT {"buyer": {"$exists": false}} / {"refunded": {"$ne": true}},
    to answer whether dropping them reveals a sold/refunded lot. Every
    other filter (price exists, export_at exists, asset=TON) is kept, to
    stay as close to BASE_FILTER as possible while isolating just the
    two conditions in question.
    """
    filter_dict = {
        "price": {"$exists": True},
        "export_at": {"$exists": True},
        "asset": "TON",
        "gift_id": gift_id,
    }
    body = {
        "page": 1,
        "limit": 30,
        "sort": json.dumps({"price": 1}),
        "filter": json.dumps(filter_dict),
        "price_range": None,
        "user_auth": "",
    }
    client._throttle()
    resp = client._session.post(TONNEL_API_URL, json=body, headers=HEADERS, impersonate="chrome", timeout=15)
    if resp.status_code != 200:
        raise TonnelError(f"unexpected status {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code)
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        raise TonnelError(f"API error: {data['error']}")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gift-id", type=int, required=True, help="A gift_id known to be gone from ordinary search.")
    args = parser.parse_args(argv)

    client = TonnelClient()
    try:
        results = probe_gift_id_without_buyer_refunded_filter(client, args.gift_id)
    except TonnelError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1

    if results:
        print(f"FOUND without buyer/refunded filter -- gift_id={args.gift_id}:")
        print(json.dumps(results[0], indent=2, ensure_ascii=False))
        print()
        print(
            "Вывод: снятие buyer/refunded из фильтра ПОКАЗЫВАЕТ лот -- "
            "возможно, положительный признак продажи существует. Проверить "
            "поля ответа (buyer/refunded/status) и пересмотреть README."
        )
    else:
        print(f"NOT FOUND even without buyer/refunded filter -- gift_id={args.gift_id}")
        print(
            "Вывод: ограничение подтверждено окончательно -- Tonnel не "
            "даёт положительного признака продажи этим способом."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
