"""Guards against the exact class of bug that hit this project twice now:
a column gets added to FloorSnapshot/price_history and used by
report.py/poller.py, but the migration or the CURRENT_SCHEMA_VERSION
bump is forgotten -- and every unit test creates its DB from scratch via
db.connect(), so the mismatch never shows up until someone runs the code
against a real, previously-created file.

This test builds a REAL on-disk DB file shaped like each historical
schema version (1 through 4), the way an actual old file would look
(full `listings` + `market_config_snapshots` + version-appropriate
`floor_snapshots`/`price_history`), points db.connect() at it (exercising
the real migration path used in production, not a hand-picked internal
function), and then runs the actual queries report.py and poller.py
depend on. If a future column is added to the ORM-ish dataclasses/SQL
without a matching migration, this test breaks BEFORE a user's real DB
does.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gift_sniper import config, db
from gift_sniper.models import FloorSnapshot, Listing
from gift_sniper.report import generate_report

LISTINGS_V1 = """
CREATE TABLE listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marketplace TEXT NOT NULL, external_id TEXT NOT NULL, tg_id TEXT,
    collection_id TEXT, collection_name TEXT, gift_number INTEGER,
    price_nano INTEGER, currency TEXT NOT NULL, collection_floor_nano INTEGER,
    model_name TEXT, symbol_name TEXT, backdrop_name TEXT,
    model_rarity_raw TEXT, symbol_rarity_raw TEXT, backdrop_rarity_raw TEXT,
    image_url TEXT, animation_url TEXT, listed_at TEXT, unlocks_at TEXT,
    status TEXT, first_seen_at TEXT NOT NULL, raw TEXT,
    UNIQUE(marketplace, external_id)
)
"""

MARKET_CONFIG_SNAPSHOTS = """
CREATE TABLE market_config_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TEXT NOT NULL, raw TEXT NOT NULL,
    commission TEXT, offer_fee TEXT, withdrawal_fee TEXT, user_cashback TEXT, usd_course TEXT
)
"""

FLOOR_SNAPSHOTS_V1 = """
CREATE TABLE floor_snapshots (
    listing_external_id TEXT PRIMARY KEY, model_name TEXT NOT NULL, backdrop_name TEXT,
    combo_floor_nano INTEGER, model_min_floor_nano INTEGER, floor_fetched_at TEXT NOT NULL,
    floor_age_sec INTEGER NOT NULL, raw_model_block TEXT NOT NULL,
    name_collision INTEGER NOT NULL DEFAULT 0, floor_skip_reason TEXT
)
"""

FLOOR_SNAPSHOTS_V2 = """
CREATE TABLE floor_snapshots (
    listing_external_id TEXT PRIMARY KEY, model_name TEXT NOT NULL, backdrop_name TEXT,
    api_combo_floor_nano INTEGER, model_min_floor_nano INTEGER, floor_fetched_at TEXT NOT NULL,
    floor_age_sec INTEGER NOT NULL, raw_model_block TEXT NOT NULL,
    name_collision INTEGER NOT NULL DEFAULT 0, floor_skip_reason TEXT,
    own_combo_floor_nano INTEGER, own_sample_size INTEGER NOT NULL DEFAULT 0,
    own_confidence TEXT NOT NULL DEFAULT 'none', floor_sanity TEXT NOT NULL DEFAULT 'no_data'
)
"""

FLOOR_SNAPSHOTS_V3 = """
CREATE TABLE floor_snapshots (
    listing_external_id TEXT PRIMARY KEY, model_name TEXT NOT NULL, backdrop_name TEXT,
    api_combo_floor_nano INTEGER, model_min_floor_nano INTEGER, floor_fetched_at TEXT NOT NULL,
    floor_age_sec INTEGER NOT NULL, raw_model_block TEXT NOT NULL,
    name_collision INTEGER NOT NULL DEFAULT 0, floor_skip_reason TEXT,
    own_combo_floor_nano INTEGER, own_sample_size INTEGER NOT NULL DEFAULT 0,
    own_confidence TEXT NOT NULL DEFAULT 'none', floor_sanity TEXT NOT NULL DEFAULT 'no_data',
    pair_floor_nano INTEGER, pair_listed_count INTEGER NOT NULL DEFAULT 0,
    pair_floor_status TEXT NOT NULL DEFAULT 'no_data', pair_floor_age_sec INTEGER NOT NULL DEFAULT 0
)
"""

PRICE_HISTORY_V4 = """
CREATE TABLE price_history (
    listing_external_id TEXT NOT NULL, marketplace TEXT NOT NULL,
    old_price_nano INTEGER NOT NULL, new_price_nano INTEGER NOT NULL,
    delta_pct NUMERIC NOT NULL, is_noise INTEGER NOT NULL DEFAULT 0,
    old_listed_at TEXT, new_listed_at TEXT, observed_at TEXT NOT NULL,
    PRIMARY KEY(listing_external_id, observed_at)
)
"""

VERSION_BUILDERS = {
    1: (FLOOR_SNAPSHOTS_V1, False),
    2: (FLOOR_SNAPSHOTS_V2, False),
    3: (FLOOR_SNAPSHOTS_V3, False),
    4: (FLOOR_SNAPSHOTS_V3, True),  # v4 floor_snapshots == v3's; price_history is what's new
}


def _build_versioned_db_file(path: str, version: int, floor_snapshots_ddl: str, has_price_history: bool) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (version, "2026-01-01T00:00:00+00:00"),
    )
    conn.execute(LISTINGS_V1)
    conn.execute(
        "INSERT INTO listings (marketplace, external_id, tg_id, collection_id, collection_name, "
        "gift_number, price_nano, currency, model_name, backdrop_name, status, first_seen_at) "
        "VALUES ('portals','ext-1','ext-1-tg','col-a','CollA',1,20000000000,'TON','M','B','listed',"
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.execute(MARKET_CONFIG_SNAPSHOTS)
    conn.execute(floor_snapshots_ddl)
    floor_cols = "listing_external_id, model_name, backdrop_name, floor_fetched_at, floor_age_sec, raw_model_block"
    conn.execute(
        f"INSERT INTO floor_snapshots ({floor_cols}) "
        f"VALUES ('ext-1','M','B','2026-01-01T00:00:00+00:00',0,'{{}}')"
    )
    if has_price_history:
        conn.execute(PRICE_HISTORY_V4)
        conn.execute(
            "INSERT INTO price_history VALUES ('ext-1','portals',25000000000,20000000000,'-20.0',0,"
            "'2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00','2026-01-02T00:00:00+00:00')"
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_migration_from_every_historical_version_survives_real_queries(version):
    floor_ddl, has_price_history = VERSION_BUILDERS[version]
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # _build_versioned_db_file creates it fresh
    conn = None
    try:
        _build_versioned_db_file(path, version, floor_ddl, has_price_history)

        # The real production path: db.connect(), not a hand-picked
        # internal migration function.
        conn = db.connect(path)
        db.verify_schema(conn)  # must not raise

        # report.py's actual main query must succeed.
        report_text = generate_report(conn, usd_rate=Decimal("1.0"))
        assert "=== Gift Sniper report ===" in report_text
        assert "=== price drops ===" in report_text

        # Pre-existing data survived the migration. (generate_report sets
        # conn.row_factory = sqlite3.Row as a side effect -- tuple()
        # normalizes either way.)
        row = conn.execute("SELECT external_id, price_nano FROM listings").fetchone()
        assert tuple(row) == ("ext-1", 20000000000)

        # poller.py's actual main write path must also succeed against
        # the migrated schema.
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        listing = Listing(
            marketplace="portals", external_id="ext-2", tg_id="ext-2-tg",
            collection_id="col-a", collection_name="CollA", gift_number=2,
            price_nano=int(Decimal("15") * config.NANO), currency="TON",
            collection_floor_nano=None, model_name="M2", symbol_name=None,
            backdrop_name="B2", model_rarity_raw=None, symbol_rarity_raw=None,
            backdrop_rarity_raw=None, image_url=None, animation_url=None,
            listed_at=now, unlocks_at=None, status="listed", first_seen_at=now, raw={},
        )
        snapshot = FloorSnapshot(
            listing_external_id="ext-2", model_name="M2", backdrop_name="B2",
            api_combo_floor_nano=None, model_min_floor_nano=None,
            floor_fetched_at=now, floor_age_sec=0, raw_model_block={},
            pair_floor_status="pending",
        )
        db.upsert_listing_with_floor(conn, listing, snapshot)  # must not raise

        count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        assert count == 2
    finally:
        if conn is not None:
            conn.close()
        if os.path.exists(path):
            os.remove(path)
