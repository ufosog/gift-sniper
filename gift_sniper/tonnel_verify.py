"""SANITY-CHECK script (Tonnel integration delivery, Правка "sanity-check
на живых данных"): takes the 10 most recent clean signals from the DB
(via signals.clean_signals() -- the SAME selection the poller and
send_test_signals.py use, not a separate/duplicated one) and queries
Tonnel's own pair floor for each, printing a table: collection, model,
backdrop, Portals price, Portals floor, Tonnel floor-with-fee, verdict.

Read-only against the DB (no alerts_sent writes, no tonnel_floor_snapshots
writes -- this is a manual spot-check, not the real poller cross-check
path in poller.py's _maybe_cross_check). Requires live network access to
gifts2.tonnel.network -- cannot be exercised in an environment without
network access, so this script is NOT covered by the automated test
suite; the user runs it directly.

Run: python -m gift_sniper.tonnel_verify --db gift_sniper.db --count 10
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from . import config, db
from .signals import clean_signals
from .tonnel_client import TonnelClient, TonnelError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--count", type=int, default=10, help="How many of the most recent clean signals to check.")
    args = parser.parse_args(argv)

    try:
        conn = db.connect(args.db)  # runs migrations -- NOT a bare sqlite3.connect()
    except db.SchemaError as exc:
        print(f"Schema check failed, refusing to run: {exc}", file=sys.stderr)
        return 1

    latest_config = db.get_latest_market_config(conn)
    usd_rate = Decimal(1)
    if latest_config is not None and latest_config["usd_course"]:
        try:
            usd_rate = Decimal(latest_config["usd_course"])
        except Exception:
            pass

    all_signals = clean_signals(conn, usd_rate=usd_rate)
    all_signals.sort(key=lambda s: s.observed_at, reverse=True)
    to_check = all_signals[: args.count]

    if not to_check:
        print("No clean signals in the DB to check.")
        return 0

    tonnel = TonnelClient()

    header = f"{'collection':<16}{'model':<14}{'backdrop':<12}{'portals_price':>14}{'portals_floor':>14}{'tonnel_fee':>12}  verdict"
    print(header)

    for signal in to_check:
        portals_price = Decimal(signal.new_price_nano) / config.NANO
        portals_floor = Decimal(signal.floor_nano) / config.NANO

        if not (signal.collection_name and signal.model_name and signal.backdrop_name):
            print(
                f"{(signal.collection_name or '')[:15]:<16}{(signal.model_name or '')[:13]:<14}"
                f"{(signal.backdrop_name or '')[:11]:<12}{portals_price:>14.2f}{portals_floor:>14.2f}"
                f"{'--':>12}  skipped (missing collection/model/backdrop)"
            )
            continue

        try:
            floor = tonnel.pair_floor(
                gift_name=signal.collection_name,
                model=signal.model_name,
                backdrop=signal.backdrop_name,
                exclude_gift_num=signal.gift_number,
            )
        except TonnelError as exc:
            print(
                f"{signal.collection_name[:15]:<16}{signal.model_name[:13]:<14}"
                f"{signal.backdrop_name[:11]:<12}{portals_price:>14.2f}{portals_floor:>14.2f}"
                f"{'--':>12}  error: {exc}"
            )
            continue

        if floor.status != "ok" or floor.floor_with_fee_nano is None:
            tonnel_fee_str = "--"
            verdict = "no_data"
        else:
            tonnel_fee = Decimal(floor.floor_with_fee_nano) / config.NANO
            tonnel_fee_str = f"{tonnel_fee:.2f}"
            verdict = "confirmed" if signal.new_price_nano < floor.floor_with_fee_nano else "worse"

        print(
            f"{signal.collection_name[:15]:<16}{signal.model_name[:13]:<14}"
            f"{signal.backdrop_name[:11]:<12}{portals_price:>14.2f}{portals_floor:>14.2f}"
            f"{tonnel_fee_str:>12}  {verdict}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
