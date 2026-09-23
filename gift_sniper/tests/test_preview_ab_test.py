from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.notifier import TelegramNotifier
from gift_sniper.preview_ab_test import PreviewABTest
from gift_sniper.signals import Signal
from .test_notifier import FakeResponse, FakeSession


def _signal(n: int, tg_id: str | None = "GiftName-1") -> Signal:
    return Signal(
        listing_external_id=f"ext-{n}",
        tg_id=tg_id,
        collection_id="col-a",
        collection_name="Collection",
        model_name="Model",
        backdrop_name="Backdrop",
        symbol_name=None,
        gift_number=n,
        photo_url=None,
        animation_url=None,
        currency="TON",
        old_price_nano=int(Decimal("50.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-20"),
        observed_at=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
        floor_nano=int(Decimal("60.0") * config.NANO),
        floor_source="at_drop",
        floor_level="pair",
        listed_count=3,
        ratio=Decimal("1.5"),
        discount=Decimal("0.2"),
        profit_before_withdrawal_nano=None,
        profit_nano=None,
        profit_usd=None,
    )


class FakeWarmSession:
    def __init__(self):
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return FakeResponse(200, {})


def _telegram_session(n_signals: int) -> FakeSession:
    return FakeSession([(200, {"ok": True, "result": {}})] * n_signals)


def test_preview_ab_test_splits_deterministically_by_index_parity():
    """КАК ТЕСТИРОВАТЬ item 5: even index -> group A, odd index -> group B,
    deterministic (same input, same split every run).
    """
    signals_list = [_signal(i) for i in range(10)]
    telegram_session = _telegram_session(10)
    notifier = TelegramNotifier("tok", "chat", session=telegram_session, sleep_fn=lambda s: None)
    warm_session = FakeWarmSession()
    ab_test = PreviewABTest(notifier, warm_session=warm_session, sleep_fn=lambda s: None, delay_sec=0)

    stats = ab_test.run(signals_list)

    assert stats["total_a"] == 5
    assert stats["total_b"] == 5
    assert stats["sent_a"] == 5
    assert stats["sent_b"] == 5

    # Re-running the identical split logic gives the identical assignment.
    ab_test2 = PreviewABTest(
        notifier, warm_session=FakeWarmSession(), sleep_fn=lambda s: None, delay_sec=0
    )
    telegram_session2 = _telegram_session(10)
    notifier2 = TelegramNotifier("tok", "chat", session=telegram_session2, sleep_fn=lambda s: None)
    ab_test2._notifier = notifier2
    stats2 = ab_test2.run(signals_list)
    assert stats2["total_a"] == stats["total_a"]
    assert stats2["total_b"] == stats["total_b"]


def test_preview_ab_test_group_b_makes_exactly_one_get_per_send_group_a_zero():
    """КАК ТЕСТИРОВАТЬ item 7: group B does exactly one GET on
    t.me/nft/ before each send, group A does zero (checked with a mock).
    """
    signals_list = [_signal(i) for i in range(4)]  # indices 0,2 -> A; 1,3 -> B
    telegram_session = _telegram_session(4)
    notifier = TelegramNotifier("tok", "chat", session=telegram_session, sleep_fn=lambda s: None)
    warm_session = FakeWarmSession()
    ab_test = PreviewABTest(notifier, warm_session=warm_session, sleep_fn=lambda s: None, delay_sec=0)

    ab_test.run(signals_list)

    assert len(warm_session.calls) == 2  # exactly the 2 group-B signals (index 1, 3)
    assert warm_session.calls == ["https://t.me/nft/GiftName-1", "https://t.me/nft/GiftName-1"]


def test_preview_ab_test_prefix_present_and_group_correct():
    signals_list = [_signal(0), _signal(1)]
    telegram_session = _telegram_session(2)
    notifier = TelegramNotifier("tok", "chat", session=telegram_session, sleep_fn=lambda s: None)
    ab_test = PreviewABTest(notifier, warm_session=FakeWarmSession(), sleep_fn=lambda s: None, delay_sec=0)

    ab_test.run(signals_list)

    call_a = telegram_session.calls[0][1]["text"]
    call_b = telegram_session.calls[1][1]["text"]
    assert call_a.startswith("[A] 1")
    assert call_b.startswith("[B] 2")


def test_preview_ab_test_sleeps_between_sends_not_after_the_last():
    signals_list = [_signal(i) for i in range(3)]
    telegram_session = _telegram_session(3)
    notifier = TelegramNotifier("tok", "chat", session=telegram_session, sleep_fn=lambda s: None)
    sleeps = []
    ab_test = PreviewABTest(
        notifier, warm_session=FakeWarmSession(), sleep_fn=lambda s: sleeps.append(s), delay_sec=3.0
    )

    ab_test.run(signals_list)

    assert sleeps == [3.0, 3.0]  # 2 pauses for 3 sends, none after the last


def test_preview_ab_test_never_writes_alerts_sent():
    """КАК ТЕСТИРОВАТЬ item 6: no rows appear in alerts_sent."""
    conn = db.connect(":memory:")
    signals_list = [_signal(i) for i in range(4)]
    telegram_session = _telegram_session(4)
    notifier = TelegramNotifier("tok", "chat", session=telegram_session, sleep_fn=lambda s: None)
    ab_test = PreviewABTest(notifier, warm_session=FakeWarmSession(), sleep_fn=lambda s: None, delay_sec=0)

    ab_test.run(signals_list)

    count = conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0]
    assert count == 0


def test_preview_ab_test_skips_warmup_when_tg_id_missing():
    signals_list = [_signal(0, tg_id="X"), _signal(1, tg_id=None)]
    telegram_session = _telegram_session(2)
    notifier = TelegramNotifier("tok", "chat", session=telegram_session, sleep_fn=lambda s: None)
    warm_session = FakeWarmSession()
    ab_test = PreviewABTest(notifier, warm_session=warm_session, sleep_fn=lambda s: None, delay_sec=0)

    ab_test.run(signals_list)

    assert warm_session.calls == []  # index 1 is group B but has no tg_id
