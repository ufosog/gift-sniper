"""Send-a-test-notification tool: lets the user check Telegram
formatting/rendering (does the gift card unfurl, does the button work,
does the text look right) WITHOUT waiting for a real clean signal to
occur and WITHOUT touching any live-bot bookkeeping.

Uses signals.clean_signals() -- the SAME function the poller calls, not
a separate/duplicated selection -- so what gets sent here is exactly
what the real bot would consider a clean signal. Never writes to
alerts_sent (so it never affects SIGNAL_COOLDOWN_MIN or is_alert_sent
for a real send), never calls the pre-send freshness check (this is a
test of FORMATTING/DELIVERY, not of whether the lot is still tradeable).

Run: python -m gift_sniper.send_test_signals --db gift_sniper.db --count 5
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from . import config, db
from .notifier import TelegramNotifier
from .signals import clean_signals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--count", type=int, default=5, help="How many of the most recent clean signals to send.")
    parser.add_argument(
        "--marketplace", choices=["portals", "tonnel"], default="portals",
        help="Tonnel signals delivery: which source's clean signals to send.",
    )
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)  # runs migrations -- NOT a bare sqlite3.connect()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to run: {exc}", file=sys.stderr)
        return 1

    # TELEGRAM_BOT_TOKEN/TELEGRAM_OWNER_ID are lazily read (like
    # PORTALS_AUTH) -- required here since this script's entire purpose
    # is sending to Telegram, unlike report.py/model_level_audit.py which
    # never need them. Shared between marketplaces, per spec.
    bot_token = config.get_telegram_bot_token()
    owner_id = config.get_telegram_owner_id()
    viewer_ids = config.get_telegram_viewer_ids()

    latest_config = db.get_latest_market_config(conn)
    usd_rate = Decimal(1)
    if latest_config is not None and latest_config["usd_course"]:
        try:
            usd_rate = Decimal(latest_config["usd_course"])
        except Exception:
            pass

    all_signals = clean_signals(conn, usd_rate=usd_rate, marketplace=args.marketplace)
    # Most recent first -- "последние N чистых сигналов" per spec.
    # Sorting here is presentation only (which of the already-computed
    # clean signals to pick), not a re-derivation of the cascade itself.
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

    notifier = TelegramNotifier(bot_token, chat_id=owner_id, viewer_chat_ids=viewer_ids)
    sent = 0
    for signal in to_send:
        ok = notifier.send_signal(signal, test_send=True)
        if ok:
            sent += 1
        else:
            print(f"failed to send test signal for listing_external_id={signal.listing_external_id}", file=sys.stderr)
        # Deliberately NOT calling db.mark_alert_sent() -- a test send
        # must never affect alerts_sent / SIGNAL_COOLDOWN_MIN / is_alert_sent
        # for the real bot.

    print(f"sent {sent}/{len(to_send)} test signal(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
