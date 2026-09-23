"""One-shot cleanup for listings.currency rows written before
CURRENCY_DEFAULT was corrected from "TON" to "GRAM" (see config.py):
11925 rows with currency='TON' against 1088 with 'GRAM' were measured
live. The API's /nfts/search response carries no currency field at all
-- parsing.py always falls back to config.CURRENCY_DEFAULT -- so the old
rows' "TON" was never data from the marketplace, it was our own wrong
default being stamped onto every row. Confirmed GRAM via /market/config
and the marketplace's own UI.

Updates currency to config.CURRENCY_DEFAULT on every listings row where
it currently differs. Idempotent: a second run updates 0 rows.

Run once, after upgrading CURRENCY_DEFAULT: python -m gift_sniper.fix_currency --db gift_sniper.db
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
        print(f"Schema check failed, refusing to fix currency: {exc}", file=sys.stderr)
        return 1

    updated_count = db.fix_listings_currency(conn, config.CURRENCY_DEFAULT)
    print(f"listings rows updated: {updated_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
