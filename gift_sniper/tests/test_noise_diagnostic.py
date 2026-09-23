from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.noise_diagnostic import generate_diagnostic, main
from .test_price_drops_report import _listing, _snapshot


def test_generate_diagnostic_runs_cleanly_on_empty_db():
    conn = db.connect(":memory:")
    text = generate_diagnostic(conn)
    assert "=== is_noise diagnostic ===" in text
    assert "last 1h: 0 drops" in text
    assert "last 24h: 0 rows" in text


def test_generate_diagnostic_buckets_significant_drops_by_size():
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    listing = _listing("noise-diag-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    # A significant (is_noise=0) 2.9% drop -> bucket "1-3%".
    db.record_price_change(
        conn, "portals", "noise-diag-1",
        old_price_nano=int(Decimal("41.19") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-2.9"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=now - timedelta(hours=1),
    )
    # A noise (is_noise=1) 0.16% drop -- must NOT show up in the is_noise=0 bucket table.
    db.record_price_change(
        conn, "portals", "noise-diag-1",
        old_price_nano=int(Decimal("40.06") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-0.16"),
        is_noise=True,
        old_listed_at=None, new_listed_at=None,
        observed_at=now - timedelta(minutes=30),
    )

    text = generate_diagnostic(conn, now=now)
    assert "last 24h: 1 rows with is_noise=0" in text
    lines = {l.split()[0]: l.split()[-1] for l in text.splitlines() if l.strip().startswith(("<", "0.", "1-", ">"))}
    assert lines["1-3%"] == "1"
    assert lines["<0.1%"] == "0"


def test_generate_diagnostic_last_hour_percentage_matches_manual_count():
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)

    listing = _listing("noise-diag-2", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    for i in range(9):
        db.record_price_change(
            conn, "portals", "noise-diag-2",
            old_price_nano=int(Decimal("40.06") * config.NANO),
            new_price_nano=int(Decimal("40.0") * config.NANO),
            delta_pct=Decimal("-0.16"),
            is_noise=True,
            old_listed_at=None, new_listed_at=None,
            observed_at=now - timedelta(minutes=i + 1),
        )
    db.record_price_change(
        conn, "portals", "noise-diag-2",
        old_price_nano=int(Decimal("41.19") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-2.9"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=now - timedelta(minutes=15),
    )

    text = generate_diagnostic(conn, now=now)
    assert "last 1h: 10 drops, 1 is_noise=0 (significant) -- 10.0%" in text


def test_noise_diagnostic_cli_runs_end_to_end(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    conn.close()

    exit_code = main(["--db", db_path])
    assert exit_code == 0
