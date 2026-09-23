"""Правка 2 (Tonnel/preview delivery follow-up): A/B measurement of
whether a "warm-up" GET on t.me/nft/<tg_id> (fetching the page before
sending the Telegram message) makes the gift-card preview unfurl more
reliably. Debugging with response logging already confirmed the SENT
message itself is correct on all five test signals (Telegram returns
ok=true, link_preview_options with the right url, entities with a
correct text_link) -- the earlier missing-preview case is NOT a
message-construction defect on our side. The hypothesis under test is
that Telegram's client fetches the preview asynchronously, after
receiving the message, and a cold/slow t.me/nft/<tg_id> response at that
moment is why a card is sometimes missing. THIS SCRIPT ONLY MEASURES --
it does not implement the warm-up in the real send path (poller.py);
that only happens if this measurement supports it.

Uses signals.clean_signals() -- the SAME selection send_test_signals.py
and the real poller use, not a separate/duplicated query. Splits the N
signals deterministically by index parity: even index -> group A (no
warm-up, sent as today), odd index -> group B (one GET on
t.me/nft/<tg_id> with a desktop-browser User-Agent, awaited, THEN sent).
3 seconds between any two sends (Telegram's ~20 msg/min/chat limit).
Every message gets a "[A] <n>" / "[B] <n>" prefix line so a human can
count, after the fact, how many cards appeared in each group.

Like send_test_signals.py: never writes to alerts_sent (so it can never
affect SIGNAL_COOLDOWN_MIN or real-bot dedup), never runs the pre-send
freshness check -- this is a delivery test, not a trading decision.

Run: python -m gift_sniper.preview_ab_test --db gift_sniper.db --count 40
"""
from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal

import requests

from . import config, db
from .notifier import TelegramNotifier
from .signals import Signal, clean_signals

WARMUP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _nft_page_url(tg_id: str) -> str:
    return f"https://t.me/nft/{tg_id}"


class PreviewABTest:
    """`warm_session` is injectable (like TelegramNotifier's `session`)
    so tests can assert group B makes exactly one GET per send and group
    A makes zero, without touching the network.
    """

    def __init__(
        self,
        notifier: TelegramNotifier,
        warm_session: requests.Session | None = None,
        sleep_fn=time.sleep,
        delay_sec: float = 3.0,
    ):
        self._notifier = notifier
        self._warm_session = warm_session or requests.Session()
        self._sleep = sleep_fn
        self._delay_sec = delay_sec

    def _warm(self, tg_id: str) -> None:
        try:
            self._warm_session.get(
                _nft_page_url(tg_id), headers={"User-Agent": WARMUP_USER_AGENT}, timeout=15
            )
        except requests.exceptions.RequestException:
            # A failed warm-up fetch is itself part of the measurement
            # (does warm-up help even when IT is slow/fails?) -- never
            # block the send over it.
            pass

    def run(self, signals_list: list[Signal]) -> dict:
        """Returns {"sent_a": int, "sent_b": int, "total_a": int, "total_b": int}."""
        stats = {"sent_a": 0, "sent_b": 0, "total_a": 0, "total_b": 0}
        for i, signal in enumerate(signals_list):
            group = "A" if i % 2 == 0 else "B"
            stats[f"total_{group.lower()}"] += 1

            if group == "B" and signal.tg_id:
                self._warm(signal.tg_id)

            prefix = f"[{group}] {i + 1}"
            ok = self._notifier.send_signal(signal, test_send=True, prefix=prefix)
            if ok:
                stats[f"sent_{group.lower()}"] += 1
            else:
                print(f"failed to send [{group}] {i + 1} listing_external_id={signal.listing_external_id}", file=sys.stderr)

            if i < len(signals_list) - 1:
                self._sleep(self._delay_sec)

        return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--count", type=int, default=40, help="How many of the most recent clean signals to send.")
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)  # runs migrations -- NOT a bare sqlite3.connect()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to run: {exc}", file=sys.stderr)
        return 1

    bot_token = config.get_telegram_bot_token()
    user_id = config.get_telegram_user_id()

    latest_config = db.get_latest_market_config(conn)
    usd_rate = Decimal(1)
    if latest_config is not None and latest_config["usd_course"]:
        try:
            usd_rate = Decimal(latest_config["usd_course"])
        except Exception:
            pass

    all_signals = clean_signals(conn, usd_rate=usd_rate)
    all_signals.sort(key=lambda s: s.observed_at, reverse=True)
    to_send = all_signals[: args.count]

    if len(to_send) < args.count:
        print(
            f"Only {len(to_send)} clean signal(s) available in the DB "
            f"(requested {args.count}) -- sending what's there."
        )

    if not to_send:
        print("No clean signals in the DB to send.")
        return 0

    notifier = TelegramNotifier(bot_token, chat_id=user_id)
    ab_test = PreviewABTest(notifier)
    stats = ab_test.run(to_send)

    print()
    print(f"group A (no warm-up): sent {stats['sent_a']}/{stats['total_a']}")
    print(f"group B (warm-up GET before send): sent {stats['sent_b']}/{stats['total_b']}")
    print()
    print(
        "Вручную посчитайте в Telegram, у скольких сообщений группы A и "
        "группы B появилась карточка подарка (превью), и сообщите два "
        "числа -- это и есть результат измерения."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
