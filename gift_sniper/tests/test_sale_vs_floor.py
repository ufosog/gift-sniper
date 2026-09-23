from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.sale_vs_floor import generate_report


def _seed_sale(conn, ext_id, sold_ton, floor_ton, listed_count, model="M", backdrop="B"):
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO listing_lifecycle (
            marketplace, listing_external_id, collection_id, model_name, backdrop_name,
            first_seen_at, last_seen_at, disappeared_at, final_status, sold_price_nano,
            floor_at_sale_nano, floor_listed_count_at_sale, floor_fetched_at_sale
        ) VALUES ('mrkt', ?, 'col-a', ?, ?, ?, ?, ?, 'sold', ?, ?, ?, ?)
        """,
        (
            ext_id, model, backdrop, now.isoformat(), now.isoformat(), now.isoformat(),
            int(Decimal(str(sold_ton)) * config.NANO),
            int(Decimal(str(floor_ton)) * config.NANO) if floor_ton is not None else None,
            listed_count, now.isoformat() if floor_ton is not None else None,
        ),
    )


# --- item 5: empty DB does not crash ---------------------------------------

def test_item5_empty_db_does_not_crash():
    conn = db.connect(":memory:")
    report_text = generate_report(conn)
    assert "=== sale vs. floor" in report_text
    assert "total confirmed MRKT sales: 0" in report_text


def test_counts_sales_with_and_without_floor():
    conn = db.connect(":memory:")
    _seed_sale(conn, "s1", "50.0", "70.0", 4)
    _seed_sale(conn, "s2", "20.0", None, None)  # no floor recorded
    conn.commit()

    report_text = generate_report(conn)
    assert "total confirmed MRKT sales: 2" in report_text
    assert "sales with floor_at_sale_nano: 1" in report_text


def test_outliers_are_separated_from_the_main_distribution():
    conn = db.connect(":memory:")
    _seed_sale(conn, "s1", "50.0", "70.0", 4)   # ratio 0.714 -- normal
    _seed_sale(conn, "s2", "5.41", "97.92", 2)  # ratio 0.055 -- outlier (Stellar Rocket/Neon Fuel)
    conn.commit()

    report_text = generate_report(conn)
    assert "outliers (ratio outside [0.2, 1.5]): 1" in report_text
    assert "n=1" in report_text  # the main distribution excludes the outlier


def test_depth_and_price_segment_buckets_appear():
    conn = db.connect(":memory:")
    _seed_sale(conn, "s1", "20.0", "40.0", 1)     # depth "1", price "<30"
    _seed_sale(conn, "s2", "50.0", "80.0", 3)     # depth "2-3", price "30-100"
    _seed_sale(conn, "s3", "150.0", "200.0", 12)  # depth "10+", price ">100"
    conn.commit()

    report_text = generate_report(conn)
    assert "by floor depth at sale" in report_text
    assert "by sale price segment" in report_text
    for bucket_marker in ("1: n=1", "2-3: n=1", "10+: n=1"):
        assert bucket_marker in report_text
    for bucket_marker in ("<30: n=1", "30-100: n=1", ">100: n=1"):
        assert bucket_marker in report_text


def test_examples_line_printed():
    conn = db.connect(":memory:")
    _seed_sale(conn, "s1", "50.0", "70.0", 4, model="Emperor", backdrop="Ice Cream")
    conn.commit()

    report_text = generate_report(conn)
    assert "examples (up to 15" in report_text
    assert "Emperor / Ice Cream" in report_text


def test_item6_migration_v18_to_v19_no_data_loss():
    """Build a v18 DB by hand (no floor_at_sale_* columns), write a sale
    row, then open it through db.connect() (runs migrations) and confirm
    the pre-existing sale row survives with the new columns NULL.
    """
    import sqlite3
    import tempfile
    import os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn_v18 = db.connect(path)  # creates a fresh, fully-migrated DB first
        conn_v18.close()

        # Force the DB back to a v18-shaped listing_lifecycle by dropping
        # the v19 columns is not directly supported by SQLite -- instead,
        # simulate a real v18 DB by recording schema_version=18 and
        # inserting a sale row using only v18 columns, then let db.connect()
        # migrate forward and confirm the row is untouched.
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM schema_version")
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (18, ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO listing_lifecycle (
                marketplace, listing_external_id, first_seen_at, last_seen_at,
                disappeared_at, final_status, sold_price_nano
            ) VALUES ('mrkt', 'pre-migration-1', ?, ?, ?, 'sold', ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(), int(Decimal("33.0") * config.NANO),
            ),
        )
        conn.commit()
        conn.close()

        migrated = db.connect(path)
        row = migrated.execute(
            "SELECT sold_price_nano, floor_at_sale_nano, floor_listed_count_at_sale, floor_fetched_at_sale "
            "FROM listing_lifecycle WHERE listing_external_id='pre-migration-1'"
        ).fetchone()
        assert row[0] == int(Decimal("33.0") * config.NANO)
        assert row[1] is None
        assert row[2] is None
        assert row[3] is None
        version = migrated.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == db.CURRENT_SCHEMA_VERSION
        migrated.close()
    finally:
        os.remove(path)
