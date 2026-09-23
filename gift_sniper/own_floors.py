"""Combo-floor computed from OUR OWN collected listings, correctly scoped
by collection.

Why this module exists: the /collections/models/backgrounds/floors API
answers by model name GLOBALLY, ignoring collection_id/short_name
(confirmed live). Model names collide across unrelated collections
constantly -- confirmed live: Berry Box (collection floor 9.35) got an
API combo-floor of 200.0; Liberty Figure (floor 4.39) got 65-97. Grouping
by collection_name here is the one thing that fixes this -- removing it
reproduces exactly the bug this module exists to fix.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass
class OwnFloor:
    floor_nano: int | None  # minimum price_nano among matching active listings
    sample_size: int
    confidence: str  # none|low|medium|high
    oldest_seen_at: datetime | None
    newest_seen_at: datetime | None


def _confidence_for(sample_size: int) -> str:
    if sample_size == 0:
        return "none"
    if sample_size <= 3:
        return "low"
    if sample_size <= 9:
        return "medium"
    return "high"


def own_combo_floor(
    conn: sqlite3.Connection,
    collection_name: str,
    model_name: str,
    backdrop_name: str,
    as_of: datetime,
    marketplace: str = "portals",
) -> OwnFloor:
    """Floor over listings in the SAME collection_name, model_name and
    backdrop_name, with status='listed' and first_seen_at <= as_of.
    `collection_name` in the grouping key is mandatory -- it is the only
    difference from the broken API floor, and it is the difference that
    matters.

    `marketplace` (schema v13, default "portals" -- this module is only
    ever called for Portals's own analytics path this delivery, see
    README "этап 2") explicitly scopes the query: `listings` now holds
    Tonnel rows too, and collection/model/backdrop NAMES are confirmed to
    match across the two marketplaces -- an unfiltered query would
    silently blend Tonnel prices (a different currency, TON not GRAM,
    and a different fee model) into a Portals floor.
    """
    rows = conn.execute(
        """
        SELECT price_nano, first_seen_at
        FROM listings
        WHERE marketplace = ?
          AND collection_name = ?
          AND model_name = ?
          AND backdrop_name = ?
          AND status = 'listed'
          AND price_nano IS NOT NULL
          AND first_seen_at <= ?
        """,
        (marketplace, collection_name, model_name, backdrop_name, as_of.isoformat()),
    ).fetchall()

    if not rows:
        return OwnFloor(floor_nano=None, sample_size=0, confidence="none",
                         oldest_seen_at=None, newest_seen_at=None)

    prices = [r[0] for r in rows]
    seen_ats = [datetime.fromisoformat(r[1]) for r in rows]
    sample_size = len(rows)

    return OwnFloor(
        floor_nano=min(prices),
        sample_size=sample_size,
        confidence=_confidence_for(sample_size),
        oldest_seen_at=min(seen_ats),
        newest_seen_at=max(seen_ats),
    )
