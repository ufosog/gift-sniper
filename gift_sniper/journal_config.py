"""Paper-journal parameters. Every value below was fixed BEFORE the
observation period starts and must not change during it -- tuning them
towards a nicer result is exactly what the journal exists to prevent.
Env overrides exist only so a NEW period can start with new values.

Source of every measured value: confirmed MRKT sales (the only marketplace
with a `sale` event carrying a price) -- 775 sales, 554 with a known
lifetime, 61 with a pair floor at the moment of sale. See README's
"Paper journal" section for the measured numbers behind each default.

Realization rates and marketplace fees are NOT duplicated here: they come
from config.REALIZATION_RATE_* (via signals.realization_rate) and
config.MARKETPLACE_FEE_RATE / WITHDRAWAL_FEE_FLAT_NANO.
"""
from __future__ import annotations

from decimal import Decimal

import os

from .config import _PROJECT_DIR, NANO, _decimal_env, _env

# Absolute by default, same reason as config.DB_DSN.
JOURNAL_DB_DSN: str = _env("JOURNAL_DB_DSN") or os.path.join(_PROJECT_DIR, "journal.db")
# Off by default: the pollers only write to journal.db when this is set.
PAPER_JOURNAL_ENABLED: bool = (_env("PAPER_JOURNAL_ENABLED", "0") or "").strip().lower() in ("1", "true", "yes")

START_BALANCE_TON: Decimal = _decimal_env("JOURNAL_START_BALANCE_TON", "100")
START_BALANCE_NANO: int = int(START_BALANCE_TON * NANO)
# No percentage position limit: a lot is bought whole when the balance
# covers its full price (reason "insufficient_balance" otherwise). A 30%
# cap contradicted MAX_POSITIONS=5 (5 x 30% = 150%) and cut 68% of signals
# at a 100 TON bank (signal price median 38.20, max 259.00).
# MAX_POSITIONS keeps the whole bank from going into small lots at once.
MAX_POSITIONS: int = int(_env("JOURNAL_MAX_POSITIONS", "5"))
# Cap on ONE position, in TON. Not a percentage of the balance (that was
# removed: 5 x 30% contradicted MAX_POSITIONS), a flat ceiling against
# concentration. Measured 2026-09-20 on the live journal: a single 285 TON
# lot took 95% of a 300 TON bank and every later signal was rejected for
# lack of money, so the journal studied nothing else. Signal price median
# is 38.20 TON; a 60 TON cap keeps about three quarters of signals
# (measured shares: 30 TON 32%, 40 TON 51%, 50 TON 67%, 70 TON 77%).
# 0 disables the cap.
MAX_POSITION_TON: Decimal = _decimal_env("JOURNAL_MAX_POSITION_TON", "0")
MAX_POSITION_NANO: int = int(MAX_POSITION_TON * NANO)
REINVEST_PCT: Decimal = _decimal_env("JOURNAL_REINVEST_PCT", "50")

# Sniping risk: 5% of lots are taken within 45 s of listing.
EXEC_MIN_AGE_SEC: int = int(_env("JOURNAL_EXEC_MIN_AGE_SEC", "45"))
# The check answers "was the lot still there ~45 s after the signal?".
# A check made later measures survival over a longer time instead: lots
# taken within 45 s 5%, 120 s 6%, 300 s 12%, 600 s 18% (MRKT sales). Up to
# 120 s the bias is about 1 point. An older pending row is never checked:
# it becomes REJECTED exec_check_missed (its fate at 45 s is unknown).
# Measured 2026-09-19: with the check tied to the 30-min closer pass, and
# the closer down for 46 h, "sniped" rows were lots repriced or withdrawn
# hours later, not lots taken within 45 s.
EXEC_MAX_AGE_SEC: int = int(_env("JOURNAL_EXEC_MAX_AGE_SEC", "120"))
# The pending check runs on its own short loop, so a row is checked at
# EXEC_MIN_AGE_SEC .. EXEC_MIN_AGE_SEC + EXEC_POLL_SEC of age.
EXEC_POLL_SEC: int = int(_env("JOURNAL_EXEC_POLL_SEC", "15"))
# A lot sells at exactly its listed price (median sold/listed 1.000 in
# every lifetime bucket, 265/420 exact matches) -- no haggling.
EXEC_SLIPPAGE: Decimal = _decimal_env("JOURNAL_EXEC_SLIPPAGE", "1.00")
# ASSUMPTION, not a measurement: no real trade of an unsold lot has ever
# been observed. Unsold lots stood close to the floor (withdrawn n=10,
# median price/floor 0.90; returned n=32, median 0.77), so dumping one
# needs a clearly lower price.
UNSOLD_LIQUIDATION_RATE: Decimal = _decimal_env("JOURNAL_UNSOLD_LIQUIDATION_RATE", "0.70")
CLOSER_INTERVAL_MIN: int = int(_env("JOURNAL_CLOSER_INTERVAL_MIN", "30"))

# A floor backed by a single other listing is not a market price.
MIN_BOOK_DEPTH: int = 2

# Three scenarios computed at once on the same signals.
#   hold_hours:  sold within 12h 93%, 24h 99%, 48h 100% (sample max 24.5h
#                -- MRKT has been collected only ~1 day, revisit in a week)
#   sell_probability: lots below the pair floor sold 39/75 (52%), n small
# A scenario may override the portfolio limits (start_balance_nano,
# max_positions, max_position_nano); anything it leaves out uses the
# values above.
SCENARIOS: dict[str, dict] = {
    "pess": {"hold_hours": 48, "min_net_spread_pct": Decimal("10"), "sell_probability": Decimal("0.40")},
    "base": {"hold_hours": 24, "min_net_spread_pct": Decimal("7"), "sell_probability": Decimal("0.50")},
    "opt": {"hold_hours": 12, "min_net_spread_pct": Decimal("5"), "sell_probability": Decimal("0.60")},
    # "all": the same rules as base, but with NO portfolio limits -- every
    # clean signal becomes a trade. Added 2026-09-21: with 10 slots and a
    # 300 TON bank the portfolio scenarios closed only ~10 trades a day
    # (no_slot 124 of 242 rejections), so answering "are the signals
    # profitable" would take weeks. This scenario answers the per-signal
    # question directly; the portfolio scenarios still answer "what would
    # my 300 TON have made".
    "all": {"hold_hours": 24, "min_net_spread_pct": Decimal("7"), "sell_probability": Decimal("0.50"),
            "start_balance_nano": int(Decimal("1000000") * NANO), "max_positions": 100000,
            "max_position_nano": 0,
            # No portfolio -> no equity to value, and marking 100+ positions
            # every 30 min was a request storm (see paper_journal.fresh_marks).
            "skip_equity": True},
}


def limits(scenario: str) -> tuple[int, int, int]:
    """(start balance, max positions, max size of one position) in nano."""
    params = SCENARIOS[scenario]
    return (params.get("start_balance_nano", START_BALANCE_NANO),
            params.get("max_positions", MAX_POSITIONS),
            params.get("max_position_nano", MAX_POSITION_NANO))
