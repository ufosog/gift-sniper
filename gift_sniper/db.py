"""Schema and storage. SQL written to be portable to Postgres later
(no SQLite-only syntax beyond AUTOINCREMENT-free INTEGER PK and the
"INSERT ... ON CONFLICT" upsert form, which Postgres also supports).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from . import config
from .models import FloorSnapshot, Listing, MarketConfigSnapshot

logger = logging.getLogger("gift_sniper.db")

# ДОПОЛНЕНИЕ (MRKT unit-conversion bug fix): a defensive backstop, NOT a
# substitute for getting unit conversion right at the source (see
# mrkt_client.py's fixed *config.NANO bug -- 16.29 TON became
# 1.6e19-nano, past SQLite's ~9.2e18 INTEGER limit, and CRASHED the
# whole poller with OverflowError). 10**16 nano = 10 million TON/GRAM --
# no real gift is worth that; a value above this is treated as a bad
# conversion, not a real price, and the ROW IS DROPPED (logged, not
# written) rather than letting SQLite's own OverflowError take the
# process down. Any future marketplace client with a similar unit bug
# hits this same backstop, not a repeat of this exact crash.
SANITY_MAX_NANO = 10 ** 16


class SchemaError(RuntimeError):
    """Raised when the DB's actual schema can't be reconciled with what
    the code expects -- either because migration is impossible (unknown
    column layout) or because a post-migration verification still finds a
    mismatch. Always includes what to do about it; never a bare
    OperationalError surfacing from the middle of a write.
    """


CURRENT_SCHEMA_VERSION = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL,
    external_id TEXT NOT NULL,
    tg_id TEXT,
    collection_id TEXT,
    collection_name TEXT,
    gift_number INTEGER,
    price_nano INTEGER,
    currency TEXT NOT NULL,
    collection_floor_nano INTEGER,
    model_name TEXT,
    symbol_name TEXT,
    backdrop_name TEXT,
    model_rarity_raw TEXT,
    symbol_rarity_raw TEXT,
    backdrop_rarity_raw TEXT,
    image_url TEXT,
    animation_url TEXT,
    listed_at TEXT,
    unlocks_at TEXT,
    status TEXT,
    first_seen_at TEXT NOT NULL,
    -- NULL for listings below FLOOR_MIN_PRICE_NANO: raw is the single
    -- biggest storage cost at ~560k listings/day, and every parsed field
    -- (price, attributes, rarity, timestamps) is stored as real columns
    -- regardless, so nothing needed for stats or the collision backfill
    -- is lost by dropping raw on cheap listings.
    raw TEXT,
    UNIQUE(marketplace, external_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_listed_at ON listings(listed_at);
CREATE INDEX IF NOT EXISTS idx_listings_collection_model
    ON listings(collection_name, model_name);

CREATE TABLE IF NOT EXISTS floor_snapshots (
    -- marketplace + composite PK (schema v15, Tonnel signals delivery):
    -- Tonnel NOW writes here too -- Правка 2, an at-drop pair-floor
    -- snapshot (pair_floor_excl_self_nano/pair_listed_count_excl_self/
    -- pair_floor_status only; api_combo_floor_nano/own_*/model_* stay
    -- unused for Tonnel rows, see db.upsert_tonnel_pair_floor_snapshot).
    -- Widened from the single-column PK a prior delivery deliberately
    -- kept narrow (when Tonnel genuinely never wrote here) -- external_id
    -- is only unique WITHIN a marketplace.
    listing_external_id TEXT NOT NULL,
    marketplace TEXT NOT NULL DEFAULT 'portals',
    model_name TEXT NOT NULL,
    backdrop_name TEXT,
    -- Floor from the API, scoped ONLY by model name (endpoint ignores
    -- collection_id/short_name). Diagnostics/comparison ONLY -- confirmed
    -- live to be frequently wrong across model-name collisions between
    -- unrelated collections. NEVER used for discount/profit.
    api_combo_floor_nano INTEGER,
    model_min_floor_nano INTEGER,
    floor_fetched_at TEXT NOT NULL,
    floor_age_sec INTEGER NOT NULL,
    raw_model_block TEXT NOT NULL,
    name_collision INTEGER NOT NULL DEFAULT 0,
    floor_skip_reason TEXT,
    -- Floor computed from our own collected listings, correctly scoped
    -- by collection_name (see own_floors.py). Source of truth for
    -- discount/profit.
    own_combo_floor_nano INTEGER,
    own_sample_size INTEGER NOT NULL DEFAULT 0,
    own_confidence TEXT NOT NULL DEFAULT 'none',
    -- 'ok' | 'suspect' | 'no_data' -- sanity check on api_combo_floor_nano
    -- against the listing's own collection_floor_nano. Diagnostic safety
    -- net; does not affect own_combo_floor_nano.
    floor_sanity TEXT NOT NULL DEFAULT 'no_data',
    -- Floor from a direct, filtered /nfts/search query for this exact
    -- (collection_id, model_name, backdrop_name) triple (see
    -- pair_floor.py). SOURCE OF TRUTH for discount/profit -- the two
    -- columns above are diagnostics/fallback only.
    pair_floor_nano INTEGER,
    pair_listed_count INTEGER NOT NULL DEFAULT 0,
    pair_floor_status TEXT NOT NULL DEFAULT 'no_data',
    pair_floor_age_sec INTEGER NOT NULL DEFAULT 0,
    -- Confirmed live (manual review of 20 top price drops, 17/20 were
    -- artifacts): pair_floor_nano above INCLUDES the listing's own price,
    -- so a lot alone in its pair (or the current cheapest) is compared
    -- against ITSELF. pair_floor_nano/pair_listed_count are kept for
    -- diagnostics/comparison ONLY -- these _excl_self columns are the
    -- real source of truth for discount/profit. See pair_floor.py.
    pair_floor_excl_self_nano INTEGER,
    pair_listed_count_excl_self INTEGER NOT NULL DEFAULT 0,
    pair_self_was_floor INTEGER NOT NULL DEFAULT 0,
    -- Model-level fallback (no backdrop filter) -- confirmed live that
    -- most pairs have exactly one active listing (336 measured with one,
    -- zero with ten), so pair_floor_status='alone_in_pair' is the common
    -- case. Only requested when the pair level is alone_in_pair -- see
    -- poller.py. Coarser than pair level; report.py never mixes pair-
    -- and model-level signals into one distribution.
    model_floor_excl_self_nano INTEGER,
    model_listed_count_excl_self INTEGER NOT NULL DEFAULT 0,
    model_floor_status TEXT NOT NULL DEFAULT 'no_data',
    PRIMARY KEY(marketplace, listing_external_id)
);

CREATE TABLE IF NOT EXISTS market_config_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    raw TEXT NOT NULL,
    commission TEXT,
    offer_fee TEXT,
    withdrawal_fee TEXT,
    user_cashback TEXT,
    usd_course TEXT
);

CREATE INDEX IF NOT EXISTS idx_market_config_snapshots_fetched_at
    ON market_config_snapshots(fetched_at);

-- signals._floor_instability; also created by _migration_19_to_20 for
-- existing databases.
CREATE INDEX IF NOT EXISTS idx_floor_snapshots_pair_time
    ON floor_snapshots(marketplace, model_name, backdrop_name, floor_fetched_at);

-- Confirmed live: listed_at is NOT a reliable signal of a new listing --
-- a lot with listed_at from the previous day resurfaced with today's
-- listed_at at the SAME price (a "touch", not a relisting). Known
-- listings are now compared by price on every page they appear on;
-- price_history records every ACTUAL price change (never a no-op touch).
CREATE TABLE IF NOT EXISTS price_history (
    listing_external_id TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    old_price_nano INTEGER NOT NULL,
    new_price_nano INTEGER NOT NULL,
    delta_pct NUMERIC NOT NULL,
    -- Confirmed live: sub-1% drops (e.g. 24.99->24.95, 0.16%) are
    -- relister-bot noise, not real repricing (67.91->65.93, 2.9%, is).
    -- Flagged, not discarded -- still a real observed price change.
    is_noise INTEGER NOT NULL DEFAULT 0,
    old_listed_at TEXT,
    new_listed_at TEXT,
    observed_at TEXT NOT NULL,
    -- Floor computed AT THE MOMENT OF THE DROP (self-excluded), only for
    -- drops clearing PRICE_DROP_MIN_PCT -- see poller.py. NULL for noise
    -- drops and raises: re-fetching the floor for every price touch would
    -- burn rate-limit budget for no reason.
    floor_at_drop_nano INTEGER,
    floor_listed_count_at_drop INTEGER,
    floor_fetched_at TEXT,
    -- "pair" | "model" | NULL -- which comparison level floor_at_drop_nano
    -- came from. Only set when the pair level was "alone_in_pair" and the
    -- model-level fallback was queried instead -- see poller.py.
    floor_level_at_drop TEXT,
    -- Set by report.py's backfill pass (idempotent, like name_collision):
    -- True if this listing had >= LADDER_MIN_DROPS drops within the last
    -- LADDER_WINDOW_HOURS -- a bot methodically walking its own price
    -- down, not a discrete signal. Confirmed live: Low Rider #23134,
    -- 6 steps of exactly 5%, ~30min apart.
    is_ladder INTEGER NOT NULL DEFAULT 0,
    -- Set by poller.py at write time: a single-step drop bigger than
    -- PRICE_DROP_MAX_PCT, or part of a burst of >1 drop on the same
    -- listing within PRICE_DROP_BURST_SEC. Confirmed live: Jelly Bunny
    -- #2627, 999->99->29 within 10 seconds.
    is_anomaly INTEGER NOT NULL DEFAULT 0,
    -- marketplace is part of the key (schema v13, Tonnel full-collector
    -- delivery): external_id is only unique WITHIN a marketplace (see
    -- listings.UNIQUE(marketplace, external_id)) -- without marketplace
    -- here, two different marketplaces' listings could in principle
    -- collide on (listing_external_id, observed_at).
    PRIMARY KEY(marketplace, listing_external_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_price_history_listing_observed
    ON price_history(listing_external_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_price_history_observed_at
    ON price_history(observed_at);

-- Anti-duplicate ledger for the Telegram notifier (notifier.py /
-- poller.py): one row per signal actually sent. PRIMARY KEY matches
-- price_history's own (listing_external_id, observed_at) -- the same
-- signal is never sent twice, even across a poller restart (this table
-- is durable, unlike an in-memory "already sent" set).
-- status: 'sent' (actually delivered) | 'skipped_stale' (the pre-send
-- freshness check, poller.py Правка 3, found the listing was no longer
-- listed at the notified price -- never retried, recorded here so it's
-- never re-checked either).
CREATE TABLE IF NOT EXISTS alerts_sent (
    -- marketplace + composite PK (schema v15, Tonnel signals delivery):
    -- this table is now written by both marketplaces -- external_id is
    -- only unique WITHIN a marketplace (see listings.UNIQUE(marketplace,
    -- external_id)), so without marketplace here two different
    -- marketplaces' alerts could in principle collide on
    -- (listing_external_id, observed_at).
    marketplace TEXT NOT NULL DEFAULT 'portals',
    listing_external_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent',
    PRIMARY KEY(marketplace, listing_external_id, observed_at)
);

-- Listing lifecycle tracking (poller.py): a listing's disappearance
-- is the only signal we have that it MIGHT have sold -- it is NOT proof
-- of a sale (the owner could simply have delisted it). Distinguished
-- from a genuine sale only by whether the listing reappears afterwards
-- (reappeared_count > 0 means it was delisted/relisted, not sold) --
-- see README, this distinction must never be blurred in reporting.
--
-- disappeared_at/final_status are set ONLY by an explicit GET
-- /nfts/search?ids=... status check (see db.record_lifecycle_status),
-- NEVER by a time-since-last-seen rule -- confirmed live that a
-- time-based rule measures feed resurfacing, not actual disappearance
-- (39325 "newly gone" in one night against 11705 total listings ever
-- collected -- impossible for a real signal; one listing hit
-- reappeared_count=41 in a single night). last_checked_at drives the
-- background check queue (oldest-checked-first); final_status records
-- what the check actually saw ('withdrawn' | 'unlisted').
CREATE TABLE IF NOT EXISTS listing_lifecycle (
    -- marketplace + composite PK (schema v13, Tonnel full-collector
    -- delivery): this table IS written by both marketplaces now (Tonnel
    -- has its own lifecycle/disappearance tracking, see tonnel_poller.py)
    -- -- external_id is only unique WITHIN a marketplace.
    marketplace TEXT NOT NULL DEFAULT 'portals',
    listing_external_id TEXT NOT NULL,
    collection_id TEXT,
    model_name TEXT,
    backdrop_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    disappeared_at TEXT,
    last_price_nano INTEGER,
    reappeared_count INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    final_status TEXT,
    -- Правка 2 (MRKT full-signaller delivery, schema v18): MRKT's
    -- "sale" event is the ONLY marketplace signal in this project that
    -- explicitly confirms a sale (Portals/Tonnel only ever give a bare
    -- disappearance, cause unknown -- see README). sold_price_nano is
    -- populated ONLY when final_status='sold' -- NULL for every other
    -- final_status (including Portals'/Tonnel's own disappearances,
    -- which never carry a confirmed price at all).
    sold_price_nano INTEGER,
    -- ПРАВКА 1 (sale-vs-floor delivery, schema v19): the pair floor AT
    -- THE MOMENT OF SALE, self-excluded -- see db.record_sale_floor /
    -- mrkt_poller.py's _handle_sale_event. Filled ONLY for final_status
    -- ='sold', and only when the floor query actually succeeded (status
    -- "ok") -- a thin book, no comparable listings, or a network error
    -- all leave these NULL, never blocking the sale from being recorded
    -- (see README -- this measures whether MRKT's confirmed sale price
    -- tracks the pair floor the profit formula assumes it does).
    floor_at_sale_nano INTEGER,
    floor_listed_count_at_sale INTEGER,
    floor_fetched_at_sale TEXT,
    PRIMARY KEY(marketplace, listing_external_id)
);

CREATE INDEX IF NOT EXISTS idx_listing_lifecycle_pair
    ON listing_lifecycle(collection_id, model_name, backdrop_name);
CREATE INDEX IF NOT EXISTS idx_listing_lifecycle_disappeared_at
    ON listing_lifecycle(disappeared_at);

-- Правка 2 (MRKT full-signaller delivery, schema v18): event-level
-- dedup for mrkt_poller.py's feed processing -- a restart must never
-- reprocess an event it already handled (listing/change_price/sale),
-- which would double-write price_history rows or re-mark a sale.
-- `marketplace` is included even though only MRKT writes here today,
-- for the same reason every other event/history table in this project
-- scopes by marketplace: a future poller's own event stream (if one
-- ever gets IDs) can share this table without a schema change.
CREATE TABLE IF NOT EXISTS processed_events (
    marketplace TEXT NOT NULL,
    event_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY(marketplace, event_id)
);

-- Tonnel cross-market verification snapshots (tonnel_client.py /
-- poller.py): one row per clean-signal cross-check against Tonnel's
-- pair floor for the same (collection, model, backdrop). Kept
-- separately from floor_snapshots -- this is a DIFFERENT marketplace's
-- data, not another Portals floor level, and a listing can be
-- cross-checked more than once over time (PRIMARY KEY includes
-- fetched_at, not just listing_external_id).
CREATE TABLE IF NOT EXISTS tonnel_floor_snapshots (
    listing_external_id TEXT NOT NULL,
    collection_name TEXT,
    model_name TEXT,
    backdrop_name TEXT,
    tonnel_floor_nano INTEGER,
    tonnel_floor_with_fee_nano INTEGER,
    tonnel_listed_count INTEGER NOT NULL DEFAULT 0,
    tonnel_status TEXT NOT NULL DEFAULT 'no_data',
    tonnel_implausible INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(listing_external_id, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_tonnel_floor_snapshots_listing
    ON tonnel_floor_snapshots(listing_external_id);

-- Правка 1/3 (two-way cross-check delivery, schema v16): the GENERIC,
-- bidirectional replacement for tonnel_floor_snapshots above (which only
-- ever recorded the Portals->Tonnel direction, one signal's marketplace
-- hardcoded into its column names). signal_marketplace/checked_marketplace
-- record WHICH direction this row is -- "portals"/"tonnel" or vice versa
-- -- so one table serves both, per spec ("не дублировать"). Kept
-- alongside tonnel_floor_snapshots (not a replacement of it -- report.py
-- still reads the old table for the pre-existing Portals->Tonnel
-- history) rather than migrating old rows, which is not possible without
-- fabricating the missing signal_marketplace/checked_marketplace values.
-- Правка 2 (MRKT third-neighbour delivery, schema v17): PRIMARY KEY
-- widened to include checked_marketplace -- with a THIRD neighbour
-- (MRKT) now checked in the SAME cross_check() call (same `now`
-- timestamp) as an existing one, two rows for the same
-- (signal_marketplace, listing_external_id, fetched_at) but DIFFERENT
-- checked_marketplace would otherwise collide on the old PK and the
-- second neighbour's snapshot would be silently dropped by ON CONFLICT
-- DO NOTHING. See _ensure_cross_check_snapshots_full_pk -- this is now
-- the FOURTH time this exact class of ON CONFLICT/PK bug has been
-- fixed in this project (price_history, listing_lifecycle,
-- floor_snapshots/alerts_sent, now this).
CREATE TABLE IF NOT EXISTS cross_check_snapshots (
    signal_marketplace TEXT NOT NULL,
    checked_marketplace TEXT NOT NULL,
    listing_external_id TEXT NOT NULL,
    collection_name TEXT,
    model_name TEXT,
    backdrop_name TEXT,
    neighbour_floor_nano INTEGER,
    neighbour_listed_count INTEGER NOT NULL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT 'no_data',
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(signal_marketplace, checked_marketplace, listing_external_id, fetched_at)
);

CREATE INDEX IF NOT EXISTS idx_cross_check_snapshots_listing
    ON cross_check_snapshots(signal_marketplace, listing_external_id);
"""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_pk_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """The table's ACTUAL PRIMARY KEY columns, in key order, straight
    from SQLite's own catalog -- not from this file's CREATE TABLE text,
    which can silently drift from what a real, migrated-over-time
    database actually has (a column added via ALTER TABLE ADD COLUMN
    does NOT join the PRIMARY KEY, no matter what a later CREATE TABLE
    string says -- see the ON CONFLICT bug this function exists to catch
    for good, README). PRAGMA table_info's `pk` field is 0 for a
    non-key column and 1,2,3... for its position in a composite key;
    this sorts by that position, not by column declaration order.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    pk_rows = [(row[5], row[1]) for row in rows if row[5] > 0]  # (pk_position, name)
    pk_rows.sort()
    return tuple(name for _pos, name in pk_rows)


def _recorded_version(conn: sqlite3.Connection) -> int | None:
    if not _table_exists(conn, "schema_version"):
        return None
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] if row and row[0] is not None else None


def _detect_version_from_columns(conn: sqlite3.Connection) -> int:
    """Used only when schema_version has no recorded row -- either a
    brand new DB (floor_snapshots doesn't exist yet, or already has the
    latest columns because SCHEMA just created it) or a pre-versioning DB
    from before this migration system existed. Never guesses: an
    unrecognized column layout raises SchemaError instead of picking a
    version blindly.
    """
    if not _table_exists(conn, "floor_snapshots"):
        return CURRENT_SCHEMA_VERSION

    cols = _table_columns(conn, "floor_snapshots")
    # v6->v7 and v7->v8 both added whole new TABLES (alerts_sent,
    # listing_lifecycle), not floor_snapshots columns; v8->v9 and v9->v10
    # each added COLUMNS on alerts_sent/listing_lifecycle; v10->v11 added
    # another new TABLE (tonnel_floor_snapshots); v11->v12 added a COLUMN
    # (tonnel_implausible) on that new table; v12->v13 added `marketplace`
    # to floor_snapshots/listing_lifecycle and widened price_history's and
    # listing_lifecycle's PRIMARY KEY; v13->v14 fixed a bug where that PK
    # widening didn't actually happen on a real, sequentially-migrated DB
    # (see _migration_13_to_14) -- distinguished here by the table's
    # ACTUAL primary key (_table_pk_columns), NOT column presence, since
    # column presence is exactly what made the original bug undetectable.
    # This branch only runs for a DB with no schema_version row at all
    # (a brand-new DB, or a pre-versioning one) -- a brand-new DB always
    # has the correct PK straight from SCHEMA, so this correctly reports
    # 14 for it; a genuinely pre-versioning DB missing the PK fix will
    # report 13 and let the normal migration path fix it on the same run.
    if "marketplace" in cols:
        if _table_pk_columns(conn, "price_history") == ("marketplace", "listing_external_id", "observed_at"):
            if (
                _table_pk_columns(conn, "floor_snapshots") == ("marketplace", "listing_external_id")
                and _table_pk_columns(conn, "alerts_sent") == ("marketplace", "listing_external_id", "observed_at")
            ):
                return 15
            return 14
        return 13
    if _table_exists(conn, "tonnel_floor_snapshots"):
        if "tonnel_implausible" in _table_columns(conn, "tonnel_floor_snapshots"):
            return 12
        return 11
    if _table_exists(conn, "listing_lifecycle") and "last_checked_at" in _table_columns(conn, "listing_lifecycle"):
        return 10
    if _table_exists(conn, "alerts_sent") and "status" in _table_columns(conn, "alerts_sent"):
        return 9
    if _table_exists(conn, "listing_lifecycle"):
        return 8
    if _table_exists(conn, "alerts_sent"):
        return 7
    if "model_floor_excl_self_nano" in cols:
        return 6
    if "pair_floor_excl_self_nano" in cols:
        return 5
    if "pair_floor_nano" in cols:
        return 3
    if "api_combo_floor_nano" in cols and "own_combo_floor_nano" in cols:
        return 2
    if "combo_floor_nano" in cols:
        return 1

    raise SchemaError(
        "Cannot determine the schema version of the existing "
        "floor_snapshots table -- it has none of the recognized column "
        "sets ('combo_floor_nano' v1, 'api_combo_floor_nano'/"
        "'own_combo_floor_nano' v2, 'pair_floor_nano' v3). "
        "This DB file was likely created by unrelated code, or its schema "
        "was hand-edited. Nothing was changed. To proceed, either point "
        "DB_DSN at a different file, or inspect the table with "
        "`PRAGMA table_info(floor_snapshots)` and decide manually whether "
        "it's safe to migrate or should be recreated."
    )


def _migration_1_to_2(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to run even if some/all target columns already
    exist (checks before each ALTER). Adds the columns introduced when
    the API combo-floor was found to be unreliable and the own-floor
    calculation was added; copies any existing combo_floor_nano values
    into api_combo_floor_nano so old snapshots aren't silently blanked.
    """
    cols_before = _table_columns(conn, "floor_snapshots")

    def add_column(name: str, decl: str) -> None:
        if name not in cols_before:
            conn.execute(f"ALTER TABLE floor_snapshots ADD COLUMN {name} {decl}")

    add_column("api_combo_floor_nano", "INTEGER")
    add_column("own_combo_floor_nano", "INTEGER")
    add_column("own_sample_size", "INTEGER NOT NULL DEFAULT 0")
    add_column("own_confidence", "TEXT NOT NULL DEFAULT 'none'")
    add_column("floor_sanity", "TEXT NOT NULL DEFAULT 'no_data'")

    if "combo_floor_nano" in cols_before and "api_combo_floor_nano" not in cols_before:
        conn.execute(
            "UPDATE floor_snapshots SET api_combo_floor_nano = combo_floor_nano "
            "WHERE api_combo_floor_nano IS NULL"
        )


def _migration_2_to_3(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds the pair-floor columns -- the direct, filtered
    /nfts/search query that replaced both api_combo_floor_nano and
    own_combo_floor_nano as the source of truth for discount/profit.
    Nothing to backfill: there is no prior column this data came from.
    """
    cols_before = _table_columns(conn, "floor_snapshots")

    def add_column(name: str, decl: str) -> None:
        if name not in cols_before:
            conn.execute(f"ALTER TABLE floor_snapshots ADD COLUMN {name} {decl}")

    add_column("pair_floor_nano", "INTEGER")
    add_column("pair_listed_count", "INTEGER NOT NULL DEFAULT 0")
    add_column("pair_floor_status", "TEXT NOT NULL DEFAULT 'no_data'")
    add_column("pair_floor_age_sec", "INTEGER NOT NULL DEFAULT 0")


def _migration_3_to_4(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds the price_history table (and its indexes) --
    tracking real price changes on already-known listings, which
    dedup previously discarded entirely. Nothing to backfill: there is
    no prior column/table this data came from.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_history (
            listing_external_id TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            old_price_nano INTEGER NOT NULL,
            new_price_nano INTEGER NOT NULL,
            delta_pct NUMERIC NOT NULL,
            is_noise INTEGER NOT NULL DEFAULT 0,
            old_listed_at TEXT,
            new_listed_at TEXT,
            observed_at TEXT NOT NULL,
            PRIMARY KEY(listing_external_id, observed_at)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_history_listing_observed "
        "ON price_history(listing_external_id, observed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_history_observed_at ON price_history(observed_at)"
    )


def _migration_4_to_5(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds the self-exclusion floor columns to
    floor_snapshots, and the drop-time-floor / ladder / anomaly columns
    to price_history. Nothing to backfill for either -- these are all
    newly-computed values with no prior column they came from.
    """
    floor_cols_before = _table_columns(conn, "floor_snapshots")

    def add_floor_column(name: str, decl: str) -> None:
        if name not in floor_cols_before:
            conn.execute(f"ALTER TABLE floor_snapshots ADD COLUMN {name} {decl}")

    add_floor_column("pair_floor_excl_self_nano", "INTEGER")
    add_floor_column("pair_listed_count_excl_self", "INTEGER NOT NULL DEFAULT 0")
    add_floor_column("pair_self_was_floor", "INTEGER NOT NULL DEFAULT 0")

    if _table_exists(conn, "price_history"):
        history_cols_before = _table_columns(conn, "price_history")

        def add_history_column(name: str, decl: str) -> None:
            if name not in history_cols_before:
                conn.execute(f"ALTER TABLE price_history ADD COLUMN {name} {decl}")

        add_history_column("floor_at_drop_nano", "INTEGER")
        add_history_column("floor_listed_count_at_drop", "INTEGER")
        add_history_column("floor_fetched_at", "TEXT")
        add_history_column("is_ladder", "INTEGER NOT NULL DEFAULT 0")
        add_history_column("is_anomaly", "INTEGER NOT NULL DEFAULT 0")


def _migration_5_to_6(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds the model-level fallback floor columns to
    floor_snapshots, and floor_level_at_drop to price_history. Nothing to
    backfill for either -- newly-computed values with no prior column.
    """
    floor_cols_before = _table_columns(conn, "floor_snapshots")

    def add_floor_column(name: str, decl: str) -> None:
        if name not in floor_cols_before:
            conn.execute(f"ALTER TABLE floor_snapshots ADD COLUMN {name} {decl}")

    add_floor_column("model_floor_excl_self_nano", "INTEGER")
    add_floor_column("model_listed_count_excl_self", "INTEGER NOT NULL DEFAULT 0")
    add_floor_column("model_floor_status", "TEXT NOT NULL DEFAULT 'no_data'")

    if _table_exists(conn, "price_history"):
        history_cols_before = _table_columns(conn, "price_history")
        if "floor_level_at_drop" not in history_cols_before:
            conn.execute("ALTER TABLE price_history ADD COLUMN floor_level_at_drop TEXT")


def _migration_6_to_7(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds the alerts_sent anti-duplicate ledger for the
    Telegram notifier -- a new table, nothing to backfill (a listing
    can't have been "sent" by a notifier that didn't exist yet).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts_sent (
            listing_external_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY(listing_external_id, observed_at)
        )
        """
    )


def _migration_7_to_8(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds listing_lifecycle -- a new table, nothing to
    backfill (lifecycle wasn't tracked before this delivery, so there is
    no prior data to migrate into it; it starts populating from the next
    poll cycle onward).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS listing_lifecycle (
            listing_external_id TEXT PRIMARY KEY,
            collection_id TEXT,
            model_name TEXT,
            backdrop_name TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            disappeared_at TEXT,
            last_price_nano INTEGER,
            reappeared_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_lifecycle_pair "
        "ON listing_lifecycle(collection_id, model_name, backdrop_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_lifecycle_disappeared_at "
        "ON listing_lifecycle(disappeared_at)"
    )


def _migration_8_to_9(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds alerts_sent.status ('sent' | 'skipped_stale') --
    the pre-send freshness check (poller.py Правка 3). Existing rows
    predate this check and were genuinely sent, so they default to
    'sent' via the column default -- no explicit backfill needed.
    """
    cols_before = _table_columns(conn, "alerts_sent")
    if "status" not in cols_before:
        conn.execute("ALTER TABLE alerts_sent ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'")


def _migration_9_to_10(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds listing_lifecycle.last_checked_at and
    final_status -- the API-status-check mechanism that replaces the
    retired time-since-last-seen rule. Schema-only: does NOT reset any
    existing disappeared_at/reappeared_count data (that data predates
    this fix and is confirmed unreliable -- see README) -- use the
    separate, explicit gift_sniper/lifecycle_reset.py script for that,
    never silently as part of a migration.
    """
    cols_before = _table_columns(conn, "listing_lifecycle")

    def add_column(name: str, decl: str) -> None:
        if name not in cols_before:
            conn.execute(f"ALTER TABLE listing_lifecycle ADD COLUMN {name} {decl}")

    add_column("last_checked_at", "TEXT")
    add_column("final_status", "TEXT")


def _migration_10_to_11(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds tonnel_floor_snapshots -- a new table, nothing to
    backfill (Tonnel cross-checking didn't exist before this delivery).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tonnel_floor_snapshots (
            listing_external_id TEXT NOT NULL,
            collection_name TEXT,
            model_name TEXT,
            backdrop_name TEXT,
            tonnel_floor_nano INTEGER,
            tonnel_floor_with_fee_nano INTEGER,
            tonnel_listed_count INTEGER NOT NULL DEFAULT 0,
            tonnel_status TEXT NOT NULL DEFAULT 'no_data',
            fetched_at TEXT NOT NULL,
            PRIMARY KEY(listing_external_id, fetched_at)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tonnel_floor_snapshots_listing "
        "ON tonnel_floor_snapshots(listing_external_id)"
    )


def _migration_11_to_12(conn: sqlite3.Connection) -> None:
    """Idempotent. Adds tonnel_floor_snapshots.tonnel_implausible
    (Правка 3, TONNEL_MAX_RATIO) -- defaults to 0 (not implausible) for
    every pre-existing row, since the flag didn't exist before this
    delivery and there's nothing to retroactively recompute it from
    without re-querying Tonnel.
    """
    cols = _table_columns(conn, "tonnel_floor_snapshots")
    if "tonnel_implausible" not in cols:
        conn.execute(
            "ALTER TABLE tonnel_floor_snapshots ADD COLUMN tonnel_implausible INTEGER NOT NULL DEFAULT 0"
        )


def _migration_12_to_13(conn: sqlite3.Connection) -> None:
    """Idempotent. Правка 1 (Tonnel full-collector delivery): every
    pre-existing row belongs to 'portals' -- that was the only source
    before this delivery, so the backfill is unconditional and exact, not
    a guess.

    floor_snapshots: a plain ADD COLUMN (marketplace, default 'portals')
    -- the PRIMARY KEY stays listing_external_id alone, since Tonnel does
    not write to this table (see the SCHEMA comment on the table).

    price_history and listing_lifecycle: ADD COLUMN is not enough --
    their PRIMARY KEY must widen to include marketplace, since Tonnel
    DOES write to both, and SQLite cannot ALTER a PRIMARY KEY in place,
    so both are rebuilt (create the new-shape table, copy rows, drop the
    old one, rename) -- the standard SQLite pattern for a PK change.

    BUG FIXED HERE (previously shipped, caught in production -- see
    README/_migration_13_to_14): the rebuild conditions below originally
    checked "marketplace" not in _table_columns(...), but price_history
    has had a `marketplace` COLUMN since _migration_3_to_4 -- years
    before this delivery -- it was just never part of the PRIMARY KEY.
    On any real DB migrated sequentially from an early version, that
    column-presence check was always False, so the rebuild branch never
    ran and the PK stayed (listing_external_id, observed_at) -- exactly
    what record_price_change's ON CONFLICT(marketplace,
    listing_external_id, observed_at) does NOT match, raising
    "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE
    constraint" on the very first Tonnel-or-mixed write. Fixed by
    checking the table's ACTUAL primary key shape (_table_pk_columns),
    which is what the ON CONFLICT clause actually has to match, instead
    of merely checking whether a same-named column happens to exist.
    """
    if "marketplace" not in _table_columns(conn, "floor_snapshots"):
        conn.execute("ALTER TABLE floor_snapshots ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'portals'")

    _ensure_price_history_marketplace_pk(conn)
    _ensure_listing_lifecycle_marketplace_pk(conn)


def _ensure_price_history_marketplace_pk(conn: sqlite3.Connection, suffix: str = "v13") -> None:
    """Idempotent, checked against the table's ACTUAL primary key (see
    _table_pk_columns) -- not column presence, which is what let this bug
    ship in the first place (see _migration_12_to_13's docstring). No-op
    if the PK is already correct. `suffix` only picks the temp table's
    name (must not collide with a name already in use in the same
    connection) -- callers running the identical fix from two different
    migration numbers just pass a different suffix.
    """
    if _table_pk_columns(conn, "price_history") == ("marketplace", "listing_external_id", "observed_at"):
        return

    has_marketplace = "marketplace" in _table_columns(conn, "price_history")
    marketplace_select = "marketplace" if has_marketplace else "'portals'"
    temp_table = f"price_history_{suffix}"

    conn.execute(
        f"""
        CREATE TABLE {temp_table} (
            listing_external_id TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            old_price_nano INTEGER NOT NULL,
            new_price_nano INTEGER NOT NULL,
            delta_pct NUMERIC NOT NULL,
            is_noise INTEGER NOT NULL DEFAULT 0,
            old_listed_at TEXT,
            new_listed_at TEXT,
            observed_at TEXT NOT NULL,
            floor_at_drop_nano INTEGER,
            floor_listed_count_at_drop INTEGER,
            floor_fetched_at TEXT,
            floor_level_at_drop TEXT,
            is_ladder INTEGER NOT NULL DEFAULT 0,
            is_anomaly INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(marketplace, listing_external_id, observed_at)
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO {temp_table} (
            listing_external_id, marketplace, old_price_nano, new_price_nano,
            delta_pct, is_noise, old_listed_at, new_listed_at, observed_at,
            floor_at_drop_nano, floor_listed_count_at_drop, floor_fetched_at,
            floor_level_at_drop, is_ladder, is_anomaly
        )
        SELECT
            listing_external_id, {marketplace_select}, old_price_nano, new_price_nano,
            delta_pct, is_noise, old_listed_at, new_listed_at, observed_at,
            floor_at_drop_nano, floor_listed_count_at_drop, floor_fetched_at,
            floor_level_at_drop, is_ladder, is_anomaly
        FROM price_history
        """
    )
    conn.execute("DROP TABLE price_history")
    conn.execute(f"ALTER TABLE {temp_table} RENAME TO price_history")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_history_listing_observed "
        "ON price_history(listing_external_id, observed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_price_history_observed_at ON price_history(observed_at)"
    )


def _ensure_listing_lifecycle_marketplace_pk(conn: sqlite3.Connection, suffix: str = "v13") -> None:
    """Idempotent, checked against the table's ACTUAL primary key --
    same discipline as _ensure_price_history_marketplace_pk. No-op if
    the PK is already correct.
    """
    if _table_pk_columns(conn, "listing_lifecycle") == ("marketplace", "listing_external_id"):
        return

    has_marketplace = "marketplace" in _table_columns(conn, "listing_lifecycle")
    marketplace_select = "marketplace" if has_marketplace else "'portals'"
    temp_table = f"listing_lifecycle_{suffix}"

    conn.execute(
        f"""
        CREATE TABLE {temp_table} (
            marketplace TEXT NOT NULL DEFAULT 'portals',
            listing_external_id TEXT NOT NULL,
            collection_id TEXT,
            model_name TEXT,
            backdrop_name TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            disappeared_at TEXT,
            last_price_nano INTEGER,
            reappeared_count INTEGER NOT NULL DEFAULT 0,
            last_checked_at TEXT,
            final_status TEXT,
            PRIMARY KEY(marketplace, listing_external_id)
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO {temp_table} (
            marketplace, listing_external_id, collection_id, model_name, backdrop_name,
            first_seen_at, last_seen_at, disappeared_at, last_price_nano,
            reappeared_count, last_checked_at, final_status
        )
        SELECT
            {marketplace_select}, listing_external_id, collection_id, model_name, backdrop_name,
            first_seen_at, last_seen_at, disappeared_at, last_price_nano,
            reappeared_count, last_checked_at, final_status
        FROM listing_lifecycle
        """
    )
    conn.execute("DROP TABLE listing_lifecycle")
    conn.execute(f"ALTER TABLE {temp_table} RENAME TO listing_lifecycle")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_lifecycle_pair "
        "ON listing_lifecycle(collection_id, model_name, backdrop_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_lifecycle_disappeared_at "
        "ON listing_lifecycle(disappeared_at)"
    )


def _migration_13_to_14(conn: sqlite3.Connection) -> None:
    """Fixes an already-shipped production bug: on any real DB migrated
    sequentially from an early version, _migration_12_to_13's rebuild of
    price_history never ran (see its docstring) -- schema_version
    recorded 13, but the PRIMARY KEY was still the pre-v13 shape
    (listing_external_id, observed_at), missing marketplace entirely.
    record_price_change's ON CONFLICT(marketplace, listing_external_id,
    observed_at) does not match that key, so EVERY call raised
    "ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE
    constraint" -- both poller.py and tonnel_poller.py crashed on their
    first price change of any kind.

    This migration re-checks (and, if still wrong, rebuilds) BOTH
    price_history and listing_lifecycle against their ACTUAL primary
    key, using the exact same idempotent, data-preserving logic as the
    now-fixed _migration_12_to_13 (see _ensure_price_history_marketplace_pk
    / _ensure_listing_lifecycle_marketplace_pk) -- safe to run whether or
    not v12->v13 already got it right: a table whose PK already matches
    is left untouched.
    """
    _ensure_price_history_marketplace_pk(conn, suffix="v14")
    _ensure_listing_lifecycle_marketplace_pk(conn, suffix="v14")


def _ensure_floor_snapshots_marketplace_pk(conn: sqlite3.Connection, suffix: str = "v15") -> None:
    """Idempotent, checked against the table's ACTUAL primary key -- same
    discipline as _ensure_price_history_marketplace_pk. No-op if the PK
    already matches. Widens floor_snapshots from its single-column PK
    (listing_external_id) to (marketplace, listing_external_id) -- Tonnel
    genuinely writes here now (Правка 2, tonnel_poller.py's at-drop pair
    floor check, see db.upsert_tonnel_pair_floor_snapshot), unlike the
    prior delivery that deliberately kept this table Portals-only.
    """
    if _table_pk_columns(conn, "floor_snapshots") == ("marketplace", "listing_external_id"):
        return

    has_marketplace = "marketplace" in _table_columns(conn, "floor_snapshots")
    marketplace_select = "marketplace" if has_marketplace else "'portals'"
    temp_table = f"floor_snapshots_{suffix}"

    conn.execute(
        f"""
        CREATE TABLE {temp_table} (
            listing_external_id TEXT NOT NULL,
            marketplace TEXT NOT NULL DEFAULT 'portals',
            model_name TEXT NOT NULL,
            backdrop_name TEXT,
            api_combo_floor_nano INTEGER,
            model_min_floor_nano INTEGER,
            floor_fetched_at TEXT NOT NULL,
            floor_age_sec INTEGER NOT NULL,
            raw_model_block TEXT NOT NULL,
            name_collision INTEGER NOT NULL DEFAULT 0,
            floor_skip_reason TEXT,
            own_combo_floor_nano INTEGER,
            own_sample_size INTEGER NOT NULL DEFAULT 0,
            own_confidence TEXT NOT NULL DEFAULT 'none',
            floor_sanity TEXT NOT NULL DEFAULT 'no_data',
            pair_floor_nano INTEGER,
            pair_listed_count INTEGER NOT NULL DEFAULT 0,
            pair_floor_status TEXT NOT NULL DEFAULT 'no_data',
            pair_floor_age_sec INTEGER NOT NULL DEFAULT 0,
            pair_floor_excl_self_nano INTEGER,
            pair_listed_count_excl_self INTEGER NOT NULL DEFAULT 0,
            pair_self_was_floor INTEGER NOT NULL DEFAULT 0,
            model_floor_excl_self_nano INTEGER,
            model_listed_count_excl_self INTEGER NOT NULL DEFAULT 0,
            model_floor_status TEXT NOT NULL DEFAULT 'no_data',
            PRIMARY KEY(marketplace, listing_external_id)
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO {temp_table} (
            listing_external_id, marketplace, model_name, backdrop_name,
            api_combo_floor_nano, model_min_floor_nano, floor_fetched_at,
            floor_age_sec, raw_model_block, name_collision, floor_skip_reason,
            own_combo_floor_nano, own_sample_size, own_confidence, floor_sanity,
            pair_floor_nano, pair_listed_count, pair_floor_status, pair_floor_age_sec,
            pair_floor_excl_self_nano, pair_listed_count_excl_self, pair_self_was_floor,
            model_floor_excl_self_nano, model_listed_count_excl_self, model_floor_status
        )
        SELECT
            listing_external_id, {marketplace_select}, model_name, backdrop_name,
            api_combo_floor_nano, model_min_floor_nano, floor_fetched_at,
            floor_age_sec, raw_model_block, name_collision, floor_skip_reason,
            own_combo_floor_nano, own_sample_size, own_confidence, floor_sanity,
            pair_floor_nano, pair_listed_count, pair_floor_status, pair_floor_age_sec,
            pair_floor_excl_self_nano, pair_listed_count_excl_self, pair_self_was_floor,
            model_floor_excl_self_nano, model_listed_count_excl_self, model_floor_status
        FROM floor_snapshots
        """
    )
    conn.execute("DROP TABLE floor_snapshots")
    conn.execute(f"ALTER TABLE {temp_table} RENAME TO floor_snapshots")


def _ensure_alerts_sent_marketplace_pk(conn: sqlite3.Connection, suffix: str = "v15") -> None:
    """Idempotent, checked against the table's ACTUAL primary key -- same
    discipline as _ensure_price_history_marketplace_pk. No-op if the PK
    already matches. Widens alerts_sent from (listing_external_id,
    observed_at) to (marketplace, listing_external_id, observed_at) --
    Tonnel now sends notifications too (Правка 4).
    """
    if _table_pk_columns(conn, "alerts_sent") == ("marketplace", "listing_external_id", "observed_at"):
        return

    has_marketplace = "marketplace" in _table_columns(conn, "alerts_sent")
    marketplace_select = "marketplace" if has_marketplace else "'portals'"
    temp_table = f"alerts_sent_{suffix}"

    conn.execute(
        f"""
        CREATE TABLE {temp_table} (
            marketplace TEXT NOT NULL DEFAULT 'portals',
            listing_external_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            PRIMARY KEY(marketplace, listing_external_id, observed_at)
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO {temp_table} (marketplace, listing_external_id, observed_at, sent_at, status)
        SELECT {marketplace_select}, listing_external_id, observed_at, sent_at, status
        FROM alerts_sent
        """
    )
    conn.execute("DROP TABLE alerts_sent")
    conn.execute(f"ALTER TABLE {temp_table} RENAME TO alerts_sent")


def _migration_14_to_15(conn: sqlite3.Connection) -> None:
    """Правка 1/2/4 (Tonnel signals delivery): Tonnel now writes to
    floor_snapshots (at-drop pair floor, Правка 2) and alerts_sent
    (notifications, Правка 4) -- both tables' PRIMARY KEY widens to
    include marketplace, same idempotent rebuild discipline as
    price_history/listing_lifecycle (see _migration_12_to_13/
    _migration_13_to_14 -- this is now the THIRD time this exact class
    of fix has been needed, hence the general class-level test in
    test_migrations.py rather than another one-off).
    """
    _ensure_floor_snapshots_marketplace_pk(conn)
    _ensure_alerts_sent_marketplace_pk(conn)


def _ensure_cross_check_snapshots_full_pk(conn: sqlite3.Connection, suffix: str = "v17") -> None:
    """Idempotent, checked against the table's ACTUAL primary key -- same
    discipline as _ensure_alerts_sent_marketplace_pk. No-op if the PK
    already matches. Widens cross_check_snapshots from
    (signal_marketplace, listing_external_id, fetched_at) to
    (signal_marketplace, checked_marketplace, listing_external_id,
    fetched_at) -- a signal can now be checked against MULTIPLE
    neighbours (Tonnel/Portals AND MRKT) in one cross_check() call, all
    sharing the same `fetched_at`, so checked_marketplace must be part
    of the key or the second neighbour's row collides with the first's
    under ON CONFLICT DO NOTHING.
    """
    if _table_pk_columns(conn, "cross_check_snapshots") == (
        "signal_marketplace", "checked_marketplace", "listing_external_id", "fetched_at",
    ):
        return
    if not _table_exists(conn, "cross_check_snapshots"):
        return  # nothing to migrate -- SCHEMA will create it with the correct PK directly

    temp_table = f"cross_check_snapshots_{suffix}"
    conn.execute(
        f"""
        CREATE TABLE {temp_table} (
            signal_marketplace TEXT NOT NULL,
            checked_marketplace TEXT NOT NULL,
            listing_external_id TEXT NOT NULL,
            collection_name TEXT,
            model_name TEXT,
            backdrop_name TEXT,
            neighbour_floor_nano INTEGER,
            neighbour_listed_count INTEGER NOT NULL DEFAULT 0,
            verdict TEXT NOT NULL DEFAULT 'no_data',
            fetched_at TEXT NOT NULL,
            PRIMARY KEY(signal_marketplace, checked_marketplace, listing_external_id, fetched_at)
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO {temp_table} (
            signal_marketplace, checked_marketplace, listing_external_id,
            collection_name, model_name, backdrop_name,
            neighbour_floor_nano, neighbour_listed_count, verdict, fetched_at
        )
        SELECT
            signal_marketplace, checked_marketplace, listing_external_id,
            collection_name, model_name, backdrop_name,
            neighbour_floor_nano, neighbour_listed_count, verdict, fetched_at
        FROM cross_check_snapshots
        """
    )
    conn.execute("DROP TABLE cross_check_snapshots")
    conn.execute(f"ALTER TABLE {temp_table} RENAME TO cross_check_snapshots")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cross_check_snapshots_listing "
        "ON cross_check_snapshots(signal_marketplace, listing_external_id)"
    )


def _migration_18_to_19(conn: sqlite3.Connection) -> None:
    """ПРАВКА 1 (sale-vs-floor delivery): adds listing_lifecycle.
    floor_at_sale_nano/floor_listed_count_at_sale/floor_fetched_at_sale --
    the pair floor at the moment of an MRKT sale, self-excluded, so
    sale_vs_floor.py can measure how sold_price_nano actually compares to
    the floor the profit formula assumes it does (see README).
    """
    with conn:
        cols = _table_columns(conn, "listing_lifecycle")
        if "floor_at_sale_nano" not in cols:
            conn.execute("ALTER TABLE listing_lifecycle ADD COLUMN floor_at_sale_nano INTEGER")
        if "floor_listed_count_at_sale" not in cols:
            conn.execute("ALTER TABLE listing_lifecycle ADD COLUMN floor_listed_count_at_sale INTEGER")
        if "floor_fetched_at_sale" not in cols:
            conn.execute("ALTER TABLE listing_lifecycle ADD COLUMN floor_fetched_at_sale TEXT")


def _migration_17_to_18(conn: sqlite3.Connection) -> None:
    """Правка 2 (MRKT full-signaller delivery): adds processed_events
    (event-level dedup for mrkt_poller.py's feed) and
    listing_lifecycle.sold_price_nano (MRKT's "sale" event is the only
    marketplace signal in this project confirming an actual sale, not
    just a bare disappearance -- see README).
    """
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_events (
                marketplace TEXT NOT NULL,
                event_id TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY(marketplace, event_id)
            )
            """
        )
        if "sold_price_nano" not in _table_columns(conn, "listing_lifecycle"):
            conn.execute("ALTER TABLE listing_lifecycle ADD COLUMN sold_price_nano INTEGER")


def _migration_16_to_17(conn: sqlite3.Connection) -> None:
    """Правка 2 (MRKT third-neighbour delivery): widens
    cross_check_snapshots' PRIMARY KEY -- see
    _ensure_cross_check_snapshots_full_pk.
    """
    _ensure_cross_check_snapshots_full_pk(conn)


def _migration_15_to_16(conn: sqlite3.Connection) -> None:
    """Правка 1/3 (two-way cross-check delivery): adds cross_check_snapshots
    -- a new table, nothing to migrate from an existing one (SCHEMA's
    CREATE TABLE IF NOT EXISTS would create it anyway, but every other
    table-addition migration in this file does this explicitly too, so
    schema_version stays an accurate, auditable record of what changed
    when).
    """
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cross_check_snapshots (
                signal_marketplace TEXT NOT NULL,
                checked_marketplace TEXT NOT NULL,
                listing_external_id TEXT NOT NULL,
                collection_name TEXT,
                model_name TEXT,
                backdrop_name TEXT,
                neighbour_floor_nano INTEGER,
                neighbour_listed_count INTEGER NOT NULL DEFAULT 0,
                verdict TEXT NOT NULL DEFAULT 'no_data',
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(signal_marketplace, listing_external_id, fetched_at)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cross_check_snapshots_listing "
            "ON cross_check_snapshots(signal_marketplace, listing_external_id)"
        )


def _migration_19_to_20(conn: sqlite3.Connection) -> None:
    """Index for signals._floor_instability (the unstable_floor stage).
    Measured 2026-09-19: without it each query scanned floor_snapshots
    (0.105 s), the cascade ran it ~2400 times per notify, and one Portals
    poll cycle took 306 s instead of ~10 s. With it: 0.0001 s, identical
    results on 30 random pairs. Index only, no data change."""
    with conn:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_floor_snapshots_pair_time "
            "ON floor_snapshots(marketplace, model_name, backdrop_name, floor_fetched_at)"
        )


# Ordered list of (target_version, migration_fn). Each fn brings the DB
# from target_version - 1 to target_version and must be idempotent.
MIGRATIONS: list[tuple[int, "callable"]] = [
    (2, _migration_1_to_2),
    (3, _migration_2_to_3),
    (4, _migration_3_to_4),
    (5, _migration_4_to_5),
    (6, _migration_5_to_6),
    (7, _migration_6_to_7),
    (8, _migration_7_to_8),
    (9, _migration_8_to_9),
    (10, _migration_9_to_10),
    (11, _migration_10_to_11),
    (12, _migration_11_to_12),
    (13, _migration_12_to_13),
    (14, _migration_13_to_14),
    (15, _migration_14_to_15),
    (16, _migration_15_to_16),
    (17, _migration_16_to_17),
    (18, _migration_17_to_18),
    (19, _migration_18_to_19),
    (20, _migration_19_to_20),
]


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    with conn:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )


def _migrate(conn: sqlite3.Connection) -> None:
    version = _recorded_version(conn)
    if version is None:
        version = _detect_version_from_columns(conn)
        _record_version(conn, version)

    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaError(
            f"DB schema version {version} is newer than this code supports "
            f"({CURRENT_SCHEMA_VERSION}). Refusing to touch it -- you're "
            f"likely running older code against a DB written by a newer "
            f"version. Update the code, or point DB_DSN at a different file."
        )

    for target_version, migration_fn in MIGRATIONS:
        if version < target_version:
            migration_fn(conn)
            version = target_version
            _record_version(conn, version)


# Columns db.upsert_listing_with_floor / report.py / poller.py actually
# read or write, per table. Used by verify_schema() as a defensive check
# BEFORE any network call -- if migration silently failed to produce the
# expected shape, fail here with a clear message, not mid-batch-write.
EXPECTED_COLUMNS: dict[str, set[str]] = {
    "listings": {
        "marketplace", "external_id", "tg_id", "collection_id", "collection_name",
        "gift_number", "price_nano", "currency", "collection_floor_nano",
        "model_name", "symbol_name", "backdrop_name",
        "model_rarity_raw", "symbol_rarity_raw", "backdrop_rarity_raw",
        "image_url", "animation_url", "listed_at", "unlocks_at", "status",
        "first_seen_at", "raw",
    },
    "floor_snapshots": {
        "listing_external_id", "marketplace", "model_name", "backdrop_name",
        "api_combo_floor_nano", "model_min_floor_nano", "floor_fetched_at",
        "floor_age_sec", "raw_model_block", "name_collision", "floor_skip_reason",
        "own_combo_floor_nano", "own_sample_size", "own_confidence", "floor_sanity",
        "pair_floor_nano", "pair_listed_count", "pair_floor_status", "pair_floor_age_sec",
        "pair_floor_excl_self_nano", "pair_listed_count_excl_self", "pair_self_was_floor",
        "model_floor_excl_self_nano", "model_listed_count_excl_self", "model_floor_status",
    },
    "market_config_snapshots": {
        "fetched_at", "raw", "commission", "offer_fee", "withdrawal_fee",
        "user_cashback", "usd_course",
    },
    "price_history": {
        "listing_external_id", "marketplace", "old_price_nano", "new_price_nano",
        "delta_pct", "is_noise", "old_listed_at", "new_listed_at", "observed_at",
        "floor_at_drop_nano", "floor_listed_count_at_drop", "floor_fetched_at",
        "is_ladder", "is_anomaly", "floor_level_at_drop",
    },
    "alerts_sent": {
        "marketplace", "listing_external_id", "observed_at", "sent_at", "status",
    },
    "listing_lifecycle": {
        "marketplace", "listing_external_id", "collection_id", "model_name", "backdrop_name",
        "first_seen_at", "last_seen_at", "disappeared_at", "last_price_nano",
        "reappeared_count", "last_checked_at", "final_status", "sold_price_nano",
        "floor_at_sale_nano", "floor_listed_count_at_sale", "floor_fetched_at_sale",
    },
    "tonnel_floor_snapshots": {
        "listing_external_id", "collection_name", "model_name", "backdrop_name",
        "tonnel_floor_nano", "tonnel_floor_with_fee_nano", "tonnel_listed_count",
        "tonnel_status", "tonnel_implausible", "fetched_at",
    },
    "cross_check_snapshots": {
        "signal_marketplace", "checked_marketplace", "listing_external_id",
        "collection_name", "model_name", "backdrop_name",
        "neighbour_floor_nano", "neighbour_listed_count", "verdict", "fetched_at",
    },
    "processed_events": {"marketplace", "event_id", "processed_at"},
}


def verify_schema(conn: sqlite3.Connection) -> None:
    """Call before the first network request. Raises SchemaError with an
    actionable message if any expected table/column is missing --
    catching a stale DB here means the poller stops cleanly before
    touching the API, instead of dying mid-write on the first batch.
    """
    for table, expected_cols in EXPECTED_COLUMNS.items():
        if not _table_exists(conn, table):
            raise SchemaError(
                f"Table '{table}' does not exist in this DB. Expected it to be "
                f"created by db.connect() -- check DB_DSN points at the right "
                f"file, or delete it to let a fresh DB be created."
            )
        actual_cols = _table_columns(conn, table)
        missing = expected_cols - actual_cols
        if missing:
            raise SchemaError(
                f"Table '{table}' is missing column(s) {sorted(missing)}. "
                f"This DB was likely created by an older version of this code "
                f"and migration did not complete. db.connect() runs migrations "
                f"automatically -- if you're seeing this, either re-run it, or "
                f"inspect 'schema_version' and 'PRAGMA table_info({table})' "
                f"manually before proceeding."
            )


def connect(dsn: str) -> sqlite3.Connection:
    conn = sqlite3.connect(dsn)
    conn.execute("PRAGMA foreign_keys = ON")
    # Правка 1/2: two independent writer processes (poller.py/Portals,
    # tonnel_poller.py/Tonnel) share one SQLite file -- WAL lets readers
    # run without blocking the writer (and vice versa), and busy_timeout
    # makes a writer that finds the DB locked WAIT instead of raising
    # "database is locked" immediately (SQLite's default is to give up
    # right away). journal_mode is stored IN THE DB FILE itself, so this
    # only needs to run once per connection, not once ever -- but it's
    # cheap and idempotent, so no special-casing for ":memory:" (which
    # silently ignores WAL and stays in its default mode; that's fine,
    # it's a single-process test DB, never contended).
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {config.SQLITE_BUSY_TIMEOUT_MS}")
    # schema_version must exist for _migrate() to record into, but
    # nothing else is created yet -- _migrate() runs BEFORE the rest of
    # SCHEMA, so version detection and column-migrations see the TRUE old
    # state of any pre-existing tables. Running SCHEMA's full
    # CREATE TABLE IF NOT EXISTS first would create brand-new tables
    # (like price_history) ahead of detection, which would make a legacy
    # pre-versioning DB look further along than it really is.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


def listing_exists(conn: sqlite3.Connection, marketplace: str, external_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM listings WHERE marketplace = ? AND external_id = ?",
        (marketplace, external_id),
    ).fetchone()
    return row is not None


def insert_listing(conn: sqlite3.Connection, listing: Listing) -> None:
    """Writes ONLY a `listings` row -- no floor_snapshots row, unlike
    upsert_listing_with_floor(). Used by tonnel_poller.py: Tonnel has no
    equivalent of the /collections/models/backgrounds/floors +
    /nfts/search-based floor analytics path (its own pair floor is
    computed live via tonnel_client.pair_floor(), never stored per
    listing, see README), so writing a floor_snapshots row for a Tonnel
    listing would just be a permanently-pending, never-processed row.
    Idempotent: re-inserting the same (marketplace, external_id) is a
    no-op, same discipline as upsert_listing_with_floor().
    """
    with conn:
        conn.execute(
            """
            INSERT INTO listings (
                marketplace, external_id, tg_id, collection_id, collection_name,
                gift_number, price_nano, currency, collection_floor_nano,
                model_name, symbol_name, backdrop_name,
                model_rarity_raw, symbol_rarity_raw, backdrop_rarity_raw,
                image_url, animation_url, listed_at, unlocks_at, status,
                first_seen_at, raw
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(marketplace, external_id) DO NOTHING
            """,
            (
                listing.marketplace,
                listing.external_id,
                listing.tg_id,
                listing.collection_id,
                listing.collection_name,
                listing.gift_number,
                listing.price_nano,
                listing.currency,
                listing.collection_floor_nano,
                listing.model_name,
                listing.symbol_name,
                listing.backdrop_name,
                str(listing.model_rarity_raw) if listing.model_rarity_raw is not None else None,
                str(listing.symbol_rarity_raw) if listing.symbol_rarity_raw is not None else None,
                str(listing.backdrop_rarity_raw) if listing.backdrop_rarity_raw is not None else None,
                listing.image_url,
                listing.animation_url,
                _iso(listing.listed_at),
                _iso(listing.unlocks_at),
                listing.status,
                _iso(listing.first_seen_at),
                json.dumps(listing.raw) if listing.raw is not None else None,
            ),
        )


def upsert_listing_with_floor(
    conn: sqlite3.Connection, listing: Listing, snapshot: FloorSnapshot
) -> None:
    """Writes a listing and its floor snapshot in one transaction.
    Idempotent: re-inserting the same (marketplace, external_id) is a no-op
    on the listings row and does not create a duplicate.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO listings (
                marketplace, external_id, tg_id, collection_id, collection_name,
                gift_number, price_nano, currency, collection_floor_nano,
                model_name, symbol_name, backdrop_name,
                model_rarity_raw, symbol_rarity_raw, backdrop_rarity_raw,
                image_url, animation_url, listed_at, unlocks_at, status,
                first_seen_at, raw
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(marketplace, external_id) DO NOTHING
            """,
            (
                listing.marketplace,
                listing.external_id,
                listing.tg_id,
                listing.collection_id,
                listing.collection_name,
                listing.gift_number,
                listing.price_nano,
                listing.currency,
                listing.collection_floor_nano,
                listing.model_name,
                listing.symbol_name,
                listing.backdrop_name,
                str(listing.model_rarity_raw) if listing.model_rarity_raw is not None else None,
                str(listing.symbol_rarity_raw) if listing.symbol_rarity_raw is not None else None,
                str(listing.backdrop_rarity_raw) if listing.backdrop_rarity_raw is not None else None,
                listing.image_url,
                listing.animation_url,
                _iso(listing.listed_at),
                _iso(listing.unlocks_at),
                listing.status,
                _iso(listing.first_seen_at),
                json.dumps(listing.raw) if listing.raw is not None else None,
            ),
        )
        conn.execute(
            """
            INSERT INTO floor_snapshots (
                listing_external_id, marketplace, model_name, backdrop_name,
                api_combo_floor_nano, model_min_floor_nano, floor_fetched_at,
                floor_age_sec, raw_model_block, name_collision, floor_skip_reason,
                own_combo_floor_nano, own_sample_size, own_confidence, floor_sanity,
                pair_floor_nano, pair_listed_count, pair_floor_status, pair_floor_age_sec,
                pair_floor_excl_self_nano, pair_listed_count_excl_self, pair_self_was_floor,
                model_floor_excl_self_nano, model_listed_count_excl_self, model_floor_status
            ) VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(marketplace, listing_external_id) DO NOTHING
            """,
            (
                snapshot.listing_external_id,
                listing.marketplace,
                snapshot.model_name,
                snapshot.backdrop_name,
                snapshot.api_combo_floor_nano,
                snapshot.model_min_floor_nano,
                _iso(snapshot.floor_fetched_at),
                snapshot.floor_age_sec,
                json.dumps(snapshot.raw_model_block),
                snapshot.floor_skip_reason,
                snapshot.own_combo_floor_nano,
                snapshot.own_sample_size,
                snapshot.own_confidence,
                snapshot.floor_sanity,
                snapshot.pair_floor_nano,
                snapshot.pair_listed_count,
                snapshot.pair_floor_status,
                snapshot.pair_floor_age_sec,
                snapshot.pair_floor_excl_self_nano,
                snapshot.pair_listed_count_excl_self,
                1 if snapshot.pair_self_was_floor else 0,
                snapshot.model_floor_excl_self_nano,
                snapshot.model_listed_count_excl_self,
                snapshot.model_floor_status,
            ),
        )


def mark_pending_below_threshold_as_no_data(conn: sqlite3.Connection, floor_min_price_nano: int) -> int:
    """ANALYTICS PATH helper, called before spending any network budget:
    a 'pending' row whose listing price is below FLOOR_MIN_PRICE_NANO
    will never be selected by get_pending_floor_rows() (which filters on
    price), so it would sit "pending" forever otherwise. This resolves it
    immediately and for free (no network call) to 'no_data'. Returns the
    number of rows updated.
    """
    with conn:
        cur = conn.execute(
            """
            UPDATE floor_snapshots
            SET pair_floor_status = 'no_data',
                floor_skip_reason = 'below_price_threshold'
            WHERE pair_floor_status = 'pending'
              AND listing_external_id IN (
                  SELECT external_id FROM listings
                  WHERE price_nano IS NULL OR price_nano < ?
              )
            """,
            (floor_min_price_nano,),
        )
        return cur.rowcount


def get_pending_floor_rows(
    conn: sqlite3.Connection, floor_min_price_nano: int, limit: int
) -> list[sqlite3.Row]:
    """Rows still awaiting ANALYTICS PATH processing: pair_floor_status
    still 'pending' AND the listing's price clears FLOOR_MIN_PRICE_NANO
    (rows below it are resolved directly by
    mark_pending_below_threshold_as_no_data, never selected here).
    Oldest first_seen_at first, so nothing waits forever under sustained
    load with a bounded per-run limit.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT l.external_id, l.collection_id, l.collection_name,
               l.collection_floor_nano, l.first_seen_at,
               f.model_name, f.backdrop_name
        FROM floor_snapshots f
        JOIN listings l ON l.external_id = f.listing_external_id
        WHERE f.pair_floor_status = 'pending'
          AND l.price_nano IS NOT NULL
          AND l.price_nano >= ?
        ORDER BY l.first_seen_at ASC
        LIMIT ?
        """,
        (floor_min_price_nano, limit),
    ).fetchall()


def count_pending_floor_rows(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM floor_snapshots WHERE pair_floor_status = 'pending'"
    ).fetchone()
    return row[0]


def update_floor_analytics(conn: sqlite3.Connection, listing_external_id: str, snapshot: FloorSnapshot) -> None:
    """ANALYTICS PATH write: fills in the floor fields for a row already
    written (as 'pending') by the FAST PATH. Unlike
    upsert_listing_with_floor, this UPDATEs an existing row -- it never
    inserts, and it is not idempotent-by-design in the ON CONFLICT sense
    because there is nothing to conflict with: the row already exists.
    """
    with conn:
        conn.execute(
            """
            UPDATE floor_snapshots
            SET api_combo_floor_nano = ?,
                model_min_floor_nano = ?,
                floor_fetched_at = ?,
                floor_age_sec = ?,
                raw_model_block = ?,
                floor_skip_reason = ?,
                own_combo_floor_nano = ?,
                own_sample_size = ?,
                own_confidence = ?,
                floor_sanity = ?,
                pair_floor_nano = ?,
                pair_listed_count = ?,
                pair_floor_status = ?,
                pair_floor_age_sec = ?,
                pair_floor_excl_self_nano = ?,
                pair_listed_count_excl_self = ?,
                pair_self_was_floor = ?,
                model_floor_excl_self_nano = ?,
                model_listed_count_excl_self = ?,
                model_floor_status = ?
            WHERE listing_external_id = ? AND marketplace = 'portals'
            """,
            (
                snapshot.api_combo_floor_nano,
                snapshot.model_min_floor_nano,
                _iso(snapshot.floor_fetched_at),
                snapshot.floor_age_sec,
                json.dumps(snapshot.raw_model_block),
                snapshot.floor_skip_reason,
                snapshot.own_combo_floor_nano,
                snapshot.own_sample_size,
                snapshot.own_confidence,
                snapshot.floor_sanity,
                snapshot.pair_floor_nano,
                snapshot.pair_listed_count,
                snapshot.pair_floor_status,
                snapshot.pair_floor_age_sec,
                snapshot.pair_floor_excl_self_nano,
                snapshot.pair_listed_count_excl_self,
                1 if snapshot.pair_self_was_floor else 0,
                snapshot.model_floor_excl_self_nano,
                snapshot.model_listed_count_excl_self,
                snapshot.model_floor_status,
                listing_external_id,
            ),
        )


def get_latest_market_config(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM market_config_snapshots ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()


def insert_market_config_snapshot_if_changed(
    conn: sqlite3.Connection, snapshot: MarketConfigSnapshot
) -> bool:
    """Writes a new row only if commission/offer_fee/withdrawal_fee/
    user_cashback/usd_course differ from the most recent stored snapshot.
    Returns True if a row was written.
    """
    latest = get_latest_market_config(conn)
    fields = ("commission", "offer_fee", "withdrawal_fee", "user_cashback", "usd_course")
    new_values = {
        f: str(getattr(snapshot, f)) if getattr(snapshot, f) is not None else None for f in fields
    }
    if latest is not None:
        old_values = {f: latest[f] for f in fields}
        if old_values == new_values:
            return False

    with conn:
        conn.execute(
            """
            INSERT INTO market_config_snapshots (
                fetched_at, raw, commission, offer_fee, withdrawal_fee,
                user_cashback, usd_course
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                _iso(snapshot.fetched_at),
                json.dumps(snapshot.raw),
                new_values["commission"],
                new_values["offer_fee"],
                new_values["withdrawal_fee"],
                new_values["user_cashback"],
                new_values["usd_course"],
            ),
        )
    return True


def get_listing_price_and_listed_at(
    conn: sqlite3.Connection, marketplace: str, external_id: str
) -> tuple[int | None, datetime | None] | None:
    """Used by poller.py to compare the page's price against what's
    already stored, for an already-known listing. Returns None if the
    listing genuinely isn't in the DB (shouldn't happen for a row already
    confirmed known, but defensive against races); (price_nano,
    listed_at) otherwise, either of which may itself be None.
    """
    row = conn.execute(
        "SELECT price_nano, listed_at FROM listings WHERE marketplace = ? AND external_id = ?",
        (marketplace, external_id),
    ).fetchone()
    if row is None:
        return None
    return row[0], _parse_iso(row[1])


def record_price_change(
    conn: sqlite3.Connection,
    marketplace: str,
    listing_external_id: str,
    old_price_nano: int,
    new_price_nano: int,
    delta_pct: Decimal,
    is_noise: bool,
    old_listed_at: datetime | None,
    new_listed_at: datetime | None,
    observed_at: datetime,
    floor_at_drop_nano: int | None = None,
    floor_listed_count_at_drop: int = 0,
    floor_fetched_at: datetime | None = None,
    is_anomaly: bool = False,
    floor_level_at_drop: str | None = None,
) -> None:
    """Records one real price change and updates the listing's current
    price_nano/listed_at in the same transaction. Never called for a
    no-op (unchanged price) -- that decision is made by the caller
    (poller.py), not here. `floor_at_drop_*` is only ever populated by
    the caller for a drop that cleared PRICE_DROP_MIN_PCT -- left at
    defaults (None/0/None) for noise drops and raises. `is_ladder` is
    NOT settable here -- it is only ever set by report.py's backfill
    pass, exactly like name_collision. `floor_level_at_drop` is "pair" or
    "model", set only when floor_at_drop_nano is populated -- see
    poller.py.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO price_history (
                listing_external_id, marketplace, old_price_nano, new_price_nano,
                delta_pct, is_noise, old_listed_at, new_listed_at, observed_at,
                floor_at_drop_nano, floor_listed_count_at_drop, floor_fetched_at, is_anomaly,
                floor_level_at_drop
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(marketplace, listing_external_id, observed_at) DO NOTHING
            """,
            (
                listing_external_id,
                marketplace,
                old_price_nano,
                new_price_nano,
                str(delta_pct),
                1 if is_noise else 0,
                _iso(old_listed_at),
                _iso(new_listed_at),
                _iso(observed_at),
                floor_at_drop_nano,
                floor_listed_count_at_drop,
                _iso(floor_fetched_at),
                1 if is_anomaly else 0,
                floor_level_at_drop,
            ),
        )
        conn.execute(
            "UPDATE listings SET price_nano = ?, listed_at = ? WHERE marketplace = ? AND external_id = ?",
            (new_price_nano, _iso(new_listed_at), marketplace, listing_external_id),
        )


def flag_burst_drops(
    conn: sqlite3.Connection,
    marketplace: str,
    listing_external_id: str,
    since: datetime,
) -> bool:
    """Confirmed live: Jelly Bunny #2627 dropped 999->99->29 within 10
    seconds -- no real seller repricing happens in bursts like that.
    Checks for any PRIOR drop on this listing since `since` (i.e. within
    PRICE_DROP_BURST_SEC of "now"); if found, retroactively flags those
    prior rows is_anomaly=1 (they looked like isolated signals when
    written, but are now known to be part of a burst) and returns True so
    the caller also flags the row it's about to insert. Idempotent to
    call repeatedly -- re-flagging an already-anomalous row is a no-op.
    """
    since_iso = _iso(since)
    row = conn.execute(
        """
        SELECT COUNT(*) FROM price_history
        WHERE marketplace = ? AND listing_external_id = ?
          AND new_price_nano < old_price_nano
          AND observed_at >= ?
        """,
        (marketplace, listing_external_id, since_iso),
    ).fetchone()
    had_recent_drop = row[0] > 0
    if had_recent_drop:
        with conn:
            conn.execute(
                """
                UPDATE price_history
                SET is_anomaly = 1
                WHERE marketplace = ? AND listing_external_id = ?
                  AND new_price_nano < old_price_nano
                  AND observed_at >= ?
                """,
                (marketplace, listing_external_id, since_iso),
            )
    return had_recent_drop


def is_alert_sent(
    conn: sqlite3.Connection, marketplace: str, listing_external_id: str, observed_at: datetime
) -> bool:
    """Anti-duplicate check for the Telegram notifier -- durable across
    poller restarts (unlike an in-memory set), see alerts_sent in SCHEMA.
    `marketplace` (schema v15) scopes the check -- external_id is only
    unique WITHIN a marketplace.
    """
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE marketplace = ? AND listing_external_id = ? AND observed_at = ?",
        (marketplace, listing_external_id, _iso(observed_at)),
    ).fetchone()
    return row is not None


def mark_alert_sent(
    conn: sqlite3.Connection,
    marketplace: str,
    listing_external_id: str,
    observed_at: datetime,
    sent_at: datetime,
    status: str = "sent",
) -> None:
    """Called ONLY after a successful Telegram send, OR after the
    pre-send freshness check (poller.py Правка 3) finds the listing is
    no longer actually buyable (status="skipped_stale") -- a plain send
    FAILURE (network/API error) must never reach here at all, so that
    case is retried on the next poll cycle rather than silently lost.
    `status` is 'sent' (default) or 'skipped_stale'. One row per SIGNAL,
    never per recipient (owner/viewers), per spec -- see notifier.py.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO alerts_sent (marketplace, listing_external_id, observed_at, sent_at, status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(marketplace, listing_external_id, observed_at) DO NOTHING
            """,
            (marketplace, listing_external_id, _iso(observed_at), _iso(sent_at), status),
        )


def get_recent_alerts(conn: sqlite3.Connection, limit: int = 5, marketplace: str = "portals") -> list[sqlite3.Row]:
    """Used by notifier.py's /last command -- the most recently ACTUALLY
    SENT signals (status='sent', excluding skipped_stale -- the user
    never saw those in Telegram, so /last must not list them as if they
    had been), joined back to price_history/listings for display.
    `marketplace` defaults "portals": CommandHandler (and therefore this
    command) only exists on the Portals side -- tonnel_poller.py sends
    notifications but runs no CommandHandler of its own (see README, the
    getUpdates long-poll offset is a single global stream per bot token,
    shared by two independent processes would be a real conflict).
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT a.listing_external_id, a.observed_at, a.sent_at,
               l.collection_name, l.model_name, l.backdrop_name, l.gift_number,
               ph.old_price_nano, ph.new_price_nano
        FROM alerts_sent a
        JOIN price_history ph ON ph.listing_external_id = a.listing_external_id
                              AND ph.observed_at = a.observed_at AND ph.marketplace = a.marketplace
        JOIN listings l ON l.external_id = a.listing_external_id AND l.marketplace = a.marketplace
        WHERE a.status = 'sent' AND a.marketplace = ?
        ORDER BY a.sent_at DESC
        LIMIT ?
        """,
        (marketplace, limit),
    ).fetchall()


def count_alerts_since(conn: sqlite3.Connection, since: datetime, marketplace: str = "portals") -> int:
    """Used by notifier.py's /status command -- counts only ACTUALLY
    SENT signals (status='sent'), not skipped_stale ones. See
    get_recent_alerts on why this defaults to "portals".
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM alerts_sent WHERE sent_at >= ? AND status = 'sent' AND marketplace = ?",
        (_iso(since), marketplace),
    ).fetchone()
    return row[0]


def get_last_sent_alert_for_listing(
    conn: sqlite3.Connection, marketplace: str, listing_external_id: str
) -> sqlite3.Row | None:
    """Used by poller.py's / tonnel_poller.py's per-listing cooldown
    (SIGNAL_COOLDOWN_MIN / TONNEL_SIGNAL_COOLDOWN_MIN): the most recent
    ACTUALLY SENT (status='sent') notification for this listing, with
    the price it was sent at -- joined back to price_history via the
    (marketplace, listing_external_id, observed_at) triple alerts_sent
    already keys on, so no separate price column was needed on
    alerts_sent itself. Returns None if this listing has never been
    successfully notified.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT a.sent_at, ph.new_price_nano
        FROM alerts_sent a
        JOIN price_history ph ON ph.listing_external_id = a.listing_external_id
                              AND ph.observed_at = a.observed_at AND ph.marketplace = a.marketplace
        WHERE a.marketplace = ? AND a.listing_external_id = ? AND a.status = 'sent'
        ORDER BY a.sent_at DESC
        LIMIT 1
        """,
        (marketplace, listing_external_id),
    ).fetchone()


def touch_listing_lifecycle(
    conn: sqlite3.Connection,
    marketplace: str,
    listing_external_id: str,
    collection_id: str | None,
    model_name: str | None,
    backdrop_name: str | None,
    price_nano: int | None,
    seen_at: datetime,
) -> None:
    """Called for EVERY listing seen on a poll iteration (new or already
    known -- see poller.py's poll_once / tonnel_poller.py's mirror of it)
    -- updates last_seen_at. If the listing had previously been marked
    disappeared_at, this is a REAPPEARANCE: disappeared_at is cleared and
    reappeared_count is incremented, since a listing coming back means it
    was delisted and relisted by its owner, NOT sold (disappearance alone
    is never proof of a sale -- see README).

    `marketplace` (schema v13) scopes every read/write here -- this table
    now holds both Portals and Tonnel rows, keyed (marketplace,
    listing_external_id), since external_id is only unique WITHIN a
    marketplace.
    """
    with conn:
        row = conn.execute(
            "SELECT disappeared_at FROM listing_lifecycle WHERE marketplace = ? AND listing_external_id = ?",
            (marketplace, listing_external_id),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO listing_lifecycle (
                    marketplace, listing_external_id, collection_id, model_name, backdrop_name,
                    first_seen_at, last_seen_at, disappeared_at, last_price_nano, reappeared_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)
                """,
                (
                    marketplace, listing_external_id, collection_id, model_name, backdrop_name,
                    _iso(seen_at), _iso(seen_at), price_nano,
                ),
            )
        elif row[0] is not None:
            conn.execute(
                """
                UPDATE listing_lifecycle
                SET last_seen_at = ?, last_price_nano = ?, disappeared_at = NULL,
                    reappeared_count = reappeared_count + 1
                WHERE marketplace = ? AND listing_external_id = ?
                """,
                (_iso(seen_at), price_nano, marketplace, listing_external_id),
            )
        else:
            conn.execute(
                "UPDATE listing_lifecycle SET last_seen_at = ?, last_price_nano = ? "
                "WHERE marketplace = ? AND listing_external_id = ?",
                (_iso(seen_at), price_nano, marketplace, listing_external_id),
            )


KNOWN_LIFECYCLE_STATUSES = {"listed", "withdrawn", "unlisted"}
# "gone_unknown" (ДОПОЛНЕНИЕ, Tonnel lifecycle fix): tonnel_poller.py's
# equivalent of "disappeared" -- Tonnel's API gives no positive
# sold-vs-delisted signal the way Portals' explicit status field does,
# so the reason genuinely can't be recorded, only the fact of absence.
# "sold" (MRKT full-signaller delivery): the ONLY marketplace in this
# project whose own event feed gives an EXPLICIT sale confirmation (a
# "sale" event, see mrkt_client.py's feed()) -- see record_sale() below,
# which is what actually sets final_status="sold" (never
# record_lifecycle_status, which has no price to record alongside it).
# "returned" (MRKT missing-event-types delivery): a "return" event --
# the gift was returned to its owner in Telegram, a DIFFERENT outcome
# from both "sold" and "unlisted" (a seller-initiated delisting) --
# mixing any of these three into one status would corrupt liquidity
# metrics (see README). "unlisted" itself is reused AS-IS for MRKT's
# "unlisting" event -- it already meant exactly this for Portals.
DISAPPEARED_STATUSES = {"withdrawn", "unlisted", "gone_unknown", "sold", "returned"}


def get_lifecycle_check_batch(conn: sqlite3.Connection, marketplace: str, limit: int) -> list[sqlite3.Row]:
    """Returns up to `limit` not-yet-disappeared listings for `marketplace`,
    oldest last_checked_at first (never-checked rows -- NULL -- sort
    first in SQLite, so they're always prioritized). This is the queue
    the background API-status-check pass (poller.py / tonnel_poller.py)
    works through -- explicitly scoped, since each marketplace's check
    uses a completely different API call.
    """
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT listing_external_id FROM listing_lifecycle
        WHERE marketplace = ? AND disappeared_at IS NULL
        ORDER BY last_checked_at ASC
        LIMIT ?
        """,
        (marketplace, limit),
    ).fetchall()


def record_lifecycle_status(
    conn: sqlite3.Connection, marketplace: str, listing_external_id: str, status: str, now: datetime
) -> None:
    """Records the result of an explicit status check (GET
    /nfts/search?ids=... for Portals; a gift_num-filtered search for
    Tonnel, see tonnel_poller.py) for one listing:
      - status == "listed"/"forsale": bumps last_checked_at only.
        disappeared_at stays NULL (or, if it was previously set -- see
        touch_listing_lifecycle for how reappearance actually gets
        detected, via the FAST PATH seeing the listing again in the
        feed, not via this function).
      - status in DISAPPEARED_STATUSES ("withdrawn", "unlisted"): sets
        disappeared_at + final_status, bumps last_checked_at.
      - any other status value: logged by the caller as unrecognized;
        treated here exactly like "listed" (bump last_checked_at only,
        never marked disappeared) -- an unknown status is not evidence
        of disappearance.
    """
    with conn:
        if status in DISAPPEARED_STATUSES:
            conn.execute(
                """
                UPDATE listing_lifecycle
                SET disappeared_at = ?, final_status = ?, last_checked_at = ?
                WHERE marketplace = ? AND listing_external_id = ?
                """,
                (_iso(now), status, _iso(now), marketplace, listing_external_id),
            )
        else:
            conn.execute(
                "UPDATE listing_lifecycle SET last_checked_at = ? WHERE marketplace = ? AND listing_external_id = ?",
                (_iso(now), marketplace, listing_external_id),
            )


def record_lifecycle_check_missing(
    conn: sqlite3.Connection, marketplace: str, listing_external_id: str, now: datetime
) -> None:
    """The listing was absent from the API response entirely -- NOT
    evidence of disappearance (an absence proves nothing; see README).
    Only bumps last_checked_at, so a listing that's chronically absent
    from responses (for whatever reason) doesn't monopolize the front of
    the check queue forever. Callers must log this themselves -- this
    function does not, so the call site controls log verbosity/context.
    """
    with conn:
        conn.execute(
            "UPDATE listing_lifecycle SET last_checked_at = ? WHERE marketplace = ? AND listing_external_id = ?",
            (_iso(now), marketplace, listing_external_id),
        )


def record_sale(
    conn: sqlite3.Connection, marketplace: str, listing_external_id: str, sold_price_nano: int | None, now: datetime
) -> None:
    """Правка 2 (MRKT full-signaller delivery): records an EXPLICIT sale
    confirmation -- final_status='sold', disappeared_at=now,
    sold_price_nano=<the sale event's amount>. This is the only
    marketplace path in this project that can call this function with a
    real price: Portals/Tonnel disappearances (record_lifecycle_status)
    never carry a confirmed price or even a confirmed CAUSE (see
    README -- "исчезновение без причины"). If the listing has no
    listing_lifecycle row yet (a sale event for a listing this poller
    never saw get listed -- possible on a cold start mid-stream), this
    is a no-op UPDATE-with-no-match, not an error -- there is nothing
    meaningful to backfill (first_seen_at/collection_id/etc. are all
    unknown).
    """
    with conn:
        conn.execute(
            """
            UPDATE listing_lifecycle
            SET disappeared_at = ?, final_status = 'sold', sold_price_nano = ?, last_checked_at = ?
            WHERE marketplace = ? AND listing_external_id = ?
            """,
            (_iso(now), sold_price_nano, _iso(now), marketplace, listing_external_id),
        )


def record_sale_floor(
    conn: sqlite3.Connection,
    marketplace: str,
    listing_external_id: str,
    floor_nano: int,
    listed_count: int,
    fetched_at: datetime,
) -> None:
    """ПРАВКА 1 (sale-vs-floor delivery): records the pair floor queried
    AT THE MOMENT OF an MRKT sale, self-excluded -- see mrkt_poller.py's
    _handle_sale_event. Called ONLY when the floor query actually
    succeeded (status "ok"); a thin book, no comparable listings, or a
    network error all leave listing_lifecycle's floor_at_sale_* columns
    NULL, never called at all -- same "no-op UPDATE-with-no-match if the
    row doesn't exist yet" behavior as record_sale (a sale event for a
    listing this poller never saw get listed has nothing to backfill).
    """
    with conn:
        conn.execute(
            """
            UPDATE listing_lifecycle
            SET floor_at_sale_nano = ?, floor_listed_count_at_sale = ?, floor_fetched_at_sale = ?
            WHERE marketplace = ? AND listing_external_id = ?
            """,
            (floor_nano, listed_count, _iso(fetched_at), marketplace, listing_external_id),
        )


def is_event_processed(conn: sqlite3.Connection, marketplace: str, event_id: str) -> bool:
    """Dedup check for mrkt_poller.py's feed processing -- protects
    against reprocessing the same event across a poller restart (see
    processed_events, schema v18).
    """
    row = conn.execute(
        "SELECT 1 FROM processed_events WHERE marketplace = ? AND event_id = ?",
        (marketplace, event_id),
    ).fetchone()
    return row is not None


def mark_event_processed(conn: sqlite3.Connection, marketplace: str, event_id: str, processed_at: datetime) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO processed_events (marketplace, event_id, processed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(marketplace, event_id) DO NOTHING
            """,
            (marketplace, event_id, _iso(processed_at)),
        )


def pair_liquidity_stats(
    conn: sqlite3.Connection,
    collection_id: str | None,
    model_name: str | None,
    backdrop_name: str | None,
    since: datetime,
    marketplace: str = "portals",
) -> tuple[int, Decimal | None]:
    """Returns (gone_count, median_time_to_gone_hours) for this exact
    (collection_id, model_name, backdrop_name) pair -- listings that
    disappeared (and have not since reappeared -- disappeared_at is
    cleared on reappearance, see touch_listing_lifecycle, so a
    delisted-and-relisted lot never inflates this count) within `since`
    .. now. median_time_to_gone_hours is None when gone_count == 0.
    Callers (signals.py) must apply their own minimum-sample-size gate
    (LIQUIDITY_WINDOW_HOURS covers the window; the "at least 3" rule
    belongs to the caller, not here) -- this function never fabricates a
    number, it just reports what it has.

    `marketplace` (schema v13, default "portals" -- signals.py is
    Portals-only this delivery, see README "этап 2") explicitly scopes
    the query: this table now holds Tonnel rows too, and Tonnel listings
    can share the same collection_name/model_name/backdrop_name text
    with a Portals pair (confirmed live: names match across the two
    marketplaces) -- an unfiltered query would silently blend
    Tonnel-sourced disappearances into a Portals liquidity estimate.
    """
    rows = conn.execute(
        """
        SELECT first_seen_at, disappeared_at FROM listing_lifecycle
        WHERE marketplace = ? AND collection_id = ? AND model_name = ? AND backdrop_name = ?
          AND disappeared_at IS NOT NULL AND disappeared_at >= ?
        """,
        (marketplace, collection_id, model_name, backdrop_name, _iso(since)),
    ).fetchall()
    gone_count = len(rows)
    if gone_count == 0:
        return 0, None

    hours = sorted(
        (datetime.fromisoformat(disappeared_at) - datetime.fromisoformat(first_seen_at)).total_seconds() / 3600
        for first_seen_at, disappeared_at in rows
    )
    mid = len(hours) // 2
    if len(hours) % 2 == 1:
        median = hours[mid]
    else:
        median = (hours[mid - 1] + hours[mid]) / 2
    return gone_count, Decimal(str(median))


def reset_lifecycle_data(conn: sqlite3.Connection) -> int:
    """One-shot cleanup for gift_sniper/lifecycle_reset.py -- resets
    disappeared_at, reappeared_count, final_status, and last_checked_at
    to their defaults on EVERY row (data collected under the retired
    time-since-last-seen rule is confirmed unreliable, see README),
    while PRESERVING first_seen_at/last_seen_at (still-accurate
    observation timestamps) and the pair identity columns
    (collection_id/model_name/backdrop_name/last_price_nano). Returns
    the number of rows reset. Never drops the table -- only resets
    columns.
    """
    with conn:
        cur = conn.execute(
            """
            UPDATE listing_lifecycle
            SET disappeared_at = NULL,
                reappeared_count = 0,
                final_status = NULL,
                last_checked_at = NULL
            """
        )
        return cur.rowcount


def fix_listings_currency(conn: sqlite3.Connection, correct_currency: str) -> int:
    """One-shot cleanup for gift_sniper/fix_currency.py -- updates
    listings.currency to `correct_currency` on every row where it
    currently differs. Idempotent: a re-run with the same
    `correct_currency` updates 0 rows (the WHERE clause only ever
    touches rows that still disagree). Returns the number of rows
    updated. Never touches any other column.
    """
    with conn:
        cur = conn.execute(
            "UPDATE listings SET currency = ? WHERE currency != ? OR currency IS NULL",
            (correct_currency, correct_currency),
        )
        return cur.rowcount


def delete_tonnel_bundle_listings(conn: sqlite3.Connection) -> int:
    """One-shot cleanup for gift_sniper/tonnel_bundle_cleanup.py --
    Правка 2: a negative gift_id is a BUNDLE (Tonnel's own documented
    convention, confirmed live: 6 such rows existed before this
    delivery's collection-time filter was added) -- its price is for the
    whole set, never comparable to a single lot, so it must be removed
    from every table it could have reached. `external_id` IS `str(gift_id)`
    for Tonnel (see tonnel_parsing.py), so a bundle's external_id always
    starts with "-". Deletes from `listings`, `price_history`, and
    `listing_lifecycle` for marketplace='tonnel'. Idempotent: a second
    run deletes 0 rows. Returns the number of `listings` rows deleted.
    """
    with conn:
        conn.execute(
            "DELETE FROM price_history WHERE marketplace = 'tonnel' AND listing_external_id LIKE '-%'"
        )
        conn.execute(
            "DELETE FROM listing_lifecycle WHERE marketplace = 'tonnel' AND listing_external_id LIKE '-%'"
        )
        cur = conn.execute(
            "DELETE FROM listings WHERE marketplace = 'tonnel' AND external_id LIKE '-%'"
        )
        return cur.rowcount


def upsert_tonnel_model_floor_snapshot(
    conn: sqlite3.Connection,
    listing_external_id: str,
    model_name: str | None,
    backdrop_name: str | None,
    model_floor_excl_self_nano: int | None,
    model_listed_count_excl_self: int,
    model_floor_status: str,
    fetched_at: datetime,
) -> None:
    """Правка 1 (Tonnel model-floor delivery): writes Tonnel's own
    at-drop MODEL-floor query into `floor_snapshots` -- the SAME table
    and SAME columns signals.py's `_floor_and_source()` already reads
    for the "snapshot" fallback (source="snapshot", level="model"), so
    no cascade change was needed to make this work: it's just another
    row, correctly scoped by marketplace='tonnel' (schema v15's
    composite PK).

    SUPERSEDES the earlier upsert_tonnel_pair_floor_snapshot (retired):
    confirmed live, 20/20 significant Tonnel price drops had NO pair
    floor at all (the feed is too thin for a second listing of the exact
    same collection+model+backdrop to coexist) -- Tonnel's pair query is
    no longer made for signal formation at all, per spec ("парный флор
    для Tonnel больше не запрашивать вовсе"). `pair_floor_status` stays
    at its schema default ('no_data') for every Tonnel row forever,
    which is exactly what makes signals.py's existing pair-then-model
    hierarchy naturally always resolve to "model" for Tonnel, with zero
    cascade code changes needed (see _floor_and_level).

    `model_floor_status` is whatever the CALLER (tonnel_poller.py)
    decided after applying TONNEL_MODEL_MIN_LISTED_COUNT -- "ok",
    "no_data", or "thin_model_book" (a real floor exists but the book is
    too shallow to trust it, see README) -- this function has no
    threshold opinion of its own, it just persists the given status.

    Unlike `upsert_listing_with_floor` (FAST PATH, ON CONFLICT DO
    NOTHING -- the row doesn't exist yet) this is a true UPSERT: Tonnel
    never gets a `floor_snapshots` row at collection time (see
    `insert_listing` -- there is no Tonnel equivalent of the API
    combo-floor/own-floor analytics path), so the FIRST at-drop check
    for a given listing INSERTs, and every later recheck UPDATEs the
    same row. Only the model-floor fields and identity are set --
    api_combo_floor_nano/own_*/pair_* stay at their defaults.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO floor_snapshots (
                listing_external_id, marketplace, model_name, backdrop_name,
                floor_fetched_at, floor_age_sec, raw_model_block,
                model_floor_excl_self_nano, model_listed_count_excl_self, model_floor_status
            ) VALUES (?, 'tonnel', ?, ?, ?, 0, '{}', ?, ?, ?)
            ON CONFLICT(marketplace, listing_external_id) DO UPDATE SET
                model_name = excluded.model_name,
                backdrop_name = excluded.backdrop_name,
                floor_fetched_at = excluded.floor_fetched_at,
                model_floor_excl_self_nano = excluded.model_floor_excl_self_nano,
                model_listed_count_excl_self = excluded.model_listed_count_excl_self,
                model_floor_status = excluded.model_floor_status
            """,
            (
                listing_external_id, model_name or "", backdrop_name,
                _iso(fetched_at), model_floor_excl_self_nano,
                model_listed_count_excl_self, model_floor_status,
            ),
        )


def upsert_mrkt_pair_floor_snapshot(
    conn: sqlite3.Connection,
    listing_external_id: str,
    model_name: str | None,
    backdrop_name: str | None,
    pair_floor_excl_self_nano: int | None,
    pair_listed_count_excl_self: int,
    pair_floor_status: str,
    fetched_at: datetime,
) -> None:
    """Правка 3 (MRKT full-signaller delivery): writes MRKT's own
    at-drop PAIR-floor query into `floor_snapshots` -- the SAME table
    and SAME columns signals.py's `_floor_and_source()` already reads
    for the "snapshot" fallback (source="snapshot", level="pair"), so no
    cascade change was needed to make this work: it's just another row,
    correctly scoped by marketplace='mrkt' (schema v16's composite PK).

    Mirrors upsert_tonnel_model_floor_snapshot's shape exactly, but
    writes the PAIR_* columns instead of MODEL_* -- MRKT's own
    /gifts/saling query returns a real `total` depth-of-book figure for
    an exact (collection, model, backdrop) triple with no pagination
    needed (see mrkt_client.pair_floor()), unlike Tonnel where the pair
    level is confirmed always empty and model-level is the only
    workable basis. `pair_floor_status` is whatever the CALLER
    (mrkt_poller.py) decided after applying MRKT_FLOOR_MIN_LISTED_COUNT
    -- this function has no threshold opinion of its own, it just
    persists the given status.

    Like the Tonnel sibling, this is a true UPSERT (not ON CONFLICT DO
    NOTHING): MRKT never gets a floor_snapshots row at collection time
    (no equivalent of Portals' API-combo-floor/own-floor analytics
    path), so the FIRST at-drop check for a listing INSERTs, every later
    recheck UPDATEs. Only the pair-floor fields and identity are set --
    api_combo_floor_nano/own_*/model_* stay at their defaults.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO floor_snapshots (
                listing_external_id, marketplace, model_name, backdrop_name,
                floor_fetched_at, floor_age_sec, raw_model_block,
                pair_floor_excl_self_nano, pair_listed_count_excl_self, pair_floor_status
            ) VALUES (?, 'mrkt', ?, ?, ?, 0, '{}', ?, ?, ?)
            ON CONFLICT(marketplace, listing_external_id) DO UPDATE SET
                model_name = excluded.model_name,
                backdrop_name = excluded.backdrop_name,
                floor_fetched_at = excluded.floor_fetched_at,
                pair_floor_excl_self_nano = excluded.pair_floor_excl_self_nano,
                pair_listed_count_excl_self = excluded.pair_listed_count_excl_self,
                pair_floor_status = excluded.pair_floor_status
            """,
            (
                listing_external_id, model_name or "", backdrop_name,
                _iso(fetched_at), pair_floor_excl_self_nano,
                pair_listed_count_excl_self, pair_floor_status,
            ),
        )


def record_tonnel_floor_snapshot(
    conn: sqlite3.Connection,
    listing_external_id: str,
    collection_name: str | None,
    model_name: str | None,
    backdrop_name: str | None,
    tonnel_floor_nano: int | None,
    tonnel_floor_with_fee_nano: int | None,
    tonnel_listed_count: int,
    tonnel_status: str,
    fetched_at: datetime,
    tonnel_implausible: bool = False,
) -> None:
    """Records one Tonnel cross-market check (tonnel_client.py /
    poller.py) -- a listing can be cross-checked more than once over
    time, so this always INSERTs a new row (PRIMARY KEY is
    (listing_external_id, fetched_at)), it never UPDATEs an existing one.

    `tonnel_implausible` (Правка 3, schema v12): True when
    signals.is_tonnel_implausible() flagged this snapshot's ratio above
    TONNEL_MAX_RATIO -- a bad Tonnel listing, not a real confirmation.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO tonnel_floor_snapshots (
                listing_external_id, collection_name, model_name, backdrop_name,
                tonnel_floor_nano, tonnel_floor_with_fee_nano, tonnel_listed_count,
                tonnel_status, tonnel_implausible, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(listing_external_id, fetched_at) DO NOTHING
            """,
            (
                listing_external_id, collection_name, model_name, backdrop_name,
                tonnel_floor_nano, tonnel_floor_with_fee_nano, tonnel_listed_count,
                tonnel_status, int(tonnel_implausible), _iso(fetched_at),
            ),
        )


def record_cross_check_snapshot(
    conn: sqlite3.Connection,
    signal_marketplace: str,
    checked_marketplace: str,
    listing_external_id: str,
    collection_name: str | None,
    model_name: str | None,
    backdrop_name: str | None,
    neighbour_floor_nano: int | None,
    neighbour_listed_count: int,
    verdict: str,
    fetched_at: datetime,
) -> None:
    """Правка 1/3 (two-way cross-check delivery) + Правка 2 (MRKT third-
    neighbour delivery): the generic, direction-agnostic sibling of
    record_tonnel_floor_snapshot -- written for EVERY (signal, neighbour)
    direction (signal_marketplace='portals'/checked_marketplace='tonnel'
    or 'mrkt', and so on). A signal can now be checked against MULTIPLE
    neighbours in one cross_check() call, all sharing the same
    `fetched_at` -- PRIMARY KEY is (signal_marketplace,
    checked_marketplace, listing_external_id, fetched_at), schema v17,
    so different neighbours never collide. Same discipline otherwise: a
    listing can be cross-checked more than once over time, so this
    always INSERTs, never UPDATEs.

    ДОПОЛНЕНИЕ (MRKT unit-conversion bug fix): `neighbour_floor_nano`
    above SANITY_MAX_NANO is refused -- logged with the marketplace and
    the offending value, ROW NOT WRITTEN, never raised. This is a
    backstop against a bad unit conversion upstream (confirmed live:
    mrkt_client.py double-converting an already-nano salePrice produced
    ~1.6e19, past SQLite's own INTEGER limit and an unhandled
    OverflowError that took the whole poller down) -- never a substitute
    for fixing the conversion at its source.
    """
    if neighbour_floor_nano is not None and neighbour_floor_nano > SANITY_MAX_NANO:
        logger.error(
            "refusing to write cross_check_snapshots row: neighbour_floor_nano=%s from "
            "checked_marketplace=%s exceeds SANITY_MAX_NANO=%s (signal_marketplace=%s, "
            "listing_external_id=%s) -- likely a unit-conversion bug upstream, not a real price",
            neighbour_floor_nano, checked_marketplace, SANITY_MAX_NANO,
            signal_marketplace, listing_external_id,
        )
        return

    with conn:
        conn.execute(
            """
            INSERT INTO cross_check_snapshots (
                signal_marketplace, checked_marketplace, listing_external_id,
                collection_name, model_name, backdrop_name,
                neighbour_floor_nano, neighbour_listed_count, verdict, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(signal_marketplace, checked_marketplace, listing_external_id, fetched_at) DO NOTHING
            """,
            (
                signal_marketplace, checked_marketplace, listing_external_id,
                collection_name, model_name, backdrop_name,
                neighbour_floor_nano, neighbour_listed_count, verdict, _iso(fetched_at),
            ),
        )


def latest_cross_check_snapshots(conn: sqlite3.Connection, signal_marketplace: str | None = None) -> list[sqlite3.Row]:
    """The latest cross_check_snapshots row per listing -- used by
    report.py's per-direction breakdown. `signal_marketplace=None` (the
    default) returns both directions in one query, so the report can
    split them itself without two round trips.
    """
    conn.row_factory = sqlite3.Row
    if signal_marketplace is None:
        return conn.execute(
            """
            SELECT * FROM cross_check_snapshots
            WHERE (signal_marketplace, listing_external_id, fetched_at) IN (
                SELECT signal_marketplace, listing_external_id, MAX(fetched_at)
                FROM cross_check_snapshots
                GROUP BY signal_marketplace, listing_external_id
            )
            """
        ).fetchall()
    return conn.execute(
        """
        SELECT * FROM cross_check_snapshots
        WHERE signal_marketplace = ?
          AND (listing_external_id, fetched_at) IN (
            SELECT listing_external_id, MAX(fetched_at)
            FROM cross_check_snapshots
            WHERE signal_marketplace = ?
            GROUP BY listing_external_id
        )
        """,
        (signal_marketplace, signal_marketplace),
    ).fetchall()


def get_portals_collection_id_by_name(conn: sqlite3.Connection, collection_name: str) -> str | None:
    """Правка 1 (two-way cross-check delivery): a Tonnel signal's own
    `collection_id` is always None (Tonnel has no such field, see
    tonnel_parsing.py/README) -- but Portals' search_pair_floor()
    REQUIRES a real Portals collection_id to filter by (confirmed:
    unfiltered without it). Since collection/model/backdrop NAMES are
    confirmed to match across the two marketplaces, this looks up any
    Portals listing we've already collected under the same
    collection_name and borrows its collection_id. Returns None if we've
    never seen this collection on Portals at all -- the caller treats
    that exactly like a failed neighbour query (verdict="no_data"),
    never blocking the Tonnel signal itself.
    """
    row = conn.execute(
        "SELECT collection_id FROM listings WHERE marketplace = 'portals' AND collection_name = ? "
        "AND collection_id IS NOT NULL LIMIT 1",
        (collection_name,),
    ).fetchone()
    return row[0] if row else None
