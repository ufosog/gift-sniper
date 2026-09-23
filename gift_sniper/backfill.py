"""Offline backfill for pair_floor_excl_self_nano / pair_listed_count_excl_self
/ pair_self_was_floor on floor_snapshots rows written before self-exclusion
existed (their pair_floor_status/discount gating already excludes rows where
these are NULL, but that means those rows just vanish from the main
distribution rather than being usable).

IMPORTANT, confirmed by inspecting the actual schema and code path
(FloorCache.snapshot_for / floors.py): `floor_snapshots.raw_model_block`
holds the API COMBO-FLOOR response block, i.e.
`{"<backdrop_name>": "<price>", ...}` -- keyed by BACKDROP NAME, with no
`listing_external_id` anywhere in it. It is NOT pair_floor.py's per-listing
order-book response (`PairFloor.raw`, a list of `{"id", "status", "price"}`
entries) -- that value was never persisted to any column. There is
therefore no listing to exclude from `raw_model_block`, for any row: this
is not a bug to work around, the data this task assumed exists simply
doesn't. Confirmed with the user before writing this script; the decision
was to leave every such row NULL rather than fabricate or approximate a
value, and report exactly how many rows fall into this bucket.

This script still does the one thing that's honestly possible offline: it
defensively checks whether `raw_model_block` for a given row happens to be
list-shaped with per-listing entries (in case a future/different code path
ever populates it that way) and reconstructs the self-excluded floor from
that if so. Under the current codebase this branch is always empty --
every row's `raw_model_block` is backdrop-keyed -- so the practical result
today is 0 recomputed, 100% left NULL, with the reason printed.

Getting a REAL value for these old rows requires the network (the live
order book at some point in time) -- there is no way around that. The
options are: (1) accept these old rows never get a self-excluded floor and
rely on freshly-collected data going forward (pair_listed_count_excl_self
etc. are already computed correctly by run_floor_worker() for every row
written under the current code), or (2) re-run a live query against
still-relevant (collection, model, backdrop) triples -- out of scope here
per the "no network calls" constraint on this script.

No network calls. No data deleted. Idempotent: safe to re-run.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import config, db
from .money import to_nano


def _try_extract_self_excluded_floor(
    raw_model_block, exclude_external_id: str
) -> tuple[int, int, bool] | None:
    """Returns (floor_excl_self_nano, listed_count_excl_self, self_was_floor)
    if `raw_model_block` turns out to be a list of per-listing entries
    (defensive future-proofing only -- see module docstring). Returns None
    if the shape doesn't support it, which is the case for EVERY row under
    the current codebase (dict keyed by backdrop name, not by listing).
    Never guesses.
    """
    if not raw_model_block:
        return None
    if not isinstance(raw_model_block, list):
        return None  # current reality: dict keyed by backdrop name -- unusable

    listed: list[tuple[object, int]] = []
    for item in raw_model_block:
        if not isinstance(item, dict):
            return None
        if item.get("status") != "listed" or item.get("price") is None:
            continue
        price_nano = to_nano(item.get("price"))
        if price_nano is None:
            continue
        listed.append((item.get("id"), price_nano))

    if not listed:
        return None

    all_prices = [p for _, p in listed]
    floor_all = min(all_prices)
    excluding = [p for i, p in listed if i != exclude_external_id]
    if not excluding:
        return None  # would-be no_data -- leave NULL, not 0

    floor_excl = min(excluding)
    self_price = next((p for i, p in listed if i == exclude_external_id), None)
    self_was_floor = self_price is not None and self_price == floor_all

    return floor_excl, len(excluding), self_was_floor


def run_backfill(conn: sqlite3.Connection) -> dict[str, int]:
    stats = {
        "processed": 0,
        "recomputed": 0,
        "left_null_empty_raw_model_block": 0,
        "left_null_backdrop_keyed_not_per_listing": 0,
    }

    rows = conn.execute(
        "SELECT listing_external_id, raw_model_block FROM floor_snapshots"
    ).fetchall()

    for listing_external_id, raw_json in rows:
        stats["processed"] += 1
        try:
            raw_model_block = json.loads(raw_json) if raw_json else {}
        except (TypeError, ValueError):
            raw_model_block = {}

        if not raw_model_block:
            stats["left_null_empty_raw_model_block"] += 1
            continue

        result = _try_extract_self_excluded_floor(raw_model_block, listing_external_id)
        if result is None:
            stats["left_null_backdrop_keyed_not_per_listing"] += 1
            continue

        floor_excl_nano, listed_count_excl, self_was_floor = result
        with conn:
            conn.execute(
                """
                UPDATE floor_snapshots
                SET pair_floor_excl_self_nano = ?,
                    pair_listed_count_excl_self = ?,
                    pair_self_was_floor = ?
                WHERE listing_external_id = ?
                """,
                (floor_excl_nano, listed_count_excl, 1 if self_was_floor else 0, listing_external_id),
            )
        stats["recomputed"] += 1

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.DB_DSN)
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)  # runs migrations first, same as report.py
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to backfill: {exc}", file=sys.stderr)
        return 1
    db.verify_schema(conn)

    stats = run_backfill(conn)

    print("=== backfill.py: pair_floor self-exclusion ===")
    print(f"rows processed: {stats['processed']}")
    print(f"rows recomputed: {stats['recomputed']}")
    print(f"rows left NULL (raw_model_block empty): {stats['left_null_empty_raw_model_block']}")
    print(
        "rows left NULL (raw_model_block is backdrop-keyed, not per-listing -- "
        f"self-exclusion is not reconstructable offline): "
        f"{stats['left_null_backdrop_keyed_not_per_listing']}"
    )
    if stats["recomputed"] == 0 and stats["processed"] > 0:
        print(
            "\nNOTE: 0 rows recomputed is EXPECTED under the current codebase -- "
            "raw_model_block has never stored per-listing pair data (see this "
            "module's docstring). Historical rows will not get a real "
            "pair_floor_excl_self_nano from this script; only listings collected "
            "under the current poller.py (which computes it live) will."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
