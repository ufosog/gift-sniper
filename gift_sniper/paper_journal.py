"""Paper journal: what WOULD have happened if every clean signal had been
bought and later sold. No real trading. Writes only to journal.db.

Two entry points:
  record_signal(jconn, signal)  -- called by the pollers after
      _maybe_notify, whether or not a notification was sent. Uses ONLY
      market data carried by the signal itself (no fresh floor at entry:
      that would be lookahead).
  run_closer(jconn, clients)    -- a separate process on a timer
      (`python -m gift_sniper.paper_journal --closer`): real execution
      check of PENDING_EXEC lots, then closing positions past HOLD_HOURS
      with a fresh floor of the SAME level the signal was computed on.

Three scenarios (journal_config.SCENARIOS) run on the same signals. The
sale draw is deterministic (sha256 of signal_id + scenario), so a rerun
reproduces it and scenarios stay comparable.

The model must UNDERSTATE the result: entry at the listed price, exit at
floor * realization_rate, unsold lots dumped at UNSOLD_LIQUIDATION_RATE,
all fees applied.

Closer robustness: a problem with ONE marketplace (no token, network or
API error, anything unexpected) only leaves that marketplace's rows in
their current status -- every other row is still processed, and the pass
itself never dies. Each pass logs one summary line.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import config, journal_config, journal_db, lot_check
from .errors import PortalsError
from .mrkt_client import MrktError
from .signals import Signal, realization_rate
from .tonnel_client import TonnelError

logger = logging.getLogger("gift_sniper.paper_journal")

PENDING_EXEC = "PENDING_EXEC"
OPEN = "OPEN"
REJECTED = "REJECTED"
CLOSED = "CLOSED"
UNSOLD = "UNSOLD"

# Tonnel's confirmed 10% BUYER fee. signals.compute_profit_nano applies the
# same number as `price * 1.1`; test_paper_journal pins the two together.
TONNEL_BUYER_FEE_RATE = Decimal("0.10")


# --- pure helpers ---------------------------------------------------------

def signal_id(signal: Signal) -> str:
    """Same identity as alerts_sent: one price_history row."""
    raw = f"{signal.marketplace}|{signal.listing_external_id}|{signal.observed_at.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def fees(marketplace: str) -> tuple[Decimal, Decimal, int]:
    """(fee_buy_pct, fee_sell_pct, network_fee_nano) of the entry marketplace."""
    if marketplace == "tonnel":
        return TONNEL_BUYER_FEE_RATE, Decimal(0), 0
    if marketplace == "mrkt":
        # MRKT's 2% buyer fee is already inside the listed price.
        return Decimal(0), config.MARKETPLACE_FEE_RATE, 0
    return Decimal(0), config.MARKETPLACE_FEE_RATE, config.WITHDRAWAL_FEE_FLAT_NANO


def pnl_nano(marketplace: str, exit_nano, entry_nano) -> int:
    fee_buy, fee_sell, network = fees(marketplace)
    return int(Decimal(exit_nano) * (1 - fee_sell) - Decimal(entry_nano) * (1 + fee_buy) - network)


def draw(sid: str, scenario: str) -> float:
    """Deterministic number in [0, 1) for the "did it sell" draw."""
    digest = hashlib.sha256(f"{sid}{scenario}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _state(jconn: sqlite3.Connection, scenario: str) -> sqlite3.Row:
    return jconn.execute("SELECT * FROM journal_state WHERE scenario = ?", (scenario,)).fetchone()


def _count(jconn: sqlite3.Connection, scenario: str, statuses: tuple[str, ...]) -> int:
    marks = ",".join("?" * len(statuses))
    return jconn.execute(
        f"SELECT COUNT(*) FROM journal_signals WHERE scenario = ? AND status IN ({marks})",
        (scenario, *statuses),
    ).fetchone()[0]


# --- record_signal --------------------------------------------------------

def _reject_reason(jconn, scenario, params, entry: Decimal, expected_pnl: int, depth: int) -> str | None:
    """Signal-quality reasons first, portfolio reasons last: the reason
    statistics must name what is wrong with the signal itself, not whether
    a slot happened to be free. A thin book usually also gives a low
    spread (realization rate 0.61 at depth 1), so thin_book goes first.
    """
    if depth < journal_config.MIN_BOOK_DEPTH:
        return "thin_book"
    _start, max_positions, max_position_nano = journal_config.limits(scenario)
    if max_position_nano and entry > max_position_nano:
        return "position_too_big"
    if entry <= 0 or Decimal(expected_pnl) * 100 / entry < params["min_net_spread_pct"]:
        return "spread_too_low"
    # Only OPEN positions hold a slot. A pending row holds nothing: it is
    # checked within EXEC_MAX_AGE_SEC, and the slot is taken only on OPEN.
    if _count(jconn, scenario, (OPEN,)) >= max_positions:
        return "no_slot"
    # The whole lot is bought: the balance must cover its full price.
    if entry > _state(jconn, scenario)["balance_nano"]:
        return "insufficient_balance"
    return None


def record_signal(jconn: sqlite3.Connection, signal: Signal, now: datetime | None = None) -> bool:
    """Writes one row per scenario. Returns False if the signal was
    already recorded (idempotent). Never makes a network request.

    With `now` given (the pollers pass it), a signal already older than
    EXEC_MAX_AGE_SEC can never get a real execution check, so it is
    rejected straight away with the same reason the closer would give.
    Measured 2026-09-20: after a restart the poller looks an hour back
    (`_last_notify_since`), so 6 of 8 fresh rows were hour-old signals that
    sat in PENDING_EXEC only to be expired a moment later. `now=None`
    (tests, replays) skips the age check entirely.
    """
    sid = signal_id(signal)
    if jconn.execute("SELECT 1 FROM journal_signals WHERE signal_id = ? LIMIT 1", (sid,)).fetchone():
        return False

    marketplace = signal.marketplace
    depth = signal.listed_count or 0
    rate = realization_rate(depth)
    entry = Decimal(signal.new_price_nano) * journal_config.EXEC_SLIPPAGE
    expected_exit = Decimal(signal.floor_nano) * rate
    expected_pnl = pnl_nano(marketplace, expected_exit, entry)
    fee_buy, fee_sell, network = fees(marketplace)

    too_old = now is not None and (now - signal.observed_at).total_seconds() > journal_config.EXEC_MAX_AGE_SEC

    with jconn:
        for scenario, params in journal_config.SCENARIOS.items():
            reason = "exec_check_missed" if too_old else _reject_reason(
                jconn, scenario, params, entry, expected_pnl, depth)
            jconn.execute(
                """
                INSERT INTO journal_signals (
                    signal_id, scenario, ts, marketplace, listing_external_id, tg_id,
                    collection, collection_id, model, backdrop, gift_number,
                    price_nano, floor_nano, floor_level, floor_depth, floor_source,
                    cross_verdict, ratio, realization_rate,
                    fee_buy_pct, fee_sell_pct, network_fee_nano,
                    expected_exit_nano, expected_pnl_nano,
                    status, reject_reason, position_size_nano
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid, scenario, signal.observed_at.isoformat(), marketplace, signal.listing_external_id,
                    signal.tg_id, signal.collection_name, signal.collection_id, signal.model_name,
                    signal.backdrop_name, signal.gift_number,
                    signal.new_price_nano, signal.floor_nano, signal.floor_level, depth, signal.floor_source,
                    signal.cross_verdict, str(signal.ratio) if signal.ratio is not None else None, str(rate),
                    str(fee_buy), str(fee_sell), network,
                    int(expected_exit), expected_pnl,
                    REJECTED if reason else PENDING_EXEC, reason, int(entry),
                ),
            )
    return True


def record_signals_safely(jconn: sqlite3.Connection | None, signals: list[Signal],
                          now: datetime | None = None) -> None:
    """Poller hook. A journal failure must never affect collection or
    notifications, so every error is logged and swallowed. `now` defaults
    to the real current time here (unlike record_signal), because every
    caller is a live poller: that enables the age check.
    """
    if jconn is None:
        return
    now = now or datetime.now(timezone.utc)
    for signal in signals:
        try:
            record_signal(jconn, signal, now=now)
        except Exception:
            logger.exception("paper journal: record_signal failed for %s", signal.listing_external_id)


class UptimeTracker:
    """One journal_uptime row per poller process run. ended_at is moved
    forward on every heartbeat, so a crash leaves the last heartbeat as
    the end of the period -- downtime is never counted as coverage. If the
    row is gone (journal reset), a new one is started.
    """

    def __init__(self, jconn: sqlite3.Connection, marketplace: str):
        self._jconn = jconn
        self._marketplace = marketplace
        self._rowid: int | None = None

    def heartbeat(self, now: datetime | None = None) -> None:
        now_iso = (now or datetime.now(timezone.utc)).isoformat()
        try:
            with self._jconn:
                if self._rowid is not None:
                    cur = self._jconn.execute(
                        "UPDATE journal_uptime SET ended_at = ? WHERE rowid = ?", (now_iso, self._rowid)
                    )
                    if cur.rowcount:
                        return
                cur = self._jconn.execute(
                    "INSERT INTO journal_uptime (marketplace, started_at, ended_at) VALUES (?, ?, ?)",
                    (self._marketplace, now_iso, now_iso),
                )
                self._rowid = cur.lastrowid
        except Exception:
            logger.exception("paper journal: uptime heartbeat failed")


# --- run_closer -----------------------------------------------------------

@dataclass
class JournalClients:
    portals: object = None
    portals_floor: object = None  # PairFloorCache; built from `portals` if None
    tonnel: object = None
    mrkt: object = None
    # gift_sniper.db (read-only): Portals collection_id by name, needed to
    # query Portals as a competitor for a Tonnel/MRKT position.
    gift_conn: object = None

    def __post_init__(self):
        if self.portals is not None and self.portals_floor is None:
            from .pair_floor import PairFloorCache
            self.portals_floor = PairFloorCache(self.portals)


class _Unavailable(Exception):
    """The check could not be made (no client, network/API error). The row
    stays in its current status and is retried on the next pass."""


BUYABLE_STATES = ("listed", "repriced_down")


def _exec_state(row: sqlite3.Row, clients: JournalClients) -> tuple[str, int | None]:
    """(exec_state, current price). A lot repriced DOWN is still bought,
    entered at the signal price: that understates the result, as the
    journal must."""
    lot = _lot_state(row, clients)
    if lot.state != lot_check.LISTED:
        return lot.state, None
    if lot.price_nano == row["price_nano"]:
        return "listed", lot.price_nano
    return ("repriced_down" if lot.price_nano < row["price_nano"] else "repriced_up"), lot.price_nano


def _lot_state(row: sqlite3.Row, clients: JournalClients) -> lot_check.LotState:
    marketplace = row["marketplace"]
    try:
        if marketplace == "portals" and clients.portals is not None:
            result = lot_check.portals_state(clients.portals.search_by_ids([row["listing_external_id"]]))
        elif marketplace == "tonnel" and clients.tonnel is not None:
            gift_id = int(row["listing_external_id"])
            result = lot_check.tonnel_state(clients.tonnel.search_minimal_by_gift_ids([gift_id], limit=1))
        elif marketplace == "mrkt" and clients.mrkt is not None and row["collection"] and row["gift_number"] is not None:
            result = lot_check.mrkt_state(clients.mrkt.find_by_number(row["collection"], row["gift_number"]))
        else:
            raise _Unavailable(f"no client or identity for {marketplace}")
    except _Unavailable:
        raise
    except (PortalsError, TonnelError, MrktError, ValueError) as exc:
        raise _Unavailable(str(exc)) from exc
    except Exception as exc:  # anything unexpected from one marketplace's client
        logger.exception("paper journal: unexpected error checking %s lot %s", marketplace, row["listing_external_id"])
        raise _Unavailable(f"unexpected: {exc}") from exc
    if result is None:
        raise _Unavailable("unusable lookup response")
    return result


def _floor_now(row: sqlite3.Row, clients: JournalClients) -> tuple[int | None, int]:
    """(floor_nano or None, depth) for the SAME level the signal used, on
    the entry marketplace. Raises _Unavailable on a failed query."""
    marketplace = row["marketplace"]
    model_level = row["floor_level"] == "model"
    try:
        if marketplace == "portals" and clients.portals_floor is not None:
            if not row["collection_id"]:
                return None, 0
            if model_level:
                floor, _age = clients.portals_floor.get_model_floor_fresh(
                    row["collection_id"], row["model"], row["listing_external_id"])
            else:
                floor, _age = clients.portals_floor.get_fresh(
                    row["collection_id"], row["model"], row["backdrop"], row["listing_external_id"])
            if floor.status == "error":
                raise _Unavailable("portals floor query failed")
            return floor.floor_excluding_self_nano, floor.listed_count_excluding_self
        if marketplace == "tonnel" and clients.tonnel is not None:
            if model_level:
                floor = clients.tonnel.model_floor(
                    gift_name=row["collection"], model=row["model"], exclude_gift_num=row["gift_number"])
            else:
                floor = clients.tonnel.pair_floor(
                    gift_name=row["collection"], model=row["model"], backdrop=row["backdrop"],
                    exclude_gift_num=row["gift_number"])
            # floor_nano without the buyer fee: this is what a seller receives.
            return (floor.floor_nano if floor.status == "ok" else None), floor.listed_count
        if marketplace == "mrkt" and clients.mrkt is not None:
            if model_level:
                floor = clients.mrkt.model_floor(
                    collection_name=row["collection"], model_name=row["model"], exclude_number=row["gift_number"])
            else:
                floor = clients.mrkt.pair_floor(
                    collection_name=row["collection"], model_name=row["model"], backdrop_name=row["backdrop"],
                    exclude_number=row["gift_number"])
            return (floor.floor_nano if floor.status == "ok" else None), floor.listed_count
    except (PortalsError, TonnelError, MrktError) as exc:
        raise _Unavailable(str(exc)) from exc
    raise _Unavailable(f"no client for {marketplace}")


def _cross_market_cap(row: sqlite3.Row, clients: JournalClients) -> int | None:
    """Highest price the lot can be listed at on its entry marketplace and
    still be the cheapest offer of the same level (pair or model) across
    all three marketplaces, compared at buyer cost. A buyer who can get the
    same thing cheaper elsewhere buys it there.

    Measured 2026-09-19: Tonnel Bonded Ring Leopard #7588 bought at 40.25
    had a Tonnel model floor of 150 (one lot; the rest from 227), while
    MRKT had the model from 42.13 (12 lots), Portals from 42.55 (25), and
    12 of 13 MRKT sales of the collection went at 40-42. Closing at the
    Tonnel floor would have booked a sale at ~142.

    None: no competitor offer found. Raises _Unavailable when a competitor
    query fails, so the close is postponed instead of booked without the cap.
    """
    from types import SimpleNamespace
    from . import cross_check as cc
    lot = SimpleNamespace(
        collection_name=row["collection"], model_name=row["model"], backdrop_name=row["backdrop"],
        gift_number=row["gift_number"], listing_external_id=row["listing_external_id"],
    )
    model_level = row["floor_level"] == "model"
    competitors = {
        "portals": (clients.portals, cc._query_portals_neighbour_model if model_level else cc._query_portals_neighbour),
        "tonnel": (clients.tonnel, cc._query_tonnel_neighbour_model if model_level else cc._query_tonnel_neighbour),
        "mrkt": (clients.mrkt, cc._query_mrkt_neighbour_model if model_level else cc._query_mrkt_neighbour),
    }
    costs = []
    for marketplace, (client, query) in competitors.items():
        if marketplace == row["marketplace"] or client is None:
            continue
        if marketplace == "portals" and clients.gift_conn is None:
            continue
        try:
            floor_nano, _count, status = query(clients.gift_conn, client, lot)
        except (PortalsError, TonnelError, MrktError) as exc:
            raise _Unavailable(f"{marketplace} competitor query failed: {exc}") from exc
        if status == "ok" and floor_nano:
            costs.append(floor_nano)
    if not costs:
        return None
    buyer_fee = TONNEL_BUYER_FEE_RATE if row["marketplace"] == "tonnel" else Decimal(0)
    return int(Decimal(min(costs)) / (1 + buyer_fee))


def _new_stats() -> dict:
    return {
        "pending_processed": 0, "opened": 0, "sniped": 0, "rejected_on_open": 0, "exec_check_missed": 0,
        "closed": 0, "unsold": 0,
        "skipped_unavailable": Counter(),  # marketplace -> rows left in PENDING_EXEC
        "close_postponed": Counter(),  # marketplace -> OPEN rows past HOLD_HOURS not closed
    }


def _process_pending(jconn, clients: JournalClients, now: datetime, stats: dict) -> None:
    """Execution check of PENDING_EXEC rows aged EXEC_MIN_AGE_SEC ..
    EXEC_MAX_AGE_SEC. An older row is never checked: a late check would
    measure whether the lot survived minutes or hours, not 45 s."""
    cutoff = now - timedelta(seconds=journal_config.EXEC_MIN_AGE_SEC)
    expired_before = now - timedelta(seconds=journal_config.EXEC_MAX_AGE_SEC)
    rows = jconn.execute(
        "SELECT * FROM journal_signals WHERE status = ? ORDER BY ts, signal_id, scenario", (PENDING_EXEC,)
    ).fetchall()
    by_signal: dict[str, list[sqlite3.Row]] = {}
    expired: list[sqlite3.Row] = []
    for row in rows:
        ts = _dt(row["ts"])
        if ts < expired_before:
            expired.append(row)
        elif ts <= cutoff:
            by_signal.setdefault(row["signal_id"], []).append(row)

    if expired:
        with jconn:
            for row in expired:
                _set_rejected(jconn, row, "exec_check_missed")
        stats["exec_check_missed"] += len(expired)

    for sid, signal_rows in by_signal.items():
        marketplace = signal_rows[0]["marketplace"]
        # One real lookup per signal, the same answer for every scenario.
        try:
            exec_state, exec_price = _exec_state(signal_rows[0], clients)
        except _Unavailable as exc:
            stats["skipped_unavailable"][marketplace] += len(signal_rows)
            logger.debug("paper journal: execution check postponed for %s (%s): %s", sid, marketplace, exc)
            continue
        with jconn:
            for row in signal_rows:
                stats["pending_processed"] += 1
                scenario = row["scenario"]
                jconn.execute(
                    "UPDATE journal_signals SET exec_checked_at = ?, exec_state = ?, exec_price_nano = ? "
                    "WHERE signal_id = ? AND scenario = ?",
                    (now.isoformat(), exec_state, exec_price, sid, scenario),
                )
                if exec_state not in BUYABLE_STATES:
                    _set_rejected(jconn, row, "sniped")
                    stats["sniped"] += 1
                    continue
                balance = _state(jconn, scenario)["balance_nano"]
                if _lot_held(jconn, scenario, row):
                    # One physical lot is bought once: a second signal on a
                    # lot already held is not a second trade.
                    _set_rejected(jconn, row, "lot_already_held")
                    stats["rejected_on_open"] += 1
                elif _count(jconn, scenario, (OPEN,)) >= journal_config.limits(scenario)[1]:
                    _set_rejected(jconn, row, "no_slot")
                    stats["rejected_on_open"] += 1
                elif row["position_size_nano"] > balance:
                    _set_rejected(jconn, row, "insufficient_balance")
                    stats["rejected_on_open"] += 1
                else:
                    jconn.execute(
                        "UPDATE journal_signals SET status = ?, ts_open = ? WHERE signal_id = ? AND scenario = ?",
                        (OPEN, now.isoformat(), sid, scenario),
                    )
                    jconn.execute(
                        "UPDATE journal_state SET balance_nano = balance_nano - ?, updated_at = ? WHERE scenario = ?",
                        (row["position_size_nano"], now.isoformat(), scenario),
                    )
                    stats["opened"] += 1


def _lot_held(jconn, scenario: str, row: sqlite3.Row) -> bool:
    return jconn.execute(
        "SELECT 1 FROM journal_signals WHERE scenario = ? AND status = ? AND marketplace = ? "
        "AND listing_external_id = ? LIMIT 1",
        (scenario, OPEN, row["marketplace"], row["listing_external_id"]),
    ).fetchone() is not None


def _set_rejected(jconn, row, reason: str) -> None:
    jconn.execute(
        "UPDATE journal_signals SET status = ?, reject_reason = ? WHERE signal_id = ? AND scenario = ?",
        (REJECTED, reason, row["signal_id"], row["scenario"]),
    )


def _close_position(jconn, row, floor_nano: int | None, depth: int, now: datetime) -> str:
    scenario = row["scenario"]
    params = journal_config.SCENARIOS[scenario]
    entry = row["position_size_nano"]
    if floor_nano is None:
        exit_price = Decimal(row["price_nano"]) * journal_config.UNSOLD_LIQUIDATION_RATE
        status, sold = UNSOLD, 0
    elif Decimal(str(draw(row["signal_id"], scenario))) < params["sell_probability"]:
        exit_price = Decimal(floor_nano) * realization_rate(depth)
        status, sold = CLOSED, 1
    else:
        exit_price = Decimal(floor_nano) * journal_config.UNSOLD_LIQUIDATION_RATE
        status, sold = UNSOLD, 0

    pnl = pnl_nano(row["marketplace"], exit_price, entry)
    pnl_pct = (Decimal(pnl) * 100 / Decimal(entry)).quantize(Decimal("0.01")) if entry else Decimal(0)
    withdraw = int(Decimal(pnl) * (1 - journal_config.REINVEST_PCT / 100)) if pnl > 0 else 0

    jconn.execute(
        """
        UPDATE journal_signals SET status = ?, ts_close = ?, exit_price_nano = ?, pnl_nano = ?, pnl_pct = ?,
            floor_at_close_nano = ?, depth_at_close = ?, sold_flag = ?
        WHERE signal_id = ? AND scenario = ?
        """,
        (status, now.isoformat(), int(exit_price), pnl, str(pnl_pct), floor_nano, depth, sold,
         row["signal_id"], scenario),
    )
    jconn.execute(
        "UPDATE journal_state SET balance_nano = balance_nano + ?, withdrawn_nano = withdrawn_nano + ?, "
        "updated_at = ? WHERE scenario = ?",
        (entry + pnl - withdraw, withdraw, now.isoformat(), scenario),
    )
    return status


def fresh_marks(jconn, scenario: str, clients: JournalClients, floor_cache: dict) -> dict[str, int]:
    """Current value of every OPEN position: floor_now * realization_rate.
    NETWORK WORK, and therefore strictly OUTSIDE any transaction -- holding
    journal.db's write lock across HTTP calls made every other process fail
    with "database is locked" (212 times in 24 h once the unlimited "all"
    scenario reached 138 open positions; found by the daily review
    2026-09-22). A scenario marked `skip_equity` gets no marks at all: it
    has no portfolio to value, and 138 positions x 3 marketplaces every
    30 minutes is a needless request storm.
    """
    if journal_config.SCENARIOS[scenario].get("skip_equity"):
        return {}
    marks: dict[str, int] = {}
    for row in jconn.execute(
            "SELECT * FROM journal_signals WHERE scenario = ? AND status = ?", (scenario, OPEN)).fetchall():
        try:
            floor_nano, depth, _cap = _capped_floor(row, clients, floor_cache)
        except _Unavailable:
            continue
        if floor_nano is not None:
            marks[row["signal_id"]] = int(Decimal(floor_nano) * realization_rate(depth))
    return marks


def _update_equity(jconn, scenario: str, marks: dict[str, int], now: datetime) -> None:
    """DB WRITES ONLY (see fresh_marks): equity = balance + every OPEN
    position at its mark, or its entry cost when no mark could be had."""
    state = _state(jconn, scenario)
    open_rows = jconn.execute(
        "SELECT * FROM journal_signals WHERE scenario = ? AND status = ?", (scenario, OPEN)
    ).fetchall()
    equity = state["balance_nano"]
    for row in open_rows:
        value = marks.get(row["signal_id"])
        if value is not None:
            jconn.execute(
                "UPDATE journal_signals SET mark_nano = ?, mark_at = ? WHERE signal_id = ? AND scenario = ?",
                (value, now.isoformat(), row["signal_id"], scenario),
            )
        elif row["mark_nano"] is not None:
            value = row["mark_nano"]
        else:
            value = row["position_size_nano"]
        equity += int(value)
    jconn.execute(
        "UPDATE journal_state SET equity_nano = ?, open_positions = ?, updated_at = ? WHERE scenario = ?",
        (equity, len(open_rows), now.isoformat(), scenario),
    )
    jconn.execute(
        "INSERT INTO journal_equity_log (scenario, ts, balance_nano, withdrawn_nano, equity_nano) VALUES (?, ?, ?, ?, ?)",
        (scenario, now.isoformat(), state["balance_nano"], state["withdrawn_nano"], equity),
    )


def _cached_floor(row, clients: JournalClients, floor_cache: dict) -> tuple[int | None, int]:
    """One floor query per signal per pass, shared by every scenario."""
    key = row["signal_id"]
    if key not in floor_cache:
        try:
            floor_cache[key] = _floor_now(row, clients)
        except _Unavailable as exc:
            floor_cache[key] = exc
        except Exception as exc:  # anything unexpected from one marketplace's client
            logger.exception("paper journal: unexpected error fetching %s floor for %s", row["marketplace"], key)
            floor_cache[key] = _Unavailable(f"unexpected: {exc}")
    cached = floor_cache[key]
    if isinstance(cached, _Unavailable):
        raise cached
    return cached


def _capped_floor(row, clients: JournalClients, floor_cache: dict) -> tuple[int | None, int, int | None]:
    """(floor capped by the cross-market price, depth, cap). One set of
    competitor queries per signal per pass."""
    floor_nano, depth = _cached_floor(row, clients, floor_cache)
    key = ("cap", row["signal_id"])
    if key not in floor_cache:
        try:
            floor_cache[key] = _cross_market_cap(row, clients)
        except _Unavailable as exc:
            floor_cache[key] = exc
        except Exception as exc:
            logger.exception("paper journal: unexpected error in cross-market cap for %s", row["signal_id"])
            floor_cache[key] = _Unavailable(f"unexpected: {exc}")
    cap = floor_cache[key]
    if isinstance(cap, _Unavailable):
        raise cap
    if floor_nano is not None and cap is not None:
        floor_nano = min(floor_nano, cap)
    return floor_nano, depth, cap


def run_exec_check(jconn: sqlite3.Connection, clients: JournalClients, now: datetime | None = None) -> dict:
    """Execution check only. Runs every EXEC_POLL_SEC, so each pending row
    is checked close to EXEC_MIN_AGE_SEC of age."""
    now = now or datetime.now(timezone.utc)
    stats = _new_stats()
    _process_pending(jconn, clients, now, stats)
    if stats["pending_processed"] or stats["exec_check_missed"] or stats["skipped_unavailable"]:
        logger.info(
            "paper journal exec check: processed=%d opened=%d sniped=%d rejected_on_open=%d "
            "exec_check_missed=%d skipped_unavailable=%d %s",
            stats["pending_processed"], stats["opened"], stats["sniped"], stats["rejected_on_open"],
            stats["exec_check_missed"], sum(stats["skipped_unavailable"].values()), dict(stats["skipped_unavailable"]),
        )
    return stats


def run_closer(jconn: sqlite3.Connection, clients: JournalClients, now: datetime | None = None) -> dict:
    """One closer pass: execution check, then closing. Returns the pass
    statistics (also logged)."""
    now = now or datetime.now(timezone.utc)
    stats = _new_stats()
    _process_pending(jconn, clients, now, stats)

    floor_cache: dict = {}
    for scenario, params in journal_config.SCENARIOS.items():
        due_before = now - timedelta(hours=params["hold_hours"])
        open_rows = jconn.execute(
            "SELECT * FROM journal_signals WHERE scenario = ? AND status = ? ORDER BY ts_open", (scenario, OPEN)
        ).fetchall()
        for row in open_rows:
            if _dt(row["ts_open"]) > due_before:
                continue
            try:
                floor_nano, depth, cap = _capped_floor(row, clients, floor_cache)
            except _Unavailable as exc:
                stats["close_postponed"][row["marketplace"]] += 1
                logger.debug("paper journal: close postponed for %s/%s: %s", row["signal_id"], scenario, exc)
                continue
            with jconn:  # DB only: every network call for this row is done
                status = _close_position(jconn, row, floor_nano, depth, now)
                jconn.execute("UPDATE journal_signals SET exit_cap_nano = ? WHERE signal_id = ? AND scenario = ?",
                              (cap, row["signal_id"], row["scenario"]))
            stats["closed" if status == CLOSED else "unsold"] += 1
        # Marks first (network), then one short transaction for the writes.
        marks = fresh_marks(jconn, scenario, clients, floor_cache)
        with jconn:
            _update_equity(jconn, scenario, marks, now)

    logger.info(
        "paper journal closer pass: pending processed=%d opened=%d sniped=%d rejected_on_open=%d "
        "exec_check_missed=%d skipped_unavailable=%d %s | closed=%d unsold=%d close_postponed=%d %s",
        stats["pending_processed"], stats["opened"], stats["sniped"], stats["rejected_on_open"],
        stats["exec_check_missed"],
        sum(stats["skipped_unavailable"].values()), dict(stats["skipped_unavailable"]),
        stats["closed"], stats["unsold"],
        sum(stats["close_postponed"].values()), dict(stats["close_postponed"]),
    )
    return stats


# --- CLI ------------------------------------------------------------------

def build_default_clients() -> JournalClients:
    """Each marketplace is built independently: one missing token or a
    failing constructor leaves only that marketplace's rows waiting."""
    clients = JournalClients()
    try:
        clients.gift_conn = sqlite3.connect(f"file:{config.DB_DSN}?mode=ro", uri=True, check_same_thread=False)
        clients.gift_conn.row_factory = sqlite3.Row
    except Exception as exc:
        logger.warning("paper journal: gift_sniper.db unavailable, Portals is not used as a competitor: %s", exc)
    try:
        from .auth import AuthManager
        from .portals_client import PortalsClient
        clients.portals = PortalsClient(auth_provider=AuthManager().get)
        clients.__post_init__()
    except Exception as exc:
        logger.warning("paper journal: Portals unavailable, its rows will wait: %s", exc)
    try:
        from .tonnel_client import TonnelClient
        clients.tonnel = TonnelClient(request_delay_ms=config.TONNEL_REQUEST_DELAY_MS)
    except Exception as exc:
        logger.warning("paper journal: Tonnel unavailable, its rows will wait: %s", exc)
    try:
        from .mrkt_client import build_default_mrkt_client
        clients.mrkt = build_default_mrkt_client()
        if clients.mrkt is None:
            logger.warning("paper journal: MRKT_ACCESS_TOKEN not set, MRKT rows will wait")
    except Exception as exc:
        logger.warning("paper journal: MRKT unavailable, its rows will wait: %s", exc)
    return clients


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper journal closer")
    parser.add_argument("--closer", action="store_true", help="run the closer loop every CLOSER_INTERVAL_MIN")
    parser.add_argument("--once", action="store_true", help="run a single closer pass and exit")
    parser.add_argument("--db", default=journal_config.JOURNAL_DB_DSN)
    parser.add_argument("--run-seconds", type=float, default=None)
    args = parser.parse_args(argv)
    if not (args.closer or args.once):
        parser.error("pass --closer or --once")

    logging.basicConfig(level=logging.INFO)
    # The pollers and the closer must open the SAME file: a relative
    # JOURNAL_DB_DSN resolves against each process's working directory.
    logger.info("paper journal closer: journal db %s", os.path.abspath(args.db))
    jconn = journal_db.connect(args.db)
    clients = build_default_clients()
    # Liveness for health.py: one beat per loop (EXEC_POLL_SEC). The
    # equity log alone is written every 30 min and is wiped by a reset.
    uptime = UptimeTracker(jconn, "closer")
    start = time.monotonic()
    next_close = 0.0
    try:
        while True:
            # A failed pass must never end the closer process.
            try:
                if time.monotonic() >= next_close:
                    run_closer(jconn, clients)
                    next_close = time.monotonic() + journal_config.CLOSER_INTERVAL_MIN * 60
                else:
                    run_exec_check(jconn, clients)
            except Exception:
                logger.exception("paper journal closer pass failed")
            uptime.heartbeat()
            if args.once or (args.run_seconds is not None and time.monotonic() - start >= args.run_seconds):
                break
            time.sleep(journal_config.EXEC_POLL_SEC)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
