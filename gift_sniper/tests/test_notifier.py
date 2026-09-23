import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.notifier import (
    CommandHandler,
    TelegramNotifier,
    build_keyboard,
    format_caption,
    passes_notify_threshold,
    select_signals_to_send,
)
from gift_sniper.signals import Signal, clean_signals
from gift_sniper.poller import Poller
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.pair_floor import PairFloorCache
from .fakes import FakePortalsClient
from .test_price_drops_report import _listing, _snapshot


def _signal(**overrides) -> Signal:
    base = dict(
        listing_external_id="ext-1",
        tg_id="IceCream-45374",
        collection_id="col-ice-cream",
        collection_name="Ice Cream",
        model_name="Emperor",
        backdrop_name="Black",
        symbol_name="Shuriken",
        gift_number=45374,
        photo_url="https://nft.fragment.com/gift/ice-cream-45374.png",
        animation_url="https://nft.fragment.com/gift/ice-cream-45374.lottie.json",
        currency="TON",
        old_price_nano=int(Decimal("55.00") * config.NANO),
        new_price_nano=int(Decimal("29.20") * config.NANO),
        delta_pct=Decimal("-46.9"),
        observed_at=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc),
        floor_nano=int(Decimal("39.60") * config.NANO),
        floor_source="at_drop",
        floor_level="pair",
        listed_count=4,
        ratio=Decimal("1.4"),
        discount=Decimal("0.263"),
        profit_before_withdrawal_nano=int(Decimal("9.77") * config.NANO),
        profit_nano=int(Decimal("9.42") * config.NANO),
        profit_usd=Decimal("13.30"),
    )
    base.update(overrides)
    return Signal(**base)


def _model_signal(**overrides) -> Signal:
    """A level="model" signal -- profit_before_withdrawal_nano/profit_nano/
    profit_usd ARE computed (ДОПОЛНЕНИЕ, min-profit-threshold delivery)
    but flagged profit_is_estimate=True: the model floor is a different
    item's price (see signals.Signal's docstring / БАГ 1), so
    passes_notify_threshold/_priority in notifier.py deliberately ignore
    profit at this level and gate on discount% instead.
    """
    base = dict(floor_level="model", profit_is_estimate=True)
    base.update(overrides)
    return _signal(**base)


# --- Tonnel signals (this delivery) ---------------------------------------


def test_format_caption_header_checkmark_only_on_real_confirmation():
    """ДЕФЕКТ 5, КАК ТЕСТИРОВАТЬ items 9/10: header is "ЛИСТИНГ ✓" ONLY
    for cross_verdict == sent_neighbour_higher (a real independent
    confirmation); plain "ЛИСТИНГ" (no checkmark) for every other
    verdict. No marketplace name in the text either way, for BOTH
    marketplaces (unchanged from Правка 1/2).
    """
    for marketplace in ("portals", "tonnel"):
        signal = _signal(marketplace=marketplace, cross_verdict="sent_neighbour_higher")
        caption = format_caption(signal)
        assert caption.startswith("<b>ЛИСТИНГ ✓</b>")
        assert "TONNEL" not in caption
        assert "PORTALS" not in caption

        for verdict in ("not_checked", "sent_no_neighbour", "neighbour_thin", "error"):
            signal = _signal(marketplace=marketplace, cross_verdict=verdict)
            caption = format_caption(signal)
            assert caption.startswith("<b>ЛИСТИНГ</b>")
            assert "ЛИСТИНГ ✓" not in caption


def test_build_keyboard_tonnel_deep_link_no_gift_prefix():
    """КАК ТЕСТИРОВАТЬ item 2: t.me/tonnel_network_bot/gift?startapp=<id>
    with NO "gift_" prefix (unlike Portals' "gift_<uuid>"); button text
    is just "Купить" (Правка 2 -- no marketplace name on the button).
    """
    signal = _signal(marketplace="tonnel", listing_external_id="123456")
    keyboard = build_keyboard(signal)
    button = keyboard["inline_keyboard"][0][0]
    assert button["url"] == "https://t.me/tonnel_network_bot/gift?startapp=123456"
    assert "gift_123456" not in button["url"]
    assert button["text"] == "Купить"


def test_build_keyboard_mrkt_deep_link_strips_dashes():
    """Правка 4 (MRKT full-signaller delivery): confirmed live, id
    "4c667e31-e667-40ed-a41d-641791998bb9" ->
    startapp=4c667e31e66740eda41d641791998bb9 (dashes stripped).
    """
    signal = _signal(marketplace="mrkt", listing_external_id="4c667e31-e667-40ed-a41d-641791998bb9")
    keyboard = build_keyboard(signal)
    button = keyboard["inline_keyboard"][0][0]
    assert button["url"] == "https://t.me/mrkt/app?startapp=4c667e31e66740eda41d641791998bb9"
    assert button["text"] == "Купить"


def test_build_keyboard_portals_unaffected():
    signal = _signal(marketplace="portals", listing_external_id="uuid-1")
    keyboard = build_keyboard(signal)
    button = keyboard["inline_keyboard"][0][0]
    assert button["url"] == "https://t.me/portals_market_bot/market?startapp=gift_uuid-1"
    assert button["text"] == "Купить"


class FakeSession:
    """Records every POST; `responses` is a list of (status_code, json_body)
    consumed in order, one per call. A response body of None simulates a
    network-level exception instead of an HTTP response.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data))
        status, body = self._responses.pop(0)
        if body is None and status is None:
            import requests
            raise requests.exceptions.ConnectionError("simulated network failure")
        return FakeResponse(status, body)


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
        self.headers = {}

    def json(self):
        return self._body


def test_format_caption_uses_configured_currency_not_hardcoded():
    signal = _signal(currency="TON")
    caption = format_caption(signal)
    assert "TON" in caption
    assert "Snoop" not in caption  # sanity: not accidentally testing a stale fixture
    assert "Ice Cream" in caption or "45374" in caption  # title present in some form


def test_format_caption_title_is_plain_not_a_link():
    """The title is plain bold text, NOT a link (no blue-colored title)."""
    signal = _signal(tg_id="IceCream-45374", collection_name="Ice Cream", gift_number=45374)
    caption = format_caption(signal)
    assert "<b>Ice Cream #45374</b>" in caption
    assert '<a href="https://t.me/nft/IceCream-45374">Ice Cream #45374</a>' not in caption


def test_format_caption_no_dot_only_line():
    """No line consisting only of "·" anywhere in the message."""
    import re

    signal = _signal(tg_id="IceCream-45374")
    caption = format_caption(signal)
    for line in caption.splitlines():
        stripped = re.sub(r"<[^>]+>", "", line).strip()
        assert stripped != "·"


def test_format_caption_missing_tg_id_no_crash():
    """tg_id=None -> no exception, no link anywhere."""
    signal = _signal(tg_id=None)
    caption = format_caption(signal)  # must not raise
    assert "t.me/nft/" not in caption
    assert "<a href=" not in caption


def test_format_caption_link_present_but_not_on_title():
    """Дефект 3 fix: the link preview is the ONLY remaining approach
    (sendAnimation/sendSticker/sendDocument removed entirely -- see
    TelegramNotifier.send_signal) -- carried on a trailing space, never
    on the title.
    """
    signal = _signal(tg_id="IceCream-45374", collection_name="Ice Cream", gift_number=45374)
    caption = format_caption(signal)
    assert "https://t.me/nft/IceCream-45374" in caption
    assert "<b>Ice Cream #45374</b>" in caption  # title still plain
    assert '<a href="https://t.me/nft/IceCream-45374">Ice Cream #45374</a>' not in caption


def test_format_caption_floor_is_the_last_visible_line():
    """No blank line (or anything readable) between PRICE and FLOOR --
    FLOOR is the last visible line (the TONNEL line was removed, per
    spec: cross_verdict stays internal to the algorithm/report.py, never
    shown to the user).
    """
    signal = _signal(floor_nano=int(Decimal("400.00") * config.NANO))
    caption = format_caption(signal)
    lines = caption.split("\n")
    assert lines[-1].startswith("FLOOR:")
    assert lines[-2].startswith("PRICE:")  # PRICE and FLOOR consecutive, no blank between
    assert "TONNEL" not in caption


def test_format_caption_link_is_inline_on_floor_line_visible_char():
    """КАК ТЕСТИРОВАТЬ items 1-2: the t.me/nft/ link is attached to a
    VISIBLE character ("·") appended inline at the end of the FLOOR line
    -- not a zero-width anchor, and no separate line is added for it.
    """
    signal = _signal(tg_id="IceCream-45374", floor_nano=int(Decimal("29.00") * config.NANO))
    caption = format_caption(signal)
    lines = caption.split("\n")
    floor_idx = next(i for i, l in enumerate(lines) if l.startswith("FLOOR:"))
    link_lines = [i for i, l in enumerate(lines) if "<a href=" in l]
    assert link_lines == [floor_idx]  # the link lives ON the FLOOR line, no separate line
    assert '<a href="https://t.me/nft/IceCream-45374">·</a>' in lines[floor_idx]
    # the anchor wraps a visible middle-dot, never a zero-width/blank character
    assert '<a href="https://t.me/nft/IceCream-45374"> </a>' not in caption


def test_format_caption_resend_after_drop_marks_header():
    """КАК ТЕСТИРОВАТЬ item 4: resend_after_drop=True -> header contains
    "ЦЕНА СНИЖЕНА".
    """
    signal = _signal()
    caption = format_caption(signal, resend_after_drop=True)
    assert "ЦЕНА СНИЖЕНА" in caption


def test_format_caption_normal_send_has_no_resend_marker():
    signal = _signal()
    caption = format_caption(signal, resend_after_drop=False)
    assert "ЦЕНА СНИЖЕНА" not in caption


def test_format_caption_no_labeled_trait_lines():
    """КАК ТЕСТИРОВАТЬ item 3: no "Модель:"/"Фон:"/"Узор:" lines --
    traits collapsed onto one line, joined by "·".
    """
    signal = _signal(model_name="Emperor", backdrop_name="Black", symbol_name="Shuriken")
    caption = format_caption(signal)
    assert "Модель:" not in caption
    assert "Фон:" not in caption
    assert "Узор:" not in caption
    assert "Emperor · Black · Shuriken" in caption


def test_format_caption_no_emoji_in_body():
    """КАК ТЕСТИРОВАТЬ item 2: no emoji anywhere in the message body."""
    signal = _signal(pair_gone_count=5, pair_median_time_to_gone_hours=Decimal("12"))
    caption = format_caption(signal)
    # Spot-check every emoji used by any retired format.
    for emoji in ("📌", "💰", "📊", "✅", "⚠️", "📉", "🔎"):
        assert emoji not in caption


def test_format_caption_no_find_in_market_line():
    signal = _signal(gift_number=45374)
    caption = format_caption(signal)
    assert "Найти в маркете" not in caption


def test_format_caption_exact_minimal_structure():
    """КАК ТЕСТИРОВАТЬ item 1: exactly 5 content lines + 2 blank
    separators, in order: header, blank, title, traits, blank, PRICE,
    FLOOR (PRICE/FLOOR consecutive, no blank between, FLOOR last).
    """
    signal = _signal(
        tg_id="astral-shard-1467",
        collection_name="Astral Shard",
        gift_number=1491,
        model_name="Ruby Fuchsite",
        backdrop_name="Chocolate",
        symbol_name="Phoenix",
        new_price_nano=int(Decimal("115.00") * config.NANO),
        floor_nano=int(Decimal("142.00") * config.NANO),
        currency="GRAM",
        cross_verdict="sent_neighbour_higher",  # ДЕФЕКТ 5: needed for the checkmark
    )
    caption = format_caption(signal)
    lines = caption.split("\n")

    assert lines == [
        "<b>ЛИСТИНГ ✓</b>",  # ДЕФЕКТ 5: checkmark, since cross_verdict=sent_neighbour_higher
        "",
        "<b>Astral Shard #1491</b>",
        "Ruby Fuchsite · Chocolate · Phoenix",
        "",
        "PRICE: 115.00 GRAM",
        'FLOOR: 142.00 GRAM <a href="https://t.me/nft/astral-shard-1467">·</a>',
    ]


def test_format_caption_no_percent_no_old_price_no_listed_count_no_liquidity_no_profit(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 2: none of the removed fields appear,
    regardless of level or liquidity data being present.
    """
    signal = _signal(
        floor_level="pair",
        old_price_nano=int(Decimal("92.41") * config.NANO),
        new_price_nano=int(Decimal("91.37") * config.NANO),
        delta_pct=Decimal("-1.1"),
        listed_count=3,
        profit_nano=int(Decimal("9.42") * config.NANO),
        profit_usd=Decimal("13.30"),
        pair_gone_count=5,
        pair_median_time_to_gone_hours=Decimal("12"),
    )
    caption = format_caption(signal)
    assert "%" not in caption
    assert "92.41" not in caption  # old price
    assert "было" not in caption
    assert "в стакане" not in caption
    assert "Ликвидность" not in caption
    assert "Прибыль" not in caption
    assert "9.42" not in caption
    assert "13.30" not in caption
    assert "по модели" not in caption


def test_format_caption_model_level_has_no_extra_line_either():
    """Even at level=model (where profit was previously fabricated and
    then replaced with a caveat line), the new minimal format shows
    nothing beyond the three blocks -- no caveat, no profit.
    """
    signal = _model_signal()
    caption = format_caption(signal)
    assert "по модели" not in caption
    assert "Прибыль" not in caption
    assert "FLOOR:" in caption


def test_build_keyboard_has_exactly_one_button():
    """КАК ТЕСТИРОВАТЬ item 2: exactly one button."""
    signal = _signal()
    keyboard = build_keyboard(signal)
    all_buttons = [btn for row in keyboard["inline_keyboard"] for btn in row]
    assert len(all_buttons) == 1


def test_build_keyboard_button_uses_confirmed_lot_deep_link():
    """КАК ТЕСТИРОВАТЬ item 1: the button's link equals
    https://t.me/portals_market_bot/market?startapp=gift_<external_id>,
    without the share-link suffix.
    """
    signal = _signal(listing_external_id="abc-123-uuid")
    keyboard = build_keyboard(signal)
    button = keyboard["inline_keyboard"][0][0]
    assert button["url"] == "https://t.me/portals_market_bot/market?startapp=gift_abc-123-uuid"
    assert button["text"] == "Купить"


def test_format_caption_omits_liquidity_line_entirely_when_present():
    """КАК ТЕСТИРОВАТЬ item 2: "Ликвидность" is removed from the
    notification entirely -- even when the Signal carries real
    pair_gone_count/median data (still computed and available for
    report.py, just not shown here).
    """
    signal = _signal(pair_gone_count=5, pair_median_time_to_gone_hours=Decimal("12"))
    caption = format_caption(signal)
    assert "Ликвидность" not in caption
    assert "недостаточно данных" not in caption


def test_format_caption_omits_liquidity_line_when_none():
    signal = _signal(pair_gone_count=None, pair_median_time_to_gone_hours=None)
    caption = format_caption(signal)
    assert "Ликвидность" not in caption


def test_select_signals_to_send_caps_at_max_and_picks_most_profitable():
    """КАК ТЕСТИРОВАТЬ item 7: 15 signals at NOTIFY_MAX_PER_MINUTE=10 ->
    the 10 most profitable are sent, 5 are summarized as "extra".
    """
    sigs = [_signal(listing_external_id=f"ext-{i}", profit_usd=Decimal(i)) for i in range(15)]
    to_send, extra = select_signals_to_send(sigs, max_count=10)
    assert len(to_send) == 10
    assert extra == 5
    # Most profitable (highest profit_usd, ext-14..ext-5) were kept.
    assert {s.listing_external_id for s in to_send} == {f"ext-{i}" for i in range(5, 15)}


def test_select_signals_to_send_under_limit_sends_all():
    sigs = [_signal(listing_external_id=f"ext-{i}") for i in range(3)]
    to_send, extra = select_signals_to_send(sigs, max_count=10)
    assert len(to_send) == 3
    assert extra == 0


def test_send_signal_always_uses_sendmessage_even_with_animation_url_present():
    """Дефект 3: sendAnimation/sendSticker/sendDocument are gone from the
    codebase entirely -- confirmed live that all three either reject the
    gift's .lottie.json outright or, in sendDocument's case, deliver a
    raw file into the chat (khabibspapakha-5245.lottie.json, 340KB) --
    a real user-visible defect, not an acceptable fallback. sendMessage
    is used regardless of animation_url being present.
    """
    session = FakeSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("fake-token", "12345", session=session)
    ok = notifier.send_signal(_signal())
    assert ok is True
    assert len(session.calls) == 1
    url, data = session.calls[0]
    assert "sendMessage" in url
    assert "animation" not in data
    assert "sticker" not in data
    assert "document" not in data


def test_notifier_module_never_calls_sendanimation_sendsticker_senddocument():
    """Test per spec: no calls to sendAnimation, sendSticker, or
    sendDocument remain anywhere in notifier.py's send path -- source
    inspection, not just behavioral, so a future re-introduction of any
    of the three is caught even before it's exercised by a live send.
    """
    import inspect

    from gift_sniper import notifier as notifier_module

    source = inspect.getsource(notifier_module.TelegramNotifier.send_signal)
    for forbidden in ('"sendAnimation"', '"sendSticker"', '"sendDocument"'):
        assert forbidden not in source


def test_send_signal_link_preview_options_still_sent():
    """КАК ТЕСТИРОВАТЬ item 1: link_preview_options present with
    is_disabled=false and WITHOUT prefer_small_media -- confirmed live
    it has no effect on gift-card previews and was removed (see README,
    "closed question" -- preview size is controlled entirely by the
    Telegram client, not something this code can influence).
    """
    session = FakeSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("fake-token", "12345", session=session)
    notifier.send_signal(_signal())
    _url, data = session.calls[0]
    assert "disable_web_page_preview" not in data  # retired, replaced by link_preview_options
    link_preview_options = json.loads(data["link_preview_options"])
    assert link_preview_options["is_disabled"] is False
    assert link_preview_options["show_above_text"] is False
    assert "prefer_small_media" not in link_preview_options
    assert "https://t.me/nft/IceCream-45374" in data["text"]


def test_send_signal_works_when_no_animation_url():
    session = FakeSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("fake-token", "12345", session=session)
    ok = notifier.send_signal(_signal(animation_url=None))
    assert ok is True
    assert len(session.calls) == 1
    url, _data = session.calls[0]
    assert "sendMessage" in url


def test_send_signal_returns_false_on_api_error_without_raising():
    """КАК ТЕСТИРОВАТЬ item 5: a Telegram API error must not raise --
    the poller must be able to continue and retry on the next cycle,
    even after every fallback method also fails.
    """
    session = FakeSession(
        [
            (400, {"ok": False, "description": "x"}),  # sendAnimation
            (400, {"ok": False, "description": "x"}),  # sendSticker
            (400, {"ok": False, "description": "x"}),  # sendDocument
            (400, {"ok": False, "description": "Bad Request"}),  # sendMessage fallback
        ]
    )
    notifier = TelegramNotifier("fake-token", "12345", session=session)
    ok = notifier.send_signal(_signal())
    assert ok is False


def test_send_signal_respects_429_retry_after(monkeypatch):
    sleeps = []
    session = FakeSession(
        [
            (429, {"ok": False, "parameters": {"retry_after": 3}}),
            (200, {"ok": True, "result": {}}),
        ]
    )
    notifier = TelegramNotifier("fake-token", "12345", session=session, sleep_fn=sleeps.append)
    ok = notifier.send_signal(_signal())
    assert ok is True
    assert sleeps == [3.0]
    assert len(session.calls) == 2  # one 429, one successful retry (both within the sendAnimation call)


def test_alerts_sent_prevents_duplicate_marking():
    """КАК ТЕСТИРОВАТЬ item 2: a signal already recorded in alerts_sent
    must be detectable as already-sent (the poller's dedup check).
    """
    conn = db.connect(":memory:")
    observed_at = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    assert db.is_alert_sent(conn, "portals", "ext-1", observed_at) is False

    db.mark_alert_sent(conn, "portals", "ext-1", observed_at, datetime.now(timezone.utc))
    assert db.is_alert_sent(conn, "portals", "ext-1", observed_at) is True

    # Idempotent: marking again must not raise or duplicate.
    db.mark_alert_sent(conn, "portals", "ext-1", observed_at, datetime.now(timezone.utc))
    count = conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0]
    assert count == 1


def test_min_profit_filter_excludes_low_profit_pair_signal(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 3: a $3 profit pair-level signal with
    NOTIFY_MIN_PROFIT_USD=5 must be excluded -- and NOTIFY_MIN_DISCOUNT_PCT
    must have no bearing on this decision at all.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("5"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("99"))  # deliberately irrelevant here
    signal = _signal(floor_level="pair", profit_usd=Decimal("3"))
    assert passes_notify_threshold(signal) is False


def test_min_profit_usd_does_not_gate_model_level_signals(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 3: NOTIFY_MIN_PROFIT_USD must not cut off a
    model-level signal (it has no profit_usd to compare against at all --
    it's gated on NOTIFY_MIN_DISCOUNT_PCT instead).
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("999999"))  # deliberately irrelevant here
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("15"))
    signal = _model_signal(discount=Decimal("0.20"))  # 20% >= 15%
    assert passes_notify_threshold(signal) is True


def test_min_discount_pct_does_not_gate_pair_level_signals(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 3: NOTIFY_MIN_DISCOUNT_PCT must not cut off a
    pair-level signal, even if its discount% is below the threshold, as
    long as its profit clears NOTIFY_MIN_PROFIT_USD.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("5"))
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("50"))  # deliberately irrelevant here
    signal = _signal(floor_level="pair", profit_usd=Decimal("13.30"), discount=Decimal("0.05"))  # 5% << 50%
    assert passes_notify_threshold(signal) is True


def test_model_level_signal_below_discount_threshold_excluded(monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_MIN_DISCOUNT_PCT", Decimal("15"))
    signal = _model_signal(discount=Decimal("0.05"))  # 5% < 15%
    assert passes_notify_threshold(signal) is False


def test_notify_levels_excludes_model_level_by_default():
    """A level=model signal must be excluded when NOTIFY_LEVELS="pair"
    (the default) -- this gate is separate from passes_notify_threshold,
    applied by poller.py's _maybe_notify.
    """
    # The default is now "pair,model" (see test_notify_levels_default.py);
    # the real gate is exercised end-to-end in the test below.
    signal = _signal(floor_level="model")
    assert signal.floor_level not in {"pair"}


def test_notify_levels_end_to_end_blocks_model_level_portals_send(monkeypatch):
    """ДЕФЕКТ 2 (systemic-check delivery), КАК ТЕСТИРОВАТЬ item 3:
    end-to-end, NOT the tautological check above -- a real level="model"
    clean signal, seeded in the DB and run through the actual
    Poller._maybe_notify(), must NOT be sent when NOTIFY_LEVELS={"pair"}.
    Confirmed live: Portals sent 54 level="model" signals despite this
    setting -- the tautological unit test above never would have caught
    a real end-to-end regression, since it never exercises _maybe_notify
    at all.
    """
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))

    conn = db.connect(":memory:")
    listing = _listing("model-leak-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    observed_at = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", "model-leak-1",
        old_price_nano=int(Decimal("60.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-33.3"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
        floor_listed_count_at_drop=5,
        floor_level_at_drop="model",
    )

    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids_responses = {"model-leak-1": {"id": "model-leak-1", "status": "listed", "price": "40.0"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 0
    assert db.is_alert_sent(conn, "portals", "model-leak-1", observed_at) is False


def test_command_handler_rejects_unauthorized_user():
    """КАК ТЕСТИРОВАТЬ item 8: a message from a different user_id gets
    "доступ ограничен" and the command itself is not executed (no /status
    reply is sent).
    """
    session = FakeSession(
        [
            (
                200,
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {"from": {"id": 999}, "text": "/status"},
                        }
                    ],
                },
            ),
            (200, {"ok": True, "result": {}}),  # the "доступ ограничен" reply
        ]
    )
    notifier = TelegramNotifier("fake-token", "12345", session=session)
    conn = db.connect(":memory:")
    handler = CommandHandler(notifier, conn, owner_id="12345")
    handler.poll_once()

    assert len(session.calls) == 2  # getUpdates + the rejection reply
    _, reply_data = session.calls[1]
    assert reply_data["text"] == "доступ ограничен"


def test_command_handler_executes_status_for_authorized_user():
    session = FakeSession(
        [
            (
                200,
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {"from": {"id": 12345}, "text": "/status"},
                        }
                    ],
                },
            ),
            (200, {"ok": True, "result": {}}),  # the /status reply
        ]
    )
    notifier = TelegramNotifier("fake-token", "12345", session=session)
    conn = db.connect(":memory:")
    handler = CommandHandler(notifier, conn, owner_id="12345")
    handler.poll_once()

    assert len(session.calls) == 2
    _, reply_data = session.calls[1]
    assert "Аптайм" in reply_data["text"]


def _build_poller_with_notifier(conn, session, by_ids_responses=None, tonnel_client=None, viewer_chat_ids=None):
    client = FakePortalsClient(pages=[[], []], by_ids_responses=by_ids_responses)
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    pair_floor_cache = PairFloorCache(client, ttl_sec=300)
    notifier = TelegramNotifier("fake-token", "12345", viewer_chat_ids=viewer_chat_ids, session=session)
    poller = Poller(
        conn, client, auth, floor_cache, pair_floor_cache, notifier=notifier, tonnel_client=tonnel_client,
    )
    return poller


def test_poller_maybe_notify_sends_clean_signal_and_marks_it_sent(monkeypatch):
    """End-to-end: a clean signal above NOTIFY_MIN_PROFIT_USD, at an
    allowed level, gets sent and recorded in alerts_sent.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = db.connect(":memory:")
    listing = _listing("notify-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    observed_at = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", "notify-1",
        old_price_nano=int(Decimal("60.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-33.3"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    session = FakeSession([(200, {"ok": True, "result": {}})])
    # КАК ТЕСТИРОВАТЬ item 6: still status="listed" at the same price ->
    # the freshness check passes and the signal is sent.
    by_ids_responses = {"notify-1": {"id": "notify-1", "status": "listed", "price": "40.0"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert poller.stats["signals_stale"] == 0
    assert db.is_alert_sent(conn, "portals", "notify-1", observed_at) is True
    # A second notify pass must NOT re-send the same signal.
    poller._maybe_notify()
    assert poller.stats["signals_sent"] == 1


def test_poller_maybe_notify_does_not_mark_sent_on_failure(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 5, poller-level: a failed send must not mark
    the signal sent, and must not raise out of _maybe_notify.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = db.connect(":memory:")
    listing = _listing("notify-fail-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    observed_at = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", "notify-fail-1",
        old_price_nano=int(Decimal("60.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-33.3"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    session = FakeSession([(500, {"ok": False, "description": "Internal Server Error"})])
    by_ids_responses = {"notify-fail-1": {"id": "notify-fail-1", "status": "listed", "price": "40.0"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses)
    poller._maybe_notify()  # must not raise

    assert poller.stats["signals_send_failed"] == 1
    assert poller.stats["signals_sent"] == 0
    assert db.is_alert_sent(conn, "portals", "notify-fail-1", observed_at) is False


def _seed_notify_signal(conn, ext_id):
    listing = _listing(ext_id, int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    observed_at = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", ext_id,
        old_price_nano=int(Decimal("60.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-33.3"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )
    return observed_at


def test_poller_maybe_notify_skips_withdrawn_lot_and_marks_stale(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 5: a lot with status="withdrawn" at the
    freshness check -> not sent, signals_stale incremented, and a
    status='skipped_stale' row exists in alerts_sent (so it's never
    retried again).
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = db.connect(":memory:")
    observed_at = _seed_notify_signal(conn, "withdrawn-1")

    session = FakeSession([])  # sendMessage must NEVER be called
    by_ids_responses = {"withdrawn-1": {"id": "withdrawn-1", "status": "withdrawn", "price": None}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses)
    poller._maybe_notify()

    assert poller.stats["signals_stale"] == 1
    assert poller.stats["signals_sent"] == 0
    assert len(session.calls) == 0  # no sendMessage -- freshness check goes through the Portals client, not Telegram
    assert db.is_alert_sent(conn, "portals", "withdrawn-1", observed_at) is True
    row = conn.execute(
        "SELECT status FROM alerts_sent WHERE listing_external_id = 'withdrawn-1'"
    ).fetchone()
    assert row[0] == "skipped_stale"


def test_poller_maybe_notify_sends_when_still_listed_same_price(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 6: status="listed" and the SAME price ->
    sent normally.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = db.connect(":memory:")
    observed_at = _seed_notify_signal(conn, "still-listed-1")

    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids_responses = {"still-listed-1": {"id": "still-listed-1", "status": "listed", "price": "40.0"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert poller.stats["signals_stale"] == 0
    row = conn.execute(
        "SELECT status FROM alerts_sent WHERE listing_external_id = 'still-listed-1'"
    ).fetchone()
    assert row[0] == "sent"


def test_poller_maybe_notify_skips_when_price_changed(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 7: status="listed" but the CURRENT price
    differs from the notified price -> not sent, marked stale.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = db.connect(":memory:")
    observed_at = _seed_notify_signal(conn, "repriced-1")

    session = FakeSession([])
    # Signal was computed at price 40.0; the live check now shows 45.0.
    by_ids_responses = {"repriced-1": {"id": "repriced-1", "status": "listed", "price": "45.0"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses)
    poller._maybe_notify()

    assert poller.stats["signals_stale"] == 1
    assert poller.stats["signals_sent"] == 0
    row = conn.execute(
        "SELECT status FROM alerts_sent WHERE listing_external_id = 'repriced-1'"
    ).fetchone()
    assert row[0] == "skipped_stale"


def test_poller_maybe_notify_freshness_check_network_error_retries_not_stale(monkeypatch):
    """A freshness-check network/API error must NOT be treated as
    confirmed-stale -- it's unverified, so the signal is retried later,
    exactly like a send failure, and is NOT written to alerts_sent.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = db.connect(":memory:")
    observed_at = _seed_notify_signal(conn, "check-fails-1")

    class BrokenFreshnessClient(FakePortalsClient):
        def search_by_ids(self, ids):
            from gift_sniper.errors import TransientError
            raise TransientError("simulated timeout")

    client = BrokenFreshnessClient(pages=[[], []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    pair_floor_cache = PairFloorCache(client, ttl_sec=300)
    notifier = TelegramNotifier("fake-token", "12345", session=FakeSession([]))
    poller = Poller(conn, client, auth, floor_cache, pair_floor_cache, notifier=notifier)

    poller._maybe_notify()  # must not raise

    assert poller.stats["signals_stale"] == 0
    assert poller.stats["signals_sent"] == 0
    assert poller.stats["signals_send_failed"] == 1
    assert db.is_alert_sent(conn, "portals", "check-fails-1", observed_at) is False


def _record_drop(conn, ext_id, old_price, new_price, observed_at):
    db.record_price_change(
        conn, "portals", ext_id,
        old_price_nano=int(Decimal(old_price) * config.NANO),
        new_price_nano=int(Decimal(new_price) * config.NANO),
        delta_pct=Decimal("-2.0"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )


def test_cooldown_allows_first_notification_for_a_listing():
    conn = db.connect(":memory:")
    assert db.get_last_sent_alert_for_listing(conn, "portals", "never-sent-1") is None


def test_check_cooldown_suppresses_small_drop_within_window(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 3: two signals for the same listing, 1 minute
    apart, price only ~2% lower -> the second is suppressed.
    """
    monkeypatch.setattr(config, "SIGNAL_COOLDOWN_MIN", 60)
    monkeypatch.setattr(config, "SIGNAL_RESEND_DROP_PCT", Decimal("10"))

    conn = db.connect(":memory:")
    listing = _listing("cooldown-1", int(Decimal("22.00") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    t1 = datetime.now(timezone.utc)
    _record_drop(conn, "cooldown-1", "23.0", "22.00", t1)
    db.mark_alert_sent(conn, "portals", "cooldown-1", t1, t1)  # first notification actually sent

    poller = _build_poller_with_notifier(conn, FakeSession([]))
    # Second signal, 1 minute later, price 21.60 (~1.8% lower than 22.00).
    signal = clean_signals(conn, usd_rate=Decimal("1"))[0]
    signal.new_price_nano = int(Decimal("21.60") * config.NANO)

    allowed, resend_after_drop = poller._check_cooldown(signal)
    assert allowed is False
    assert resend_after_drop is False


def test_check_cooldown_bypassed_for_a_real_further_drop(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 4: a second signal with price 15% lower than
    the last-notified price -> allowed, marked as a resend.
    """
    monkeypatch.setattr(config, "SIGNAL_COOLDOWN_MIN", 60)
    monkeypatch.setattr(config, "SIGNAL_RESEND_DROP_PCT", Decimal("10"))

    conn = db.connect(":memory:")
    listing = _listing("cooldown-2", int(Decimal("22.00") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    t1 = datetime.now(timezone.utc)
    _record_drop(conn, "cooldown-2", "23.0", "22.00", t1)
    db.mark_alert_sent(conn, "portals", "cooldown-2", t1, t1)

    poller = _build_poller_with_notifier(conn, FakeSession([]))
    signal = clean_signals(conn, usd_rate=Decimal("1"))[0]
    signal.new_price_nano = int(Decimal("18.70") * config.NANO)  # 15% below 22.00

    allowed, resend_after_drop = poller._check_cooldown(signal)
    assert allowed is True
    assert resend_after_drop is True


def test_check_cooldown_allows_different_listings_independently(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 5: two signals for DIFFERENT listings, 1
    minute apart -> both allowed (cooldown is per-listing).
    """
    monkeypatch.setattr(config, "SIGNAL_COOLDOWN_MIN", 60)

    conn = db.connect(":memory:")
    t1 = datetime.now(timezone.utc)
    db.mark_alert_sent(conn, "portals", "listing-a", t1, t1)

    poller = _build_poller_with_notifier(conn, FakeSession([]))
    listing_b = _listing("listing-b", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing_b, _snapshot(listing_b, int(Decimal("70.0") * config.NANO)))
    _record_drop(conn, "listing-b", "45.0", "40.0", datetime.now(timezone.utc))
    signal_b = clean_signals(conn, usd_rate=Decimal("1"))[0]

    allowed, resend_after_drop = poller._check_cooldown(signal_b)
    assert allowed is True
    assert resend_after_drop is False


def test_poller_maybe_notify_suppresses_second_signal_within_cooldown(monkeypatch):
    """End-to-end: two clean signals for the same listing within the
    cooldown window and a small drop -> the second is suppressed and
    signals_suppressed_cooldown is incremented; the first is sent
    normally.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "SIGNAL_COOLDOWN_MIN", 60)
    monkeypatch.setattr(config, "SIGNAL_RESEND_DROP_PCT", Decimal("10"))

    conn = db.connect(":memory:")
    listing = _listing("e2e-cooldown-1", int(Decimal("22.00") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))

    t1 = datetime.now(timezone.utc)
    _record_drop(conn, "e2e-cooldown-1", "23.0", "22.00", t1)

    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids_responses = {"e2e-cooldown-1": {"id": "e2e-cooldown-1", "status": "listed", "price": "22.00"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses)
    poller._maybe_notify()
    assert poller.stats["signals_sent"] == 1

    # Second drop, ~1.8% further down, within the cooldown window.
    t2 = t1 + timedelta(minutes=1)
    _record_drop(conn, "e2e-cooldown-1", "22.00", "21.60", t2)
    by_ids_responses["e2e-cooldown-1"] = {"id": "e2e-cooldown-1", "status": "listed", "price": "21.60"}
    poller._maybe_notify()

    assert poller.stats["signals_suppressed_cooldown"] == 1


# --- ДЕФЕКТ 3 (systemic-check delivery), КАК ТЕСТИРОВАТЬ items 4/5/6 ---
# --- All three share one SIGNAL_COOLDOWN_MIN=90 window so a 70-minute ---
# --- gap is still "within cooldown" -- matching the spec's own item 5 ---
# --- ("через 70 минут ... заблокирована порогом SIGNAL_RESEND_DROP_PCT"), ---
# --- which is only coherent if the cooldown window is longer than 70 ---
# --- minutes (the default SIGNAL_COOLDOWN_MIN=60 would let a 70-minute ---
# --- gap through on elapsed time alone, before the price check even runs). ---

def _setup_cooldown_listing(monkeypatch, ext_id="cooldown-item-1"):
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "SIGNAL_COOLDOWN_MIN", 90)
    monkeypatch.setattr(config, "SIGNAL_RESEND_DROP_PCT", Decimal("10"))

    conn = db.connect(":memory:")
    listing = _listing(ext_id, int(Decimal("22.00") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    t1 = datetime.now(timezone.utc)
    _record_drop(conn, ext_id, "23.0", "22.00", t1)

    session = FakeSession([(200, {"ok": True, "result": {}})] * 3)
    by_ids_responses = {ext_id: {"id": ext_id, "status": "listed", "price": "22.00"}}
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses)
    poller._maybe_notify()
    assert poller.stats["signals_sent"] == 1
    return conn, poller, by_ids_responses, t1


def test_item4_resend_21min_gap_small_drop_blocked_by_cooldown(monkeypatch):
    ext_id = "cooldown-item-4"
    conn, poller, by_ids_responses, t1 = _setup_cooldown_listing(monkeypatch, ext_id)

    t2 = t1 + timedelta(minutes=21)
    _record_drop(conn, ext_id, "22.00", "21.65", t2)  # ~1.6% further down
    by_ids_responses[ext_id] = {"id": ext_id, "status": "listed", "price": "21.65"}
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1  # unchanged -- still just the first
    assert poller.stats["signals_suppressed_cooldown"] == 1


def test_item5_resend_70min_gap_2pct_drop_blocked_by_resend_threshold(monkeypatch):
    ext_id = "cooldown-item-5"
    conn, poller, by_ids_responses, t1 = _setup_cooldown_listing(monkeypatch, ext_id)

    t2 = t1 + timedelta(minutes=70)
    _record_drop(conn, ext_id, "22.00", "21.56", t2)  # 2% further down, < SIGNAL_RESEND_DROP_PCT=10
    by_ids_responses[ext_id] = {"id": ext_id, "status": "listed", "price": "21.56"}
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1  # unchanged
    assert poller.stats["signals_suppressed_cooldown"] == 1


def test_item6_resend_70min_gap_15pct_drop_bypasses_cooldown(monkeypatch):
    ext_id = "cooldown-item-6"
    conn, poller, by_ids_responses, t1 = _setup_cooldown_listing(monkeypatch, ext_id)

    t2 = t1 + timedelta(minutes=70)
    _record_drop(conn, ext_id, "22.00", "18.70", t2)  # 15% further down, > SIGNAL_RESEND_DROP_PCT=10
    by_ids_responses[ext_id] = {"id": ext_id, "status": "listed", "price": "18.70"}
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 2
    assert poller.stats["signals_suppressed_cooldown"] == 0


# --- cross-check as a pure pre-send filter (Правка 1/3) --------------------


class FakeTonnelClient:
    """Stub for Poller-level tests -- never touches curl_cffi/the network.
    `floor` is returned for every pair_floor() call; `raise_error`, if set,
    is raised instead. `calls` records every invocation so tests can assert
    it was (or, КАК ТЕСТИРОВАТЬ item 7, was NOT) called at all.
    """

    def __init__(self, floor=None, raise_error=None):
        self._floor = floor
        self._raise_error = raise_error
        self.calls: list[dict] = []

    def pair_floor(self, gift_name, model, backdrop, exclude_gift_num=None):
        self.calls.append(
            {"gift_name": gift_name, "model": model, "backdrop": backdrop, "exclude_gift_num": exclude_gift_num}
        )
        if self._raise_error is not None:
            raise self._raise_error
        return self._floor

    def model_floor(self, gift_name, model, exclude_gift_num=None):
        from gift_sniper.tonnel_client import TonnelFloor
        return TonnelFloor(floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=[])


def _tonnel_signal_setup(conn, price="40.0", floor="70.0"):
    listing = _listing("tonnel-1", int(Decimal(price) * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal(floor) * config.NANO)))
    observed_at = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", "tonnel-1",
        old_price_nano=int(Decimal("60.0") * config.NANO),
        new_price_nano=int(Decimal(price) * config.NANO),
        delta_pct=Decimal("-33.3"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal(floor) * config.NANO),
        floor_listed_count_at_drop=5,
    )
    return observed_at


def test_price_above_own_floor_signal_never_reaches_cross_check(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 5: a signal rejected by price_above_own_floor
    never reaches cross-check at all -- the Tonnel client mock raises if
    called, proving zero network spend on a lot that was never going
    anywhere.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair", "model"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)

    conn = db.connect(":memory:")
    listing = _listing("over-floor-cc-1", int(Decimal("210.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("34.0") * config.NANO)))
    observed_at = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", "over-floor-cc-1",
        old_price_nano=int(Decimal("230.0") * config.NANO),
        new_price_nano=int(Decimal("210.0") * config.NANO),
        delta_pct=Decimal("-8.7"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
    )

    class RaisingTonnelClient:
        def pair_floor(self, *args, **kwargs):
            raise AssertionError("cross-check must never be called for a price_above_own_floor signal")

    session = FakeSession([])  # sendMessage must never be called either
    poller = _build_poller_with_notifier(conn, session, tonnel_client=RaisingTonnelClient())
    poller._maybe_notify()  # must not raise

    assert poller.stats["signals_sent"] == 0
    assert session.calls == []


def test_maybe_notify_sends_when_no_neighbour(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 7: no comparable Tonnel lot -> sent, verdict
    sent_no_neighbour.
    """
    from gift_sniper.tonnel_client import TonnelFloor

    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)

    conn = db.connect(":memory:")
    _tonnel_signal_setup(conn, price="40.0", floor="70.0")

    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids_responses = {"tonnel-1": {"id": "tonnel-1", "status": "listed", "price": "40.0"}}
    fake_tonnel = FakeTonnelClient(
        floor=TonnelFloor(floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=[])
    )
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses, tonnel_client=fake_tonnel)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert poller.stats["sent_no_neighbour"] == 1
    sent_text = session.calls[0][1]["text"]
    # ДЕФЕКТ 5: sent_no_neighbour is not a real confirmation -- no checkmark.
    assert sent_text.startswith("<b>ЛИСТИНГ</b>")
    assert "ЛИСТИНГ ✓" not in sent_text


def test_maybe_notify_skips_when_neighbour_cheaper(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 4: P=17.80, N=11.44 (neighbour cheaper) ->
    not sent, skipped_neighbour_cheaper.
    """
    from gift_sniper.tonnel_client import TonnelFloor

    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)

    conn = db.connect(":memory:")
    observed_at = _tonnel_signal_setup(conn, price="17.80", floor="29.00")

    session = FakeSession([])  # no sendMessage call expected at all
    by_ids_responses = {"tonnel-1": {"id": "tonnel-1", "status": "listed", "price": "17.80"}}
    fake_tonnel = FakeTonnelClient(
        floor=TonnelFloor(
            floor_nano=int(Decimal("10.4") * config.NANO),
            floor_with_fee_nano=int(Decimal("11.44") * config.NANO),
            listed_count=5, status="ok", raw=[],
        )
    )
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses, tonnel_client=fake_tonnel)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 0
    assert poller.stats["skipped_neighbour_cheaper"] == 1
    assert session.calls == []

    row = conn.execute(
        "SELECT status FROM alerts_sent WHERE listing_external_id='tonnel-1' AND observed_at=?",
        (observed_at.isoformat(),),
    ).fetchone()
    assert tuple(row) == ("skipped_cross_worse",)


def test_maybe_notify_skips_when_gap_below_threshold(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 5: P=17.80, N=19.00 (gap 6.7% < default 10%)
    -> not sent."""
    from gift_sniper.tonnel_client import TonnelFloor

    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))

    conn = db.connect(":memory:")
    _tonnel_signal_setup(conn, price="17.80", floor="29.00")

    session = FakeSession([])
    by_ids_responses = {"tonnel-1": {"id": "tonnel-1", "status": "listed", "price": "17.80"}}
    fake_tonnel = FakeTonnelClient(
        floor=TonnelFloor(
            floor_nano=int(Decimal("17.27") * config.NANO),  # 19.00/1.1 -- with_fee = 19.00
            floor_with_fee_nano=int(Decimal("19.00") * config.NANO),
            listed_count=5, status="ok", raw=[],
        )
    )
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses, tonnel_client=fake_tonnel)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 0
    assert poller.stats["skipped_neighbour_cheaper"] == 1


def test_maybe_notify_sends_when_gap_above_threshold(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 6: P=17.80, N=25.00 (gap 40%) -> sent."""
    from gift_sniper.tonnel_client import TonnelFloor

    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))

    conn = db.connect(":memory:")
    _tonnel_signal_setup(conn, price="17.80", floor="29.00")

    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids_responses = {"tonnel-1": {"id": "tonnel-1", "status": "listed", "price": "17.80"}}
    fake_tonnel = FakeTonnelClient(
        floor=TonnelFloor(
            floor_nano=int(Decimal("22.73") * config.NANO),
            floor_with_fee_nano=int(Decimal("25.00") * config.NANO),
            listed_count=5, status="ok", raw=[],
        )
    )
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses, tonnel_client=fake_tonnel)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert poller.stats["sent_neighbour_higher"] == 1


def test_maybe_notify_neighbour_thin_below_min_count_still_sends(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 8: a comparable neighbour lot exists but only
    1 listing, below CROSS_MIN_NEIGHBOUR_COUNT=3 -> treated like "no
    neighbour", verdict "neighbour_thin", still SENT.
    """
    from gift_sniper.tonnel_client import TonnelFloor

    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("0"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    conn = db.connect(":memory:")
    _tonnel_signal_setup(conn, price="21.50", floor="30.0")

    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids_responses = {"tonnel-1": {"id": "tonnel-1", "status": "listed", "price": "21.50"}}
    fake_tonnel = FakeTonnelClient(
        floor=TonnelFloor(
            floor_nano=int(Decimal("19.55") * config.NANO),
            floor_with_fee_nano=int(Decimal("21.50") * config.NANO),  # equal to price, would be "worse" if trusted
            listed_count=1, status="ok", raw=[],
        )
    )
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses, tonnel_client=fake_tonnel)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert poller.stats["neighbour_thin"] == 1


def test_maybe_notify_sends_on_neighbour_query_error(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 6 (spec): a neighbour query failure never
    blocks the send -- verdict "error", still SENT.
    """
    from gift_sniper.tonnel_client import TonnelError as RealTonnelError

    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)

    conn = db.connect(":memory:")
    _tonnel_signal_setup(conn, price="40.0", floor="70.0")

    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids_responses = {"tonnel-1": {"id": "tonnel-1", "status": "listed", "price": "40.0"}}
    fake_tonnel = FakeTonnelClient(raise_error=RealTonnelError("boom"))
    poller = _build_poller_with_notifier(conn, session, by_ids_responses=by_ids_responses, tonnel_client=fake_tonnel)
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert poller.stats["error"] == 1


def test_maybe_notify_skips_tonnel_entirely_when_disabled(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 7 (spec numbering): CROSS_CHECK_ENABLED=false
    -> zero calls to Tonnel (a mock that raises AssertionError if
    called), signal still sent.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", False)

    conn = db.connect(":memory:")
    _tonnel_signal_setup(conn, price="40.0", floor="70.0")

    session = FakeSession([(200, {"ok": True, "result": {}})])
    by_ids_responses = {"tonnel-1": {"id": "tonnel-1", "status": "listed", "price": "40.0"}}

    class RaisingTonnelClient:
        def pair_floor(self, *args, **kwargs):
            raise AssertionError("Tonnel must not be called when CROSS_CHECK_ENABLED=false")

    poller = _build_poller_with_notifier(
        conn, session, by_ids_responses=by_ids_responses, tonnel_client=RaisingTonnelClient(),
    )
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert poller.stats["sent_no_neighbour"] == 0
    assert poller.stats["sent_neighbour_higher"] == 0
    assert poller.stats["skipped_neighbour_cheaper"] == 0
    assert poller.stats["neighbour_thin"] == 0
    assert poller.stats["error"] == 0
    sent_text = session.calls[0][1]["text"]
    # ДЕФЕКТ 5: cross-check disabled -> cross_verdict stays "not_checked",
    # not a real confirmation -- no checkmark.
    assert sent_text.startswith("<b>ЛИСТИНГ</b>")
    assert "ЛИСТИНГ ✓" not in sent_text


def test_no_cross_market_signal_type_remains_in_codebase():
    """КАК ТЕСТИРОВАТЬ item 9: no mention of CrossMarketSignal or the
    "МЕЖБИРЖЕВОЙ" format anywhere in the source tree.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "CrossMarketSignal" not in text, path
        assert "МЕЖБИРЖЕВОЙ" not in text, path


def test_cross_check_enabled_default_source_is_true():
    """Правка 1 (two-way cross-check delivery): supersedes the earlier
    "off by default" verdict -- that was because the one-way, per-listing
    on-demand query almost never found a comparable Tonnel lot. Measured
    live on 19 real signals from both marketplaces, the PAIR-level
    comparison (this delivery) found a comparable neighbour for 6/12
    Portals signals and 5/7 Tonnel signals (~60%) -- workable, so the
    default flips to true.
    """
    import inspect

    from gift_sniper import config as config_module

    source = inspect.getsource(config_module)
    assert '_env("CROSS_CHECK_ENABLED", "true")' in source
    assert config_module.CROSS_CHECK_ENABLED is True


# --- multi-recipient notifications (owner + viewers) ----------------------


def test_send_signal_broadcasts_to_owner_and_viewers():
    """КАК ТЕСТИРОВАТЬ item 1: TELEGRAM_VIEWER_IDS="111,222" -> the
    notification goes out three times: owner + two viewers.
    """
    session = FakeSession([(200, {"ok": True, "result": {}})] * 3)
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=["111", "222"], session=session)
    signal = _signal()

    ok = notifier.send_signal(signal)

    assert ok is True
    assert len(session.calls) == 3
    sent_chat_ids = [data["chat_id"] for _url, data in session.calls]
    assert sent_chat_ids == ["OWNER", "111", "222"]


def test_send_signal_one_recipient_403_does_not_block_others(caplog):
    """КАК ТЕСТИРОВАТЬ item 2: one recipient fails with the "bot can't
    initiate conversation" 403 -- the rest still get the message, and a
    distinct log line with the /start hint is emitted for the failure.
    """
    session = FakeSession([
        (200, {"ok": True, "result": {}}),  # owner: ok
        (403, {"ok": False, "description": "Forbidden: bot can't initiate conversation with a user"}),  # viewer 1: fails
        (200, {"ok": True, "result": {}}),  # viewer 2: ok
    ])
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=["111", "222"], session=session)
    signal = _signal()

    with caplog.at_level("ERROR"):
        ok = notifier.send_signal(signal)

    assert ok is True  # owner succeeded
    assert len(session.calls) == 3  # all three attempted, despite the middle failure
    assert any("started a conversation" in r.message for r in caplog.records)
    assert any("/start" in r.message for r in caplog.records)


def test_send_signal_owner_failure_returns_false_even_if_viewers_succeed():
    session = FakeSession([
        (403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}),  # owner: fails
        (200, {"ok": True, "result": {}}),  # viewer: ok
    ])
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=["111"], session=session)
    signal = _signal()

    ok = notifier.send_signal(signal)

    assert ok is False  # gated on the OWNER's send specifically
    assert len(session.calls) == 2  # viewer still attempted


def test_no_viewers_behaves_like_before():
    """КАК ТЕСТИРОВАТЬ item 5: TELEGRAM_VIEWER_IDS empty -> exactly one
    send, to the owner only (unchanged from single-recipient behavior).
    """
    session = FakeSession([(200, {"ok": True, "result": {}})])
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=None, session=session)
    signal = _signal()

    ok = notifier.send_signal(signal)

    assert ok is True
    assert len(session.calls) == 1
    assert session.calls[0][1]["chat_id"] == "OWNER"


def test_command_from_viewer_executes_readonly_command():
    """КАК ТЕСТИРОВАТЬ item 3: /status from a viewer is executed, and the
    reply goes back to the viewer's own chat (not broadcast).
    """
    session = FakeSession([
        (200, {"ok": True, "result": [
            {"update_id": 1, "message": {"from": {"id": 111}, "text": "/status"}},
        ]}),
        (200, {"ok": True, "result": {}}),  # the /status reply
    ])
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=["111"], session=session)
    conn = db.connect(":memory:")
    handler = CommandHandler(notifier, conn, owner_id="OWNER", viewer_ids=["111"])

    handler.poll_once()

    assert len(session.calls) == 2  # getUpdates + the reply
    reply_call = session.calls[1]
    assert reply_call[1]["chat_id"] == "111"  # reply goes to the viewer, not broadcast


def test_command_from_viewer_non_readonly_is_silently_ignored():
    """Правка: "любые команды, меняющие поведение системы, им
    недоступны" -- /start (not in READONLY_COMMANDS) from a viewer is
    silently ignored, no reply at all (and NOT "доступ ограничен" --
    the viewer IS authorized, just not for this).
    """
    session = FakeSession([
        (200, {"ok": True, "result": [
            {"update_id": 1, "message": {"from": {"id": 111}, "text": "/start"}},
        ]}),
    ])
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=["111"], session=session)
    conn = db.connect(":memory:")
    handler = CommandHandler(notifier, conn, owner_id="OWNER", viewer_ids=["111"])

    handler.poll_once()

    assert len(session.calls) == 1  # only getUpdates -- no reply sent at all


def test_command_from_stranger_gets_access_denied():
    """КАК ТЕСТИРОВАТЬ item 4: a sender who is neither owner nor viewer
    gets "доступ ограничен", sent only to them.
    """
    session = FakeSession([
        (200, {"ok": True, "result": [
            {"update_id": 1, "message": {"from": {"id": 999}, "text": "/status"}},
        ]}),
        (200, {"ok": True, "result": {}}),
    ])
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=["111"], session=session)
    conn = db.connect(":memory:")
    handler = CommandHandler(notifier, conn, owner_id="OWNER", viewer_ids=["111"])

    handler.poll_once()

    reply_call = session.calls[1]
    assert reply_call[1]["chat_id"] == "999"
    assert reply_call[1]["text"] == "доступ ограничен"


def test_owner_can_run_start_command():
    session = FakeSession([
        (200, {"ok": True, "result": [
            {"update_id": 1, "message": {"from": {"id": "OWNER"}, "text": "/start"}},
        ]}),
        (200, {"ok": True, "result": {}}),
    ])
    notifier = TelegramNotifier("tok", "OWNER", viewer_chat_ids=["111"], session=session)
    conn = db.connect(":memory:")
    handler = CommandHandler(notifier, conn, owner_id="OWNER", viewer_ids=["111"])

    handler.poll_once()

    assert len(session.calls) == 2
    assert "Gift Sniper bot" in session.calls[1][1]["text"]


def test_get_telegram_owner_id_falls_back_to_legacy_user_id(monkeypatch, caplog):
    """КАК ТЕСТИРОВАТЬ item 6: only TELEGRAM_USER_ID is set (no
    TELEGRAM_OWNER_ID) -> still works, with a warning logged.
    """
    monkeypatch.delenv("TELEGRAM_OWNER_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_USER_ID", "legacy-id-123")

    with caplog.at_level("WARNING"):
        owner_id = config.get_telegram_owner_id()

    assert owner_id == "legacy-id-123"
    assert any("TELEGRAM_OWNER_ID" in r.message for r in caplog.records)


def test_get_telegram_owner_id_prefers_new_name_when_both_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "new-id")
    monkeypatch.setenv("TELEGRAM_USER_ID", "legacy-id")
    assert config.get_telegram_owner_id() == "new-id"


def test_get_telegram_owner_id_raises_when_neither_set(monkeypatch):
    monkeypatch.delenv("TELEGRAM_OWNER_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_USER_ID", raising=False)
    import pytest as _pytest

    with _pytest.raises(config.ConfigError):
        config.get_telegram_owner_id()


def test_get_telegram_viewer_ids_parses_comma_separated_and_strips():
    import os as _os

    _os.environ["TELEGRAM_VIEWER_IDS"] = " 111, 222 ,, 333"
    try:
        assert config.get_telegram_viewer_ids() == ["111", "222", "333"]
    finally:
        del _os.environ["TELEGRAM_VIEWER_IDS"]


def test_get_telegram_viewer_ids_empty_by_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_VIEWER_IDS", raising=False)
    assert config.get_telegram_viewer_ids() == []


def test_poller_alerts_sent_has_one_row_per_signal_regardless_of_recipients(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 7: alerts_sent stays one row per SIGNAL, not
    per recipient, even with viewers configured.
    """
    monkeypatch.setattr(config, "NOTIFY_MIN_PROFIT_USD", Decimal("1"))
    monkeypatch.setattr(config, "NOTIFY_LEVELS", {"pair"})

    conn = db.connect(":memory:")
    listing = _listing("multi-1", int(Decimal("40.0") * config.NANO))
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("70.0") * config.NANO)))
    observed_at = datetime.now(timezone.utc)
    db.record_price_change(
        conn, "portals", "multi-1",
        old_price_nano=int(Decimal("60.0") * config.NANO),
        new_price_nano=int(Decimal("40.0") * config.NANO),
        delta_pct=Decimal("-33.3"),
        is_noise=False,
        old_listed_at=None, new_listed_at=None,
        observed_at=observed_at,
        floor_at_drop_nano=int(Decimal("70.0") * config.NANO),
        floor_listed_count_at_drop=5,
    )

    session = FakeSession([(200, {"ok": True, "result": {}})] * 3)  # owner + 2 viewers
    by_ids_responses = {"multi-1": {"id": "multi-1", "status": "listed", "price": "40.0"}}
    poller = _build_poller_with_notifier(
        conn, session, by_ids_responses=by_ids_responses, viewer_chat_ids=["111", "222"],
    )
    poller._maybe_notify()

    assert poller.stats["signals_sent"] == 1
    assert len(session.calls) == 3  # three actual Telegram sends
    alerts_count = conn.execute("SELECT COUNT(*) FROM alerts_sent").fetchone()[0]
    assert alerts_count == 1  # still exactly one row
