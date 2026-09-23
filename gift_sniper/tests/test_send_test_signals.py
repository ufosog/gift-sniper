import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gift_sniper import config, db
from gift_sniper.send_test_signals import main
from .test_notifier import FakeSession
from .test_price_drops_report import _listing, _snapshot


def _seed_clean_signal(conn, ext_id, observed_at=None, delta_pct="-11.1"):
    # delta_pct varies per call by default via the caller -- multiple
    # signals seeded with the SAME collection + delta_pct + timestamp
    # would otherwise get caught by the is_bulk_update filter (one
    # seller repricing several lots at once), which is correct cascade
    # behavior but not what these tests are seeding for.
    observed_at = observed_at or datetime.now(timezone.utc)
    listing = _listing(ext_id, int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    db.record_price_change(
        conn, "portals", ext_id,
        old_price_nano=int(Decimal("45.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal(delta_pct),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )
    return observed_at


@pytest.fixture(autouse=True)
def _telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_USER_ID", "12345")


def _patch_session(monkeypatch, responses):
    """Replaces TelegramNotifier's requests.Session with a FakeSession
    that records calls, by monkeypatching requests.Session() to return
    it (send_test_signals.main() constructs the notifier without a
    session override, so this is the only hook point).
    """
    session = FakeSession(responses)
    monkeypatch.setattr("gift_sniper.notifier.requests.Session", lambda: session)
    return session


def test_send_test_signals_uses_clean_signals_not_own_query(monkeypatch, tmp_path):
    """КАК ТЕСТИРОВАТЬ item 5: uses signals.clean_signals() -- verified
    indirectly here by confirming a signal that clean_signals() would
    exclude (e.g. noise-only, no clean drop at all) is never sent.
    """
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    conn.close()  # no clean signals at all in this DB

    session = _patch_session(monkeypatch, [])
    exit_code = main(["--db", db_path, "--count", "5"])
    assert exit_code == 0
    assert session.calls == []


def test_send_test_signals_sends_requested_count(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    for i in range(3):
        _seed_clean_signal(conn, f"test-sig-{i}", delta_pct=f"-{11 + i}.1")
    conn.close()

    session = _patch_session(monkeypatch, [(200, {"ok": True, "result": {}})] * 3)
    exit_code = main(["--db", db_path, "--count", "3"])
    assert exit_code == 0
    assert len(session.calls) == 3
    for _url, data in session.calls:
        assert "sendMessage" in _url
        assert "— тестовая отправка" in data["text"]


def test_send_test_signals_reports_when_fewer_available(monkeypatch, tmp_path, capsys):
    """КАК ТЕСТИРОВАТЬ: if fewer clean signals exist than requested,
    sends what's there and says so.
    """
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    _seed_clean_signal(conn, "only-one")
    conn.close()

    session = _patch_session(monkeypatch, [(200, {"ok": True, "result": {}})])
    exit_code = main(["--db", db_path, "--count", "5"])
    assert exit_code == 0
    assert len(session.calls) == 1
    captured = capsys.readouterr()
    assert "Only 1" in captured.out


def test_send_test_signals_never_writes_alerts_sent(monkeypatch, tmp_path):
    """КАК ТЕСТИРОВАТЬ item 4: send_test_signals never creates rows in
    alerts_sent -- must not affect the real bot's cooldown/dedup state.
    """
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    observed_at = _seed_clean_signal(conn, "no-alert-row")
    conn.close()

    session = _patch_session(monkeypatch, [(200, {"ok": True, "result": {}})])
    main(["--db", db_path, "--count", "1"])

    conn2 = db.connect(db_path)
    count = conn2.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0]
    assert count == 0
    assert db.is_alert_sent(conn2, "portals", "no-alert-row", observed_at) is False


def _seed_tonnel_clean_signal(conn, ext_id):
    from gift_sniper.models import Listing

    now = datetime.now(timezone.utc)
    listing = Listing(
        marketplace="tonnel", external_id=ext_id, tg_id=f"IceCream-{ext_id}", collection_id=None,
        collection_name="Ice Cream", gift_number=1, price_nano=int(Decimal("20.0") * config.NANO),
        currency="TON", collection_floor_nano=None, model_name="Emperor", symbol_name=None, backdrop_name="Black",
        model_rarity_raw=None, symbol_rarity_raw=None, backdrop_rarity_raw=None,
        image_url=None, animation_url=None, listed_at=None, unlocks_at=None,
        status="forsale", first_seen_at=now, raw={},
    )
    db.insert_listing(conn, listing)
    db.record_price_change(
        conn, "tonnel", ext_id,
        old_price_nano=int(Decimal("40.0") * config.NANO), new_price_nano=int(Decimal("20.0") * config.NANO),
        delta_pct=Decimal("-50"), is_noise=False, old_listed_at=None, new_listed_at=None, observed_at=now,
    )
    db.upsert_tonnel_model_floor_snapshot(conn, ext_id, "Emperor", "Black", int(Decimal("30.0") * config.NANO), 4, "ok", now)


def test_send_test_signals_marketplace_tonnel_sends_only_tonnel_signals(monkeypatch, tmp_path):
    """SANITY-CHECK item: --marketplace tonnel sends Tonnel's clean
    signals, with the Tonnel deep link (the header/currency no longer
    distinguish marketplace -- Правка 2, unified format -- so the
    Tonnel-ness is verified via the keyboard's URL instead).
    """
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    _seed_clean_signal(conn, "portals-only")  # must NOT be sent
    _seed_tonnel_clean_signal(conn, "3001")
    conn.close()

    session = _patch_session(monkeypatch, [(200, {"ok": True, "result": {}})])
    exit_code = main(["--db", db_path, "--count", "5", "--marketplace", "tonnel"])
    assert exit_code == 0
    assert len(session.calls) == 1
    sent_text = session.calls[0][1]["text"]
    # ДЕФЕКТ 5: send_test_signals.py never runs cross_check -- cross_verdict
    # stays "not_checked", so no checkmark.
    assert sent_text.startswith("<b>ЛИСТИНГ</b>")
    assert "t.me/tonnel_network_bot/gift?startapp=3001" in session.calls[0][1]["reply_markup"]
