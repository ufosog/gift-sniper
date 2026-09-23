"""КАК ТЕСТИРОВАТЬ items 6-7 (realization-rate delivery, ПРАВКА 2):
unstable_floor cascade stage.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.signals import run_cascade
from .test_min_profit_and_stale_floor import _listing, _record_drop, _snapshot

OBSERVED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _nano(ton) -> int:
    return int(Decimal(str(ton)) * config.NANO)


def _pair_snapshot(conn, ext_id, floor, fetched_at):
    listing = _listing(ext_id, _nano(floor) + _nano(1))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, _nano(floor), floor_fetched_at=fetched_at))


def _candidate(conn):
    # price 15, floor 25, depth 5: profit 25*0.95*0.98 - 15 - 0.35 = 7.93,
    # ratio 1.67 -- clears every other stage.
    listing = _listing("cand-1", _nano(15))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, _nano(25), floor_fetched_at=OBSERVED_AT))
    _record_drop(conn, "portals", "cand-1", "18", "15", "-16.7", OBSERVED_AT, floor_at_drop="25")


def test_item6_five_snapshots_spread_2_5_dropped_at_unstable_floor(monkeypatch):
    monkeypatch.setattr(config, "FLOOR_MAX_INSTABILITY", Decimal("2.0"))
    conn = db.connect(":memory:")
    _candidate(conn)  # its own snapshot: 25
    for i, floor in enumerate([10, 14, 18, 22]):
        _pair_snapshot(conn, f"snap-{i}", floor, OBSERVED_AT - timedelta(hours=i + 1))

    cascade = run_cascade(conn, now=OBSERVED_AT, marketplace="portals")
    assert [r["listing_external_id"] for r in cascade.unstable_floor] == ["cand-1"]
    assert cascade.clean == []


def test_item7_two_snapshots_any_spread_stage_skipped():
    conn = db.connect(":memory:")
    _candidate(conn)  # 25
    _pair_snapshot(conn, "snap-0", 5, OBSERVED_AT - timedelta(hours=1))  # x5 spread, but only 2 snapshots

    cascade = run_cascade(conn, now=OBSERVED_AT, marketplace="portals")
    assert cascade.unstable_floor == []
    assert [r["listing_external_id"] for r in cascade.clean] == ["cand-1"]


def test_snapshots_outside_window_do_not_count():
    """Window is anchored to the drop's observed_at: snapshots older than
    FLOOR_STABILITY_WINDOW_HOURS before it never count, even with a wild
    spread -- leaving < 3 in-window snapshots, so the stage is skipped.
    """
    conn = db.connect(":memory:")
    _candidate(conn)
    _pair_snapshot(conn, "old-0", 5, OBSERVED_AT - timedelta(hours=config.FLOOR_STABILITY_WINDOW_HOURS + 1))
    _pair_snapshot(conn, "old-1", 6, OBSERVED_AT - timedelta(hours=config.FLOOR_STABILITY_WINDOW_HOURS + 2))
    _pair_snapshot(conn, "new-0", 24, OBSERVED_AT - timedelta(hours=1))

    cascade = run_cascade(conn, now=OBSERVED_AT, marketplace="portals")
    assert cascade.unstable_floor == []
    assert [r["listing_external_id"] for r in cascade.clean] == ["cand-1"]


def test_stable_pair_with_many_snapshots_passes():
    conn = db.connect(":memory:")
    _candidate(conn)
    for i, floor in enumerate([23, 24, 26, 27]):
        _pair_snapshot(conn, f"snap-{i}", floor, OBSERVED_AT - timedelta(hours=i + 1))

    cascade = run_cascade(conn, now=OBSERVED_AT, marketplace="portals")
    assert cascade.unstable_floor == []
    assert [r["listing_external_id"] for r in cascade.clean] == ["cand-1"]
