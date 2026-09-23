from datetime import datetime, timedelta, timezone

from gift_sniper import db
from gift_sniper.lifecycle_reset import main


def test_reset_lifecycle_data_clears_corrupted_fields_preserves_seen_at():
    """КАК ТЕСТИРОВАТЬ item 6: lifecycle_reset.py resets the right fields
    and preserves the rest.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    first_seen = now - timedelta(days=2)

    db.touch_listing_lifecycle(conn, "portals", "corrupt-1", "col-a", "Model", "Backdrop", 1000, first_seen)
    db.record_lifecycle_status(conn, "portals", "corrupt-1", "withdrawn", now)
    db.touch_listing_lifecycle(conn, "portals", "corrupt-1", "col-a", "Model", "Backdrop", 950, now)  # reappears once

    row_before = conn.execute(
        "SELECT reappeared_count, first_seen_at, last_seen_at, collection_id, last_price_nano "
        "FROM listing_lifecycle WHERE listing_external_id = 'corrupt-1'"
    ).fetchone()
    assert row_before[0] == 1  # reappeared once -- confirms fixture is meaningful

    reset_count = db.reset_lifecycle_data(conn)
    assert reset_count == 1

    row_after = conn.execute(
        "SELECT disappeared_at, reappeared_count, final_status, last_checked_at, "
        "first_seen_at, last_seen_at, collection_id, last_price_nano "
        "FROM listing_lifecycle WHERE listing_external_id = 'corrupt-1'"
    ).fetchone()
    assert row_after[0] is None  # disappeared_at reset
    assert row_after[1] == 0  # reappeared_count reset
    assert row_after[2] is None  # final_status reset
    assert row_after[3] is None  # last_checked_at reset
    # Preserved:
    assert row_after[4] == row_before[1]  # first_seen_at
    assert row_after[5] == row_before[2]  # last_seen_at
    assert row_after[6] == row_before[3]  # collection_id
    assert row_after[7] == row_before[4]  # last_price_nano


def test_reset_lifecycle_data_reports_count_for_multiple_rows():
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    for i in range(4):
        db.touch_listing_lifecycle(conn, "portals", f"row-{i}", "col-a", "Model", "Backdrop", 1000, now)

    reset_count = db.reset_lifecycle_data(conn)
    assert reset_count == 4


def test_lifecycle_reset_cli_runs_end_to_end(tmp_path):
    """python -m gift_sniper.lifecycle_reset --db <path> works end to
    end against a real on-disk DB and prints the reset count.
    """
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    now = datetime.now(timezone.utc)
    db.touch_listing_lifecycle(conn, "portals", "cli-1", "col-a", "Model", "Backdrop", 1000, now)
    db.record_lifecycle_status(conn, "portals", "cli-1", "withdrawn", now)
    conn.close()

    exit_code = main(["--db", db_path])
    assert exit_code == 0

    conn2 = db.connect(db_path)
    row = conn2.execute(
        "SELECT disappeared_at FROM listing_lifecycle WHERE listing_external_id = 'cli-1'"
    ).fetchone()
    assert row[0] is None
