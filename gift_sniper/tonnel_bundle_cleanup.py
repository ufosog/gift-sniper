"""One-shot cleanup for the 6 bundle rows measured live before Правка 2
added the collection-time filter (see tonnel_poller.py: gift_id < 0 is
now dropped before it ever reaches the DB). A negative gift_id is a
BUNDLE (Tonnel's own documented convention) -- its price is for the
whole set, never comparable to a single lot's price, so any
already-collected bundle rows must be removed before they can distort
anything downstream.

Deletes from listings, price_history, and listing_lifecycle for
marketplace='tonnel' where external_id (= str(gift_id)) starts with
"-". Idempotent: a second run deletes 0 rows.

Run once: python -m gift_sniper.tonnel_bundle_cleanup --db gift_sniper.db
"""
from __future__ import annotations

import argparse
import sys

from . import config, db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)  # runs migrations -- NOT a bare sqlite3.connect()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to clean up: {exc}", file=sys.stderr)
        return 1

    deleted_count = db.delete_tonnel_bundle_listings(conn)
    print(f"bundle listings deleted: {deleted_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
