"""КАК ТЕСТИРОВАТЬ 1-2 (notifications back): NOTIFY_LEVELS defaults to
"pair,model", and a model-level Portals signal above the profit threshold
is actually sent end-to-end -- while the profit check still applies.
"""
import inspect
from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from .test_notifier import FakeSession, _build_poller_with_notifier, _listing, _snapshot

DEFAULT_LEVELS = {lvl.strip() for lvl in "pair,model".split(",")}


def test_item1_default_contains_pair_and_model():
    assert '_env("NOTIFY_LEVELS", "pair,model")' in inspect.getsource(config)


def _seed_model_signal(conn, ext_id):
    listing = _listing(ext_id, int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    observed_at = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", ext_id,
        old_price_nano=int(Decimal("60.0") * config.NANO), new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-33.3"), is_noise=False, old_listed_at=None, new_listed_at=None,
        observed_at=observed_at, floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
        floor_listed_count_at_drop=5, floor_level_at_drop="model",
    )
    return observed_at


def test_item2_model_level_portals_signal_above_threshold_is_sent(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_LEVELS", DEFAULT_LEVELS)
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))

    conn = db.connect(":memory:")
    observed_at = _seed_model_signal(conn, "model-send-1")
    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids = {"model-send-1": {"id": "model-send-1", "status": "listed", "price": "40.0"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert db.is_alert_sent(conn, "portals", "model-send-1", observed_at) is True


def test_model_level_signal_below_profit_threshold_still_not_sent(monkeypatch):
    """Model-level signals are profit-checked by the cascade's
    MIN_SIGNAL_PROFIT_TON (below_min_profit) -- NOTIFY_MIN_PROFIT_USD
    applies to pair level only, model level is additionally gated by
    NOTIFY_MIN_DISCOUNT_PCT (see notifier.passes_notify_threshold)."""
    monkeypatch.setattr(config, "NOTIFY_LEVELS", DEFAULT_LEVELS)
    monkeypatch.setattr(config, "MIN_SIGNAL_PROFIT_NANO", int(Decimal("1000") * config.NANO))

    conn = db.connect(":memory:")
    _seed_model_signal(conn, "model-cheap-1")
    session = FakeSession([])
    by_ids = {"model-cheap-1": {"id": "model-cheap-1", "status": "listed", "price": "40.0"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 0
    assert session.calls == []
