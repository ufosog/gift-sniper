"""Diagnostic script (Tonnel full-collector delivery). The tg_id
construction rule in tonnel_parsing.py (<NameБезПробелов>-<gift_num>,
same as Portals' confirmed rule) is now CONFIRMED live -- 8/8 lots
checked this way were found at t.me/nft/<tg_id> (see README). This
script is no longer a blocking prerequisite for anything; it remains
useful as a spot-check tool -- e.g. after collecting a fresh batch of
Tonnel listings, or if a future collision/edge case (apostrophes, other
punctuation) is suspected.

Read-only against the DB, and only ever GETs t.me pages -- never writes
anything. Requires live network access, so it is NOT run by the
automated test suite; the user runs it directly.

Run: python -m gift_sniper.tonnel_tgid_probe --db gift_sniper.db --count 5
"""
from __future__ import annotations

import argparse
import sys

import requests

from . import config, db

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _nft_page_url(tg_id: str) -> str:
    return f"https://t.me/nft/{tg_id}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--count", type=int, default=5, help="How many collected Tonnel listings to probe.")
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to run: {exc}", file=sys.stderr)
        return 1

    rows = conn.execute(
        "SELECT external_id, tg_id, collection_name, gift_number FROM listings "
        "WHERE marketplace = 'tonnel' AND tg_id IS NOT NULL "
        "ORDER BY first_seen_at DESC LIMIT ?",
        (args.count,),
    ).fetchall()

    if not rows:
        print("No Tonnel listings with a constructed tg_id found in the DB -- run tonnel_poller.py first.")
        return 0

    session = requests.Session()
    found = 0
    print(f"{'external_id':<14}{'tg_id':<28}{'result'}")
    for external_id, tg_id, collection_name, gift_number in rows:
        url = _nft_page_url(tg_id)
        try:
            resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            ok = resp.status_code == 200 and "og:title" in resp.text
        except requests.exceptions.RequestException as exc:
            print(f"{external_id:<14}{tg_id:<28}error: {exc}")
            continue
        if ok:
            found += 1
        print(f"{external_id:<14}{tg_id:<28}{'found' if ok else f'NOT FOUND (status={resp.status_code})'}")

    print()
    print(f"found {found}/{len(rows)}")
    if found < len(rows):
        print(
            "Некоторые страницы не найдены для этой выборки -- само правило "
            "построения tg_id уже подтверждено (8/8 ранее), но стоит проверить "
            "конкретные не найденные лоты на нестандартные символы в названии "
            "(например, апострофы) или на то, что лот уже снят с продажи."
        )
    else:
        print("Все найдены.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
