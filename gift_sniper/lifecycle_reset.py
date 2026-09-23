"""One-shot cleanup for listing_lifecycle data collected under the
retired time-since-last-seen disappearance rule -- confirmed unreliable
(39325 "newly gone" in one night against 11705 listings ever collected;
one listing hit reappeared_count=41). That rule measured feed
resurfacing, not real disappearance -- see config.py / README.

Resets disappeared_at, reappeared_count, and final_status to their
defaults on EVERY row, while PRESERVING first_seen_at and last_seen_at
(when each listing was actually first/last observed in the feed is
still accurate -- only the disappearance/reappearance bookkeeping was
wrong). last_checked_at is also cleared, so every row re-enters the
background API-status-check queue from scratch under the new mechanism.

Run once, after upgrading to schema v10: python -m gift_sniper.lifecycle_reset --db gift_sniper.db
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
        print(f"Schema check failed, refusing to reset: {exc}", file=sys.stderr)
        return 1

    reset_count = db.reset_lifecycle_data(conn)
    print(f"listing_lifecycle rows reset: {reset_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
