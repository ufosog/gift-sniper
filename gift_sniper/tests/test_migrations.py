import sqlite3

from gift_sniper import db


def _create_old_schema_floor_snapshots(conn: sqlite3.Connection) -> None:
    """Reconstructs the floor_snapshots table exactly as it looked before
    the api_combo_floor_nano rename and own_* columns were added --
    version 1 of the schema. Mirrors what db.connect() would do on a real
    old DB file: SCHEMA's CREATE TABLE IF NOT EXISTS runs first (creating
    schema_version and any genuinely-missing tables, but leaving this
    already-existing floor_snapshots table untouched), then this old
    table is created before it, exactly as a pre-existing file would have
    it on disk.
    """
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        """
        CREATE TABLE floor_snapshots (
            listing_external_id TEXT PRIMARY KEY,
            model_name TEXT NOT NULL,
            backdrop_name TEXT,
            combo_floor_nano INTEGER,
            model_min_floor_nano INTEGER,
            floor_fetched_at TEXT NOT NULL,
            floor_age_sec INTEGER NOT NULL,
            raw_model_block TEXT NOT NULL,
            name_collision INTEGER NOT NULL DEFAULT 0,
            floor_skip_reason TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO floor_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ext-1", "ModelX", "Copper", 12345, 12345, "2026-01-01T00:00:00+00:00", 0, "{}", 0, None),
    )
    conn.commit()


def test_migration_adds_columns_and_preserves_data_on_old_schema():
    conn = sqlite3.connect(":memory:")
    _create_old_schema_floor_snapshots(conn)

    db._migrate(conn)

    cols = db._table_columns(conn, "floor_snapshots")
    for expected in (
        "api_combo_floor_nano", "own_combo_floor_nano",
        "own_sample_size", "own_confidence", "floor_sanity",
    ):
        assert expected in cols

    row = conn.execute(
        "SELECT listing_external_id, api_combo_floor_nano, own_confidence, floor_sanity "
        "FROM floor_snapshots"
    ).fetchone()
    assert row == ("ext-1", 12345, "none", "no_data")

    count = conn.execute("SELECT COUNT(*) FROM floor_snapshots").fetchone()[0]
    assert count == 1  # row not lost, not duplicated


def test_migration_twice_does_not_error_or_duplicate():
    conn = sqlite3.connect(":memory:")
    _create_old_schema_floor_snapshots(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "duplicate column name" or similar

    count = conn.execute("SELECT COUNT(*) FROM floor_snapshots").fetchone()[0]
    assert count == 1

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v2_schema_floor_snapshots(conn: sqlite3.Connection) -> None:
    """Reconstructs floor_snapshots as it looked at schema version 2 --
    api_combo_floor_nano / own_* present, pair_floor_* columns not yet
    added.
    """
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (2, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        """
        CREATE TABLE floor_snapshots (
            listing_external_id TEXT PRIMARY KEY,
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
            floor_sanity TEXT NOT NULL DEFAULT 'no_data'
        )
        """
    )
    conn.execute(
        "INSERT INTO floor_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ext-9", "ModelY", "Onyx", 200000000000, 200000000000,
         "2026-01-01T00:00:00+00:00", 0, "{}", 0, None,
         50000000000, 12, "high", "suspect"),
    )
    conn.commit()


def test_migration_2_to_3_adds_pair_floor_columns_without_losing_data():
    conn = sqlite3.connect(":memory:")
    _create_v2_schema_floor_snapshots(conn)

    db._migrate(conn)

    cols = db._table_columns(conn, "floor_snapshots")
    for expected in (
        "pair_floor_nano", "pair_listed_count", "pair_floor_status", "pair_floor_age_sec",
    ):
        assert expected in cols

    row = conn.execute(
        "SELECT listing_external_id, api_combo_floor_nano, own_combo_floor_nano, "
        "own_confidence, floor_sanity, pair_floor_status "
        "FROM floor_snapshots"
    ).fetchone()
    # Old v2 values preserved; new pair_floor_status defaults to 'no_data'
    # (no prior column to backfill it from).
    assert row == ("ext-9", 200000000000, 50000000000, "high", "suspect", "no_data")

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v3_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 3 -- full
    pair_floor_* columns present, price_history table not yet added.
    """
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (3, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        """
        CREATE TABLE listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace TEXT NOT NULL,
            external_id TEXT NOT NULL,
            price_nano INTEGER,
            currency TEXT NOT NULL,
            listed_at TEXT,
            first_seen_at TEXT NOT NULL,
            UNIQUE(marketplace, external_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO listings (marketplace, external_id, price_nano, currency, listed_at, first_seen_at) "
        "VALUES ('portals', 'ext-42', 20000000000, 'TON', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        """
        CREATE TABLE floor_snapshots (
            listing_external_id TEXT PRIMARY KEY,
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
            pair_floor_age_sec INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO floor_snapshots (listing_external_id, model_name, backdrop_name, "
        "floor_fetched_at, floor_age_sec, raw_model_block, pair_floor_status) "
        "VALUES ('ext-42', 'ModelZ', 'Copper', '2026-01-01T00:00:00+00:00', 0, '{}', 'ok')"
    )
    conn.commit()


def test_migration_3_to_4_adds_price_history_without_losing_existing_data():
    conn = sqlite3.connect(":memory:")
    _create_v3_schema(conn)

    db._migrate(conn)

    assert db._table_exists(conn, "price_history")
    cols = db._table_columns(conn, "price_history")
    for expected in (
        "listing_external_id", "marketplace", "old_price_nano", "new_price_nano",
        "delta_pct", "is_noise", "old_listed_at", "new_listed_at", "observed_at",
    ):
        assert expected in cols

    # Pre-existing listings/floor_snapshots data untouched.
    listing_row = conn.execute("SELECT external_id, price_nano FROM listings").fetchone()
    assert listing_row == ("ext-42", 20000000000)
    floor_row = conn.execute("SELECT listing_external_id, pair_floor_status FROM floor_snapshots").fetchone()
    assert floor_row == ("ext-42", "ok")

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v4_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 4 -- price_history
    exists but without the floor_at_drop_*/is_ladder/is_anomaly columns,
    and floor_snapshots doesn't have the _excl_self columns yet.
    """
    _create_v3_schema(conn)
    conn.execute("UPDATE schema_version SET version = 4")
    conn.execute(
        """
        CREATE TABLE price_history (
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
        "INSERT INTO price_history VALUES (?,?,?,?,?,?,?,?,?)",
        ("ext-42", "portals", 25000000000, 20000000000, "-20.0", 0,
         "2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"),
    )
    conn.commit()


def test_migration_4_to_5_adds_self_exclusion_and_anomaly_columns_without_losing_data():
    conn = sqlite3.connect(":memory:")
    _create_v4_schema(conn)

    db._migrate(conn)

    floor_cols = db._table_columns(conn, "floor_snapshots")
    for expected in ("pair_floor_excl_self_nano", "pair_listed_count_excl_self", "pair_self_was_floor"):
        assert expected in floor_cols

    history_cols = db._table_columns(conn, "price_history")
    for expected in ("floor_at_drop_nano", "floor_listed_count_at_drop", "floor_fetched_at", "is_ladder", "is_anomaly"):
        assert expected in history_cols

    # Pre-existing data untouched.
    listing_row = conn.execute("SELECT external_id, price_nano FROM listings").fetchone()
    assert listing_row == ("ext-42", 20000000000)
    history_row = conn.execute(
        "SELECT listing_external_id, old_price_nano, new_price_nano FROM price_history"
    ).fetchone()
    assert history_row == ("ext-42", 25000000000, 20000000000)


def _create_v5_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 5 -- self-
    exclusion pair floor columns exist, but the model-level fallback
    columns (Правка 2) and floor_level_at_drop (Правка 4) don't yet.
    """
    _create_v4_schema(conn)
    conn.execute("UPDATE schema_version SET version = 5")
    conn.execute("ALTER TABLE floor_snapshots ADD COLUMN pair_floor_excl_self_nano INTEGER")
    conn.execute("ALTER TABLE floor_snapshots ADD COLUMN pair_listed_count_excl_self INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE floor_snapshots ADD COLUMN pair_self_was_floor INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "UPDATE floor_snapshots SET pair_floor_excl_self_nano = 20400000000, pair_self_was_floor = 0"
    )
    conn.execute("ALTER TABLE price_history ADD COLUMN floor_at_drop_nano INTEGER")
    conn.execute("ALTER TABLE price_history ADD COLUMN floor_listed_count_at_drop INTEGER")
    conn.execute("ALTER TABLE price_history ADD COLUMN floor_fetched_at TEXT")
    conn.execute("ALTER TABLE price_history ADD COLUMN is_ladder INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE price_history ADD COLUMN is_anomaly INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def test_migration_5_to_6_adds_model_floor_and_floor_level_columns_without_losing_data():
    conn = sqlite3.connect(":memory:")
    _create_v5_schema(conn)

    db._migrate(conn)

    floor_cols = db._table_columns(conn, "floor_snapshots")
    for expected in ("model_floor_excl_self_nano", "model_listed_count_excl_self", "model_floor_status"):
        assert expected in floor_cols

    history_cols = db._table_columns(conn, "price_history")
    assert "floor_level_at_drop" in history_cols

    # Pre-existing v5 data untouched; new columns default sensibly.
    floor_row = conn.execute(
        "SELECT listing_external_id, pair_floor_excl_self_nano, model_floor_status "
        "FROM floor_snapshots"
    ).fetchone()
    assert floor_row == ("ext-42", 20400000000, "no_data")

    history_row = conn.execute(
        "SELECT listing_external_id, old_price_nano, new_price_nano, floor_level_at_drop "
        "FROM price_history"
    ).fetchone()
    assert history_row == ("ext-42", 25000000000, 20000000000, None)

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v6_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 6 -- model-level
    fallback floor columns exist, but alerts_sent (the Telegram notifier's
    anti-duplicate ledger, this delivery) doesn't yet.
    """
    _create_v5_schema(conn)
    conn.execute("UPDATE schema_version SET version = 6")
    conn.execute("ALTER TABLE floor_snapshots ADD COLUMN model_floor_excl_self_nano INTEGER")
    conn.execute("ALTER TABLE floor_snapshots ADD COLUMN model_listed_count_excl_self INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE floor_snapshots ADD COLUMN model_floor_status TEXT NOT NULL DEFAULT 'no_data'")
    conn.execute("ALTER TABLE price_history ADD COLUMN floor_level_at_drop TEXT")
    conn.commit()


def test_migration_6_to_7_adds_alerts_sent_table_without_losing_data():
    conn = sqlite3.connect(":memory:")
    _create_v6_schema(conn)

    db._migrate(conn)

    assert db._table_exists(conn, "alerts_sent")
    alerts_cols = db._table_columns(conn, "alerts_sent")
    for expected in ("listing_external_id", "observed_at", "sent_at"):
        assert expected in alerts_cols

    # Pre-existing v6 data untouched.
    floor_row = conn.execute(
        "SELECT listing_external_id, pair_floor_excl_self_nano FROM floor_snapshots"
    ).fetchone()
    assert floor_row == ("ext-42", 20400000000)

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def test_migration_6_to_7_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v6_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "table alerts_sent already exists" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v7_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 7 -- alerts_sent
    exists, but listing_lifecycle (this delivery, Правка 2) doesn't yet.
    """
    _create_v6_schema(conn)
    conn.execute("UPDATE schema_version SET version = 7")
    conn.execute(
        """
        CREATE TABLE alerts_sent (
            listing_external_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY(listing_external_id, observed_at)
        )
        """
    )
    conn.execute(
        "INSERT INTO alerts_sent VALUES (?,?,?)",
        ("ext-42", "2026-01-02T00:00:00+00:00", "2026-01-02T00:01:00+00:00"),
    )
    conn.commit()


def test_migration_7_to_8_adds_listing_lifecycle_table_without_losing_data():
    """КАК ТЕСТИРОВАТЬ item 7: migration 7 -> 8, data not lost."""
    conn = sqlite3.connect(":memory:")
    _create_v7_schema(conn)

    db._migrate(conn)

    assert db._table_exists(conn, "listing_lifecycle")
    lifecycle_cols = db._table_columns(conn, "listing_lifecycle")
    for expected in (
        "listing_external_id", "collection_id", "model_name", "backdrop_name",
        "first_seen_at", "last_seen_at", "disappeared_at", "last_price_nano",
        "reappeared_count",
    ):
        assert expected in lifecycle_cols

    # Pre-existing v7 data untouched.
    alerts_row = conn.execute(
        "SELECT listing_external_id, sent_at FROM alerts_sent"
    ).fetchone()
    assert alerts_row == ("ext-42", "2026-01-02T00:01:00+00:00")
    floor_row = conn.execute(
        "SELECT listing_external_id, pair_floor_excl_self_nano FROM floor_snapshots"
    ).fetchone()
    assert floor_row == ("ext-42", 20400000000)

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def test_migration_7_to_8_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v7_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "table listing_lifecycle already exists" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v8_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 8 --
    listing_lifecycle exists, but alerts_sent.status (this delivery,
    Правка 3) doesn't yet.
    """
    _create_v7_schema(conn)
    conn.execute("UPDATE schema_version SET version = 8")
    conn.execute(
        """
        CREATE TABLE listing_lifecycle (
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
        "INSERT INTO listing_lifecycle "
        "(listing_external_id, collection_id, model_name, backdrop_name, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?)",
        ("ext-42", "col-a", "Model", "Backdrop", "2026-01-02T00:00:00+00:00", "2026-01-02T00:05:00+00:00"),
    )
    conn.commit()


def test_migration_8_to_9_adds_alerts_sent_status_column_without_losing_data():
    conn = sqlite3.connect(":memory:")
    _create_v8_schema(conn)

    db._migrate(conn)

    alerts_cols = db._table_columns(conn, "alerts_sent")
    assert "status" in alerts_cols

    # Pre-existing rows default to 'sent' (they predate the freshness
    # check, and were genuinely delivered).
    alerts_row = conn.execute("SELECT listing_external_id, status FROM alerts_sent").fetchone()
    assert alerts_row == ("ext-42", "sent")

    # Pre-existing v8 lifecycle data untouched.
    lifecycle_row = conn.execute(
        "SELECT listing_external_id, collection_id FROM listing_lifecycle"
    ).fetchone()
    assert lifecycle_row == ("ext-42", "col-a")

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def test_migration_8_to_9_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v8_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "duplicate column name: status" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v9_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 9 --
    alerts_sent.status exists, but listing_lifecycle.last_checked_at /
    final_status (this delivery, the API-status-check fix) don't yet.
    """
    _create_v8_schema(conn)
    conn.execute("UPDATE schema_version SET version = 9")
    conn.execute("ALTER TABLE alerts_sent ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'")
    conn.execute(
        "INSERT INTO listing_lifecycle "
        "(listing_external_id, collection_id, model_name, backdrop_name, first_seen_at, last_seen_at, "
        "disappeared_at, reappeared_count) VALUES (?,?,?,?,?,?,?,?)",
        ("ext-99", "col-a", "Model", "Backdrop", "2026-01-02T00:00:00+00:00", "2026-01-02T00:05:00+00:00",
         None, 0),
    )
    conn.commit()


def test_migration_9_to_10_adds_lifecycle_check_columns_without_losing_data():
    """КАК ТЕСТИРОВАТЬ (lifecycle-fix delivery) item: migration 9 -> 10,
    data not lost.
    """
    conn = sqlite3.connect(":memory:")
    _create_v9_schema(conn)

    db._migrate(conn)

    lifecycle_cols = db._table_columns(conn, "listing_lifecycle")
    assert "last_checked_at" in lifecycle_cols
    assert "final_status" in lifecycle_cols

    # Pre-existing v9 data untouched (schema-only migration -- the actual
    # data reset is a separate, explicit script, lifecycle_reset.py).
    lifecycle_row = conn.execute(
        "SELECT listing_external_id, collection_id, disappeared_at, reappeared_count "
        "FROM listing_lifecycle WHERE listing_external_id = 'ext-99'"
    ).fetchone()
    assert lifecycle_row == ("ext-99", "col-a", None, 0)
    alerts_row = conn.execute("SELECT listing_external_id, status FROM alerts_sent").fetchone()
    assert alerts_row == ("ext-42", "sent")

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def test_migration_9_to_10_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v9_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "duplicate column name" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v10_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 10 -- everything
    through the v9->v10 lifecycle-check-columns migration exists, but
    tonnel_floor_snapshots (this delivery, Tonnel cross-market check) does
    not yet.
    """
    _create_v9_schema(conn)
    conn.execute("UPDATE schema_version SET version = 10")
    conn.execute("ALTER TABLE listing_lifecycle ADD COLUMN last_checked_at TEXT")
    conn.execute("ALTER TABLE listing_lifecycle ADD COLUMN final_status TEXT")
    conn.commit()


def test_migration_10_to_11_adds_tonnel_floor_snapshots_table_without_losing_data():
    """КАК ТЕСТИРОВАТЬ item 9: migration 10 -> 11, data not lost."""
    conn = sqlite3.connect(":memory:")
    _create_v10_schema(conn)

    db._migrate(conn)

    assert db._table_exists(conn, "tonnel_floor_snapshots")
    tonnel_cols = db._table_columns(conn, "tonnel_floor_snapshots")
    assert "listing_external_id" in tonnel_cols
    assert "tonnel_floor_with_fee_nano" in tonnel_cols
    assert "cross_verdict" not in tonnel_cols  # that field lives on Signal/notification, not this table

    # Pre-existing v10 data untouched.
    alerts_row = conn.execute("SELECT listing_external_id, status FROM alerts_sent").fetchone()
    assert alerts_row == ("ext-42", "sent")
    lifecycle_row = conn.execute(
        "SELECT listing_external_id, collection_id FROM listing_lifecycle WHERE listing_external_id = 'ext-99'"
    ).fetchone()
    assert lifecycle_row == ("ext-99", "col-a")

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def test_migration_10_to_11_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v10_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "table already exists" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v11_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 11 --
    tonnel_floor_snapshots exists, but its tonnel_implausible column
    (Правка 3, TONNEL_MAX_RATIO) does not yet.
    """
    _create_v10_schema(conn)
    conn.execute("UPDATE schema_version SET version = 11")
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
        "INSERT INTO tonnel_floor_snapshots "
        "(listing_external_id, collection_name, model_name, backdrop_name, "
        "tonnel_floor_nano, tonnel_floor_with_fee_nano, tonnel_listed_count, tonnel_status, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("ext-tonnel-1", "Input Key", "Gold Star", "Amber", 60_000_000_000, 66_000_000_000, 2, "ok",
         "2026-09-09T12:00:00+00:00"),
    )
    conn.commit()


def test_migration_11_to_12_adds_tonnel_implausible_column_without_losing_data():
    """КАК ТЕСТИРОВАТЬ (this delivery) item: migration 11 -> 12, data not lost."""
    conn = sqlite3.connect(":memory:")
    _create_v11_schema(conn)

    db._migrate(conn)

    tonnel_cols = db._table_columns(conn, "tonnel_floor_snapshots")
    assert "tonnel_implausible" in tonnel_cols

    row = conn.execute(
        "SELECT listing_external_id, tonnel_floor_with_fee_nano, tonnel_implausible "
        "FROM tonnel_floor_snapshots WHERE listing_external_id = 'ext-tonnel-1'"
    ).fetchone()
    assert row == ("ext-tonnel-1", 66_000_000_000, 0)  # pre-existing row defaults to not-implausible

    alerts_row = conn.execute("SELECT listing_external_id, status FROM alerts_sent").fetchone()
    assert alerts_row == ("ext-42", "sent")

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def test_migration_11_to_12_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v11_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "duplicate column name" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def _create_v12_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the DB as it looked at schema version 12 -- before
    marketplace was added to floor_snapshots/listing_lifecycle and before
    price_history/listing_lifecycle's PRIMARY KEY widened to include it
    (this delivery, the Tonnel full collector).
    """
    _create_v11_schema(conn)
    conn.execute("UPDATE schema_version SET version = 12")
    conn.execute("ALTER TABLE tonnel_floor_snapshots ADD COLUMN tonnel_implausible INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "INSERT INTO listing_lifecycle "
        "(listing_external_id, collection_id, model_name, backdrop_name, first_seen_at, last_seen_at, "
        "disappeared_at, reappeared_count) VALUES (?,?,?,?,?,?,?,?)",
        ("ext-lifecycle-1", "col-a", "Model", "Backdrop", "2026-01-02T00:00:00+00:00",
         "2026-01-02T00:05:00+00:00", None, 0),
    )
    conn.execute(
        "INSERT INTO price_history "
        "(listing_external_id, marketplace, old_price_nano, new_price_nano, delta_pct, is_noise, "
        "old_listed_at, new_listed_at, observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("ext-ph-1", "portals", 90_000_000_000, 80_000_000_000, "-11.1", 0, None, None,
         "2026-01-02T00:10:00+00:00"),
    )
    conn.commit()


def test_migration_12_to_13_widens_keys_without_losing_data():
    """КАК ТЕСТИРОВАТЬ item 7: migration 12 -> 13, data not lost, old rows
    get marketplace='portals'.
    """
    conn = sqlite3.connect(":memory:")
    _create_v12_schema(conn)

    db._migrate(conn)

    floor_cols = db._table_columns(conn, "floor_snapshots")
    assert "marketplace" in floor_cols
    lifecycle_cols = db._table_columns(conn, "listing_lifecycle")
    assert "marketplace" in lifecycle_cols
    ph_cols = db._table_columns(conn, "price_history")
    assert "marketplace" in ph_cols

    lifecycle_row = conn.execute(
        "SELECT marketplace, listing_external_id, collection_id FROM listing_lifecycle "
        "WHERE listing_external_id = 'ext-lifecycle-1'"
    ).fetchone()
    assert lifecycle_row == ("portals", "ext-lifecycle-1", "col-a")

    ph_row = conn.execute(
        "SELECT marketplace, listing_external_id, old_price_nano, new_price_nano FROM price_history "
        "WHERE listing_external_id = 'ext-ph-1'"
    ).fetchone()
    assert ph_row == ("portals", "ext-ph-1", 90_000_000_000, 80_000_000_000)

    alerts_row = conn.execute("SELECT listing_external_id, status FROM alerts_sent").fetchone()
    assert alerts_row == ("ext-42", "sent")

    # Composite PK actually enforced: same external_id, different
    # marketplace, does not collide.
    conn.execute(
        "INSERT INTO listing_lifecycle "
        "(marketplace, listing_external_id, collection_id, model_name, backdrop_name, "
        "first_seen_at, last_seen_at) VALUES ('tonnel', 'ext-lifecycle-1', 'gift-name', 'M', 'B', "
        "'2026-01-02T00:00:00+00:00', '2026-01-02T00:05:00+00:00')"
    )
    count = conn.execute(
        "SELECT COUNT(*) FROM listing_lifecycle WHERE listing_external_id = 'ext-lifecycle-1'"
    ).fetchone()[0]
    assert count == 2

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def test_migration_12_to_13_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v12_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "table already exists" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION

    ph_row = conn.execute(
        "SELECT marketplace FROM price_history WHERE listing_external_id = 'ext-ph-1'"
    ).fetchone()
    assert ph_row == ("portals",)  # not duplicated by the second migration pass


def _create_v13_broken_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs the REAL production bug: schema_version says 13 (the
    migration "ran"), price_history HAS a marketplace column (it always
    did, since _migration_3_to_4), but its PRIMARY KEY was never widened
    -- because the original (buggy) _migration_12_to_13 checked column
    PRESENCE, which was already true, so its rebuild branch never fired.
    listing_lifecycle, by contrast, genuinely did get its PK fixed by the
    original code (it never had a pre-existing marketplace column to
    trip the same bug) -- reproduced here in its CORRECT v13 shape, to
    prove _migration_13_to_14 leaves an already-correct table alone.
    """
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (13, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        """
        CREATE TABLE listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace TEXT NOT NULL,
            external_id TEXT NOT NULL,
            price_nano INTEGER,
            currency TEXT NOT NULL,
            listed_at TEXT,
            first_seen_at TEXT NOT NULL,
            UNIQUE(marketplace, external_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO listings (marketplace, external_id, price_nano, currency, listed_at, first_seen_at) "
        "VALUES ('portals', 'ext-42', 20000000000, 'TON', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    # The bug itself: marketplace column present, but PK is the OLD shape.
    conn.execute(
        """
        CREATE TABLE price_history (
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
            PRIMARY KEY(listing_external_id, observed_at)
        )
        """
    )
    conn.execute(
        "INSERT INTO price_history "
        "(listing_external_id, marketplace, old_price_nano, new_price_nano, delta_pct, is_noise, "
        "old_listed_at, new_listed_at, observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("ext-ph-1", "portals", 90_000_000_000, 80_000_000_000, "-11.1", 0, None, None,
         "2026-01-02T00:10:00+00:00"),
    )
    # listing_lifecycle: CORRECT shape already (the part of v12->13 that worked).
    conn.execute(
        """
        CREATE TABLE listing_lifecycle (
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
        "INSERT INTO listing_lifecycle "
        "(marketplace, listing_external_id, collection_id, model_name, backdrop_name, first_seen_at, last_seen_at) "
        "VALUES ('portals', 'ext-lifecycle-1', 'col-a', 'Model', 'Backdrop', "
        "'2026-01-02T00:00:00+00:00', '2026-01-02T00:05:00+00:00')"
    )
    conn.execute(
        """
        CREATE TABLE floor_snapshots (
            listing_external_id TEXT PRIMARY KEY,
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
            model_floor_status TEXT NOT NULL DEFAULT 'no_data'
        )
        """
    )
    conn.execute(
        "INSERT INTO floor_snapshots (listing_external_id, model_name, backdrop_name, "
        "floor_fetched_at, floor_age_sec, raw_model_block, pair_floor_status) "
        "VALUES ('ext-42', 'ModelZ', 'Copper', '2026-01-01T00:00:00+00:00', 0, '{}', 'ok')"
    )
    conn.execute(
        """
        CREATE TABLE alerts_sent (
            listing_external_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            PRIMARY KEY(listing_external_id, observed_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE tonnel_floor_snapshots (
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
        )
        """
    )
    conn.commit()


def test_migration_13_to_14_fixes_broken_price_history_pk_reproducing_prod_bug():
    """КАК ТЕСТИРОВАТЬ items 1-3: reproduces the EXACT reported crash --
    schema_version=13, price_history has `marketplace` but the wrong PK
    -- then verifies record_price_change() (the real write path, not a
    raw SQL check) works for both marketplaces after migrating.
    """
    conn = sqlite3.connect(":memory:")
    _create_v13_broken_schema(conn)

    # Before the fix: this is the exact crash reported live.
    assert db._table_pk_columns(conn, "price_history") == ("listing_external_id", "observed_at")

    db._migrate(conn)

    assert db._table_pk_columns(conn, "price_history") == ("marketplace", "listing_external_id", "observed_at")
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION

    # КАК ТЕСТИРОВАТЬ item 1: real write path, both marketplaces, no crash.
    from datetime import datetime, timezone
    from decimal import Decimal

    observed_at = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    db.record_price_change(
        conn, "portals", "ext-99",
        old_price_nano=100, new_price_nano=90, delta_pct=Decimal("-10"),
        is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
    )
    db.record_price_change(
        conn, "tonnel", "ext-99",
        old_price_nano=200, new_price_nano=190, delta_pct=Decimal("-5"),
        is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
    )

    # КАК ТЕСТИРОВАТЬ item 2: same listing_external_id + observed_at,
    # different marketplace -> two rows, not a conflict.
    rows = conn.execute(
        "SELECT marketplace, old_price_nano, new_price_nano FROM price_history "
        "WHERE listing_external_id = 'ext-99' ORDER BY marketplace"
    ).fetchall()
    assert rows == [("portals", 100, 90), ("tonnel", 200, 190)]

    # КАК ТЕСТИРОВАТЬ item 3: repeat write, same (marketplace,
    # listing_external_id, observed_at) -> DO NOTHING, no duplicate.
    db.record_price_change(
        conn, "portals", "ext-99",
        old_price_nano=100, new_price_nano=90, delta_pct=Decimal("-10"),
        is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=observed_at,
    )
    count = conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE listing_external_id='ext-99' AND marketplace='portals'"
    ).fetchone()[0]
    assert count == 1

    # КАК ТЕСТИРОВАТЬ item 4: pre-existing v13 data intact, backfilled 'portals'.
    ph_row = conn.execute(
        "SELECT marketplace, old_price_nano, new_price_nano FROM price_history WHERE listing_external_id='ext-ph-1'"
    ).fetchone()
    assert ph_row == ("portals", 90_000_000_000, 80_000_000_000)
    listing_row = conn.execute("SELECT external_id, price_nano FROM listings").fetchone()
    assert listing_row == ("ext-42", 20000000000)

    # listing_lifecycle was already correct pre-migration -- left alone.
    lifecycle_row = conn.execute(
        "SELECT marketplace, listing_external_id, collection_id FROM listing_lifecycle"
    ).fetchone()
    assert lifecycle_row == ("portals", "ext-lifecycle-1", "col-a")


def test_migration_13_to_14_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v13_broken_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "table already exists" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION
    ph_row = conn.execute(
        "SELECT marketplace FROM price_history WHERE listing_external_id = 'ext-ph-1'"
    ).fetchone()
    assert ph_row == ("portals",)  # not duplicated by the second migration pass


def test_migration_13_to_14_leaves_already_correct_pk_untouched():
    """A DB that genuinely reached v13 correctly (e.g. via a fresh
    _migration_12_to_13 run with today's fixed code) must not be
    rebuilt again -- _ensure_price_history_marketplace_pk's guard must
    return immediately when the PK already matches.
    """
    conn = sqlite3.connect(":memory:")
    _create_v12_schema(conn)
    db._migrate(conn)  # brings it correctly through 13 AND 14 in one pass

    ph_row_count_before = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]

    db._migrate(conn)  # a second full pass -- must be a total no-op

    ph_row_count_after = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    assert ph_row_count_after == ph_row_count_before


def _create_v14_broken_schema(conn: sqlite3.Connection) -> None:
    """Reconstructs a DB at schema_version=14 with floor_snapshots and
    alerts_sent still at their OLD (single/double-column) PRIMARY KEY --
    the state _migration_14_to_15 must fix. Both tables already have a
    `marketplace` column (added at v13), same shape as the real
    price_history bug this delivery's predecessor fixed.
    """
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (14, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        """
        CREATE TABLE listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            marketplace TEXT NOT NULL,
            external_id TEXT NOT NULL,
            price_nano INTEGER,
            currency TEXT NOT NULL,
            listed_at TEXT,
            first_seen_at TEXT NOT NULL,
            UNIQUE(marketplace, external_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE floor_snapshots (
            listing_external_id TEXT PRIMARY KEY,
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
            model_floor_status TEXT NOT NULL DEFAULT 'no_data'
        )
        """
    )
    conn.execute(
        "INSERT INTO floor_snapshots (listing_external_id, model_name, backdrop_name, "
        "floor_fetched_at, floor_age_sec, raw_model_block, pair_floor_status) "
        "VALUES ('ext-42', 'ModelZ', 'Copper', '2026-01-01T00:00:00+00:00', 0, '{}', 'ok')"
    )
    conn.execute(
        """
        CREATE TABLE alerts_sent (
            listing_external_id TEXT NOT NULL,
            marketplace TEXT NOT NULL DEFAULT 'portals',
            observed_at TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'sent',
            PRIMARY KEY(listing_external_id, observed_at)
        )
        """
    )
    conn.execute(
        "INSERT INTO alerts_sent (listing_external_id, observed_at, sent_at, status) "
        "VALUES ('ext-42', '2026-01-01T00:00:00+00:00', '2026-01-01T00:05:00+00:00', 'sent')"
    )
    conn.execute(
        """
        CREATE TABLE price_history (
            listing_external_id TEXT NOT NULL,
            marketplace TEXT NOT NULL,
            old_price_nano INTEGER NOT NULL,
            new_price_nano INTEGER NOT NULL,
            delta_pct NUMERIC NOT NULL,
            is_noise INTEGER NOT NULL DEFAULT 0,
            old_listed_at TEXT,
            new_listed_at TEXT,
            observed_at TEXT NOT NULL,
            PRIMARY KEY(marketplace, listing_external_id, observed_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE listing_lifecycle (
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
        """
        CREATE TABLE tonnel_floor_snapshots (
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
        )
        """
    )
    conn.commit()


def test_migration_14_to_15_widens_floor_snapshots_and_alerts_sent_pk():
    """КАК ТЕСТИРОВАТЬ (this delivery): floor_snapshots and alerts_sent
    widen to include marketplace, data preserved.
    """
    conn = sqlite3.connect(":memory:")
    _create_v14_broken_schema(conn)

    assert db._table_pk_columns(conn, "floor_snapshots") == ("listing_external_id",)
    assert db._table_pk_columns(conn, "alerts_sent") == ("listing_external_id", "observed_at")

    db._migrate(conn)

    assert db._table_pk_columns(conn, "floor_snapshots") == ("marketplace", "listing_external_id")
    assert db._table_pk_columns(conn, "alerts_sent") == ("marketplace", "listing_external_id", "observed_at")
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION

    floor_row = conn.execute(
        "SELECT marketplace, listing_external_id, pair_floor_status FROM floor_snapshots WHERE listing_external_id='ext-42'"
    ).fetchone()
    assert tuple(floor_row) == ("portals", "ext-42", "ok")

    alert_row = conn.execute(
        "SELECT marketplace, listing_external_id, status FROM alerts_sent WHERE listing_external_id='ext-42'"
    ).fetchone()
    assert tuple(alert_row) == ("portals", "ext-42", "sent")

    # Composite PK actually enforced: same external_id, different marketplace, no collision.
    conn.execute(
        "INSERT INTO floor_snapshots (listing_external_id, marketplace, model_name, floor_fetched_at, "
        "floor_age_sec, raw_model_block) VALUES ('ext-42', 'tonnel', 'M', '2026-01-01T00:00:00+00:00', 0, '{}')"
    )
    count = conn.execute("SELECT COUNT(*) FROM floor_snapshots WHERE listing_external_id='ext-42'").fetchone()[0]
    assert count == 2


def test_migration_14_to_15_is_idempotent():
    conn = sqlite3.connect(":memory:")
    _create_v14_broken_schema(conn)

    db._migrate(conn)
    db._migrate(conn)  # must not raise "table already exists" or similar

    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION
    count = conn.execute("SELECT COUNT(*) FROM floor_snapshots").fetchone()[0]
    assert count == 1  # not duplicated by the second migration pass


def test_full_migration_chain_from_v3_then_real_writes_succeed_for_every_on_conflict_table():
    """Правка: "тест на класс, а не на случай" -- this is that test.
    Builds a DB via the REAL migration chain (v3 through
    CURRENT_SCHEMA_VERSION, exactly as db.connect() would encounter on a
    genuinely old file -- NOT hand-built at the final shape), then
    exercises EVERY db.py write path that uses "INSERT ... ON CONFLICT"
    with real data. If any ON CONFLICT column list ever drifts from the
    table's actual PRIMARY KEY/UNIQUE index again -- for ANY of these
    tables, not just price_history -- this test fails with the exact
    same sqlite3.OperationalError a real poller would hit, instead of
    silently passing like the column-presence-only checks that let this
    bug ship three times (api_combo_floor_nano, report.py bypassing
    db.connect(), and this ON CONFLICT mismatch).
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    from gift_sniper.models import FloorSnapshot, Listing

    conn = sqlite3.connect(":memory:")
    _create_v3_schema(conn)
    # _create_v3_schema's `listings` is deliberately trimmed for the
    # floor_snapshots/price_history-focused fixtures above -- migrations
    # never ALTER `listings` (it has always had these columns, since
    # before the migration system existed), so a real DB always has them
    # even at "v3". Added here, not in _create_v3_schema itself, to
    # avoid changing what the OTHER tests in this file rely on it for.
    for col, decl in (
        ("tg_id", "TEXT"), ("collection_id", "TEXT"), ("collection_name", "TEXT"),
        ("gift_number", "INTEGER"), ("collection_floor_nano", "INTEGER"),
        ("model_name", "TEXT"), ("symbol_name", "TEXT"), ("backdrop_name", "TEXT"),
        ("model_rarity_raw", "TEXT"), ("symbol_rarity_raw", "TEXT"), ("backdrop_rarity_raw", "TEXT"),
        ("image_url", "TEXT"), ("animation_url", "TEXT"), ("unlocks_at", "TEXT"),
        ("status", "TEXT"), ("raw", "TEXT"),
    ):
        conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {decl}")
    conn.commit()

    db._migrate(conn)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == db.CURRENT_SCHEMA_VERSION

    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    # listings + floor_snapshots (upsert_listing_with_floor: 2 ON CONFLICT targets)
    listing = Listing(
        marketplace="portals", external_id="chain-1", tg_id="X-1", collection_id="col-a",
        collection_name="Coll", gift_number=1, price_nano=1_000_000_000, currency="TON",
        collection_floor_nano=None, model_name="M", symbol_name=None, backdrop_name="B",
        model_rarity_raw=None, symbol_rarity_raw=None, backdrop_rarity_raw=None,
        image_url=None, animation_url=None, listed_at=None, unlocks_at=None,
        status="listed", first_seen_at=now, raw={},
    )
    snapshot = FloorSnapshot(
        listing_external_id="chain-1", model_name="M", backdrop_name="B",
        api_combo_floor_nano=None, model_min_floor_nano=None, floor_fetched_at=now,
        floor_age_sec=0, raw_model_block={}, pair_floor_status="pending",
    )
    db.upsert_listing_with_floor(conn, listing, snapshot)
    db.upsert_listing_with_floor(conn, listing, snapshot)  # repeat -- must DO NOTHING, not raise
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE external_id='chain-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM floor_snapshots WHERE listing_external_id='chain-1'").fetchone()[0] == 1

    # insert_listing (listings only, Tonnel's path)
    tonnel_listing = Listing(
        marketplace="tonnel", external_id="chain-1", tg_id=None, collection_id=None,
        collection_name="Coll", gift_number=1, price_nano=2_000_000_000, currency="TON",
        collection_floor_nano=None, model_name="M", symbol_name=None, backdrop_name="B",
        model_rarity_raw=None, symbol_rarity_raw=None, backdrop_rarity_raw=None,
        image_url=None, animation_url=None, listed_at=None, unlocks_at=None,
        status="forsale", first_seen_at=now, raw={},
    )
    db.insert_listing(conn, tonnel_listing)  # same external_id, DIFFERENT marketplace -> must succeed, not collide
    db.insert_listing(conn, tonnel_listing)  # repeat -- must DO NOTHING
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE external_id='chain-1'").fetchone()[0] == 2

    # price_history (the reported bug)
    db.record_price_change(
        conn, "portals", "chain-1", old_price_nano=100, new_price_nano=90,
        delta_pct=Decimal("-10"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=now,
    )
    db.record_price_change(
        conn, "tonnel", "chain-1", old_price_nano=200, new_price_nano=190,
        delta_pct=Decimal("-5"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=now,
    )
    db.record_price_change(
        conn, "portals", "chain-1", old_price_nano=100, new_price_nano=90,
        delta_pct=Decimal("-10"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=now,
    )
    assert conn.execute("SELECT COUNT(*) FROM price_history WHERE listing_external_id='chain-1'").fetchone()[0] == 2

    # alerts_sent -- two marketplaces, same listing_external_id/observed_at, no collision
    db.mark_alert_sent(conn, "portals", "chain-1", now, now)
    db.mark_alert_sent(conn, "portals", "chain-1", now, now)  # repeat -- must DO NOTHING
    db.mark_alert_sent(conn, "tonnel", "chain-1", now, now)
    assert conn.execute("SELECT COUNT(*) FROM alerts_sent WHERE listing_external_id='chain-1'").fetchone()[0] == 2

    # floor_snapshots -- Tonnel's at-drop pair floor write, Portals' row untouched
    db.upsert_tonnel_model_floor_snapshot(conn, "chain-1", "M", "B", 900_000_000, 2, "ok", now)
    db.upsert_tonnel_model_floor_snapshot(conn, "chain-1", "M", "B", 850_000_000, 3, "ok", now)  # re-check -- UPDATE, not a new row
    assert conn.execute(
        "SELECT COUNT(*) FROM floor_snapshots WHERE listing_external_id='chain-1'"
    ).fetchone()[0] == 2  # one portals row (from upsert_listing_with_floor) + one tonnel row
    tonnel_floor_row = conn.execute(
        "SELECT model_floor_excl_self_nano, model_listed_count_excl_self FROM floor_snapshots "
        "WHERE listing_external_id='chain-1' AND marketplace='tonnel'"
    ).fetchone()
    assert tuple(tonnel_floor_row) == (850_000_000, 3)  # the re-check's values, not the first check's

    # tonnel_floor_snapshots
    db.record_tonnel_floor_snapshot(
        conn, "chain-1", "Coll", "M", "B", 1_000_000_000, 1_100_000_000, 1, "ok", now,
    )
    db.record_tonnel_floor_snapshot(
        conn, "chain-1", "Coll", "M", "B", 1_000_000_000, 1_100_000_000, 1, "ok", now,
    )  # repeat -- must DO NOTHING
    assert conn.execute(
        "SELECT COUNT(*) FROM tonnel_floor_snapshots WHERE listing_external_id='chain-1'"
    ).fetchone()[0] == 1

    # cross_check_snapshots (two-way cross-check delivery, schema v16;
    # PK widened again in v17 -- see below)
    db.record_cross_check_snapshot(
        conn, "portals", "tonnel", "chain-1", "Coll", "M", "B", 1_000_000_000, 1, "confirmed", now,
    )
    db.record_cross_check_snapshot(
        conn, "portals", "tonnel", "chain-1", "Coll", "M", "B", 1_000_000_000, 1, "confirmed", now,
    )  # repeat -- must DO NOTHING
    db.record_cross_check_snapshot(
        conn, "tonnel", "portals", "chain-1", "Coll", "M", "B", 900_000_000, 2, "worse", now,
    )
    # MRKT third-neighbour delivery (schema v17): a SECOND neighbour
    # checked for the SAME signal_marketplace/listing_external_id/
    # fetched_at as the row above (this is exactly what a real
    # cross_check() call does -- Tonnel AND MRKT both queried with the
    # same `now`) -- must NOT collide with the Portals->Tonnel row.
    db.record_cross_check_snapshot(
        conn, "portals", "mrkt", "chain-1", "Coll", "M", "B", 1_100_000_000, 5, "sent_neighbour_higher", now,
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM cross_check_snapshots WHERE listing_external_id='chain-1'"
    ).fetchone()[0] == 3  # tonnel + portals + mrkt neighbours, no collision

    # processed_events (MRKT full-signaller delivery, schema v18) --
    # КАК ТЕСТИРОВАТЬ item 10: v17 -> v18, no data lost, new table usable.
    db.mark_event_processed(conn, "mrkt", "evt-1", now)
    db.mark_event_processed(conn, "mrkt", "evt-1", now)  # repeat -- must DO NOTHING
    db.mark_event_processed(conn, "mrkt", "evt-2", now)
    assert conn.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0] == 2
    assert db.is_event_processed(conn, "mrkt", "evt-1") is True
    assert db.is_event_processed(conn, "mrkt", "evt-999") is False

    # listing_lifecycle.sold_price_nano (schema v18) -- new column must
    # be writable on a table that already existed pre-migration.
    db.touch_listing_lifecycle(conn, "mrkt", "chain-1", "col-a", "M", "B", 2_000_000_000, now)
    db.record_sale(conn, "mrkt", "chain-1", 1_500_000_000, now)
    sold_row = conn.execute(
        "SELECT final_status, sold_price_nano FROM listing_lifecycle "
        "WHERE marketplace='mrkt' AND listing_external_id='chain-1'"
    ).fetchone()
    assert sold_row == ("sold", 1_500_000_000)


def test_on_conflict_mismatch_is_actually_detectable_by_sqlite():
    """Proves the failure mode the previous test guards against is real
    and would actually be CAUGHT (КАК ТЕСТИРОВАТЬ item 5, "падает, если
    добавить лишнее поле"): an ON CONFLICT column list that does not
    exactly match a real PRIMARY KEY/UNIQUE index raises
    sqlite3.OperationalError, on the current, fully-migrated schema --
    the same error class as the reported crash.
    """
    conn = sqlite3.connect(":memory:")
    _create_v3_schema(conn)
    db._migrate(conn)

    with __import__("pytest").raises(sqlite3.OperationalError, match="ON CONFLICT"):
        conn.execute(
            """
            INSERT INTO price_history (
                listing_external_id, marketplace, old_price_nano, new_price_nano,
                delta_pct, is_noise, old_listed_at, new_listed_at, observed_at
            ) VALUES ('x', 'portals', 1, 1, '0', 0, NULL, NULL, '2026-01-01T00:00:00+00:00')
            ON CONFLICT(marketplace, listing_external_id, observed_at, is_noise) DO NOTHING
            """
        )


def test_fresh_db_reaches_latest_version_without_running_migrations(monkeypatch):
    called = {"n": 0}

    def spy(conn):
        called["n"] += 1

    monkeypatch.setattr(db, "MIGRATIONS", [(2, spy)])

    conn = db.connect(":memory:")

    assert called["n"] == 0  # a brand-new DB is created at the latest
    # shape directly by SCHEMA -- no migration function should run.
    version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert version == db.CURRENT_SCHEMA_VERSION


def test_unknown_column_layout_raises_clear_schema_error():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE floor_snapshots (listing_external_id TEXT PRIMARY KEY, mystery_column TEXT)"
    )
    conn.commit()

    try:
        db._migrate(conn)
        assert False, "expected SchemaError"
    except db.SchemaError as exc:
        assert "floor_snapshots" in str(exc)


def test_verify_schema_passes_on_freshly_connected_db():
    conn = db.connect(":memory:")
    db.verify_schema(conn)  # must not raise


def test_verify_schema_raises_on_missing_column():
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA)
    conn.execute("DROP TABLE floor_snapshots")
    conn.execute(
        "CREATE TABLE floor_snapshots (listing_external_id TEXT PRIMARY KEY, model_name TEXT)"
    )
    conn.commit()

    try:
        db.verify_schema(conn)
        assert False, "expected SchemaError"
    except db.SchemaError as exc:
        assert "floor_snapshots" in str(exc)
        assert "missing" in str(exc).lower()
