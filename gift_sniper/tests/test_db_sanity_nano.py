"""ЗАЩИТА ОТ ПОВТОРЕНИЯ (MRKT unit-conversion bug fix): db.py's
SANITY_MAX_NANO backstop -- a bad unit conversion upstream must never
crash the poller with sqlite3's own OverflowError.
"""
from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db

SANITY_MAX_NANO = db.SANITY_MAX_NANO


def test_absurd_neighbour_floor_nano_is_not_written(caplog):
    """КАК ТЕСТИРОВАТЬ item 3: an artificial 10**19 value -> the row is
    NOT written, a log entry names the offending marketplace/value, and
    no exception is raised (the old bug's OverflowError never happens).
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    with caplog.at_level("ERROR"):
        db.record_cross_check_snapshot(
            conn, "portals", "mrkt", "ext-1", "Coll", "M", "B",
            10 ** 19, 5, "sent_neighbour_higher", now,
        )  # must not raise

    count = conn.execute("SELECT COUNT(*) FROM cross_check_snapshots").fetchone()[0]
    assert count == 0
    assert any("mrkt" in r.message and "SANITY_MAX_NANO" in r.message for r in caplog.records)


def test_value_exactly_at_sanity_max_is_written():
    """The boundary itself is still accepted -- only values STRICTLY
    above SANITY_MAX_NANO are refused."""
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    db.record_cross_check_snapshot(
        conn, "portals", "mrkt", "ext-1", "Coll", "M", "B",
        SANITY_MAX_NANO, 5, "sent_neighbour_higher", now,
    )
    count = conn.execute("SELECT COUNT(*) FROM cross_check_snapshots").fetchone()[0]
    assert count == 1


def test_value_just_above_sanity_max_is_refused():
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    db.record_cross_check_snapshot(
        conn, "portals", "mrkt", "ext-1", "Coll", "M", "B",
        SANITY_MAX_NANO + 1, 5, "sent_neighbour_higher", now,
    )
    count = conn.execute("SELECT COUNT(*) FROM cross_check_snapshots").fetchone()[0]
    assert count == 0


def test_normal_value_from_any_marketplace_unaffected():
    """A real, sane floor value (well below SANITY_MAX_NANO) still
    writes normally, regardless of which marketplace is the neighbour.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    for checked_marketplace in ("tonnel", "portals", "mrkt"):
        signal_marketplace = "tonnel" if checked_marketplace == "portals" else "portals"
        db.record_cross_check_snapshot(
            conn, signal_marketplace, checked_marketplace, "ext-1", "Coll", "M", "B",
            int(Decimal("20.0") * config.NANO), 5, "sent_neighbour_higher", now,
        )
    count = conn.execute("SELECT COUNT(*) FROM cross_check_snapshots").fetchone()[0]
    assert count == 3


def test_none_neighbour_floor_nano_never_triggers_the_check():
    """None (no comparable neighbour at all) must not be compared against
    SANITY_MAX_NANO -- it's a valid, expected value (verdict
    "sent_no_neighbour"), not an absurd one.
    """
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    db.record_cross_check_snapshot(
        conn, "portals", "mrkt", "ext-1", "Coll", "M", "B",
        None, 0, "sent_no_neighbour", now,
    )
    count = conn.execute("SELECT COUNT(*) FROM cross_check_snapshots").fetchone()[0]
    assert count == 1
