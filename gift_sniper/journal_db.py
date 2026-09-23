"""journal.db -- the paper journal's own database, separate from
gift_sniper.db so experiments never touch working data and the journal
can be reset by deleting one file. Own schema_version, no shared
migrations with db.py.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import journal_config

CURRENT_SCHEMA_VERSION = 4

# v2: per-position current valuation written by the closer, shown by
# /magazine ("куплен X, сейчас Y").
# v3: what the execution check saw and when -- needed to tell a real
# snipe from a late check or a reprice.
_ADDED_COLUMNS = {
    "mark_nano": "INTEGER", "mark_at": "TEXT",
    "exec_checked_at": "TEXT", "exec_state": "TEXT", "exec_price_nano": "INTEGER",
    # v4: cross-market cap on the exit price at close (see paper_journal._cross_market_cap)
    "exit_cap_nano": "INTEGER",
}

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

-- Append-only: one row per (signal, scenario). Rejected signals are
-- written exactly like accepted ones.
CREATE TABLE IF NOT EXISTS journal_signals (
    signal_id TEXT NOT NULL,
    scenario TEXT NOT NULL,
    ts TEXT NOT NULL,
    marketplace TEXT NOT NULL,
    listing_external_id TEXT NOT NULL,
    tg_id TEXT,
    collection TEXT,
    -- Not in the original spec: Portals' floor query at close needs the
    -- collection_id, the name alone is not enough.
    collection_id TEXT,
    model TEXT,
    backdrop TEXT,
    gift_number INTEGER,
    price_nano INTEGER NOT NULL,
    floor_nano INTEGER NOT NULL,
    floor_level TEXT,
    floor_depth INTEGER,
    floor_source TEXT,
    cross_verdict TEXT,
    ratio NUMERIC,
    realization_rate NUMERIC,
    fee_buy_pct NUMERIC,
    fee_sell_pct NUMERIC,
    network_fee_nano INTEGER,
    expected_exit_nano INTEGER,
    expected_pnl_nano INTEGER,
    status TEXT NOT NULL,  -- PENDING_EXEC | OPEN | REJECTED | CLOSED | UNSOLD
    reject_reason TEXT,
    position_size_nano INTEGER,
    ts_open TEXT,
    ts_close TEXT,
    exit_price_nano INTEGER,
    pnl_nano INTEGER,
    pnl_pct NUMERIC,
    floor_at_close_nano INTEGER,
    depth_at_close INTEGER,
    sold_flag INTEGER,
    mark_nano INTEGER,
    mark_at TEXT,
    exec_checked_at TEXT,
    exec_state TEXT,  -- listed | repriced_down | repriced_up | gone | not_for_sale
    exec_price_nano INTEGER,
    exit_cap_nano INTEGER,
    PRIMARY KEY (signal_id, scenario)
);
CREATE INDEX IF NOT EXISTS idx_journal_signals_status ON journal_signals(scenario, status);

CREATE TABLE IF NOT EXISTS journal_state (
    scenario TEXT PRIMARY KEY,
    balance_nano INTEGER NOT NULL,
    withdrawn_nano INTEGER NOT NULL,
    equity_nano INTEGER NOT NULL,
    open_positions INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_uptime (
    marketplace TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL
);

-- Not in the original spec: max drawdown needs an equity series, a single
-- current value in journal_state cannot give it.
CREATE TABLE IF NOT EXISTS journal_equity_log (
    scenario TEXT NOT NULL,
    ts TEXT NOT NULL,
    balance_nano INTEGER NOT NULL,
    withdrawn_nano INTEGER NOT NULL,
    equity_nano INTEGER NOT NULL
);
"""


class JournalSchemaError(RuntimeError):
    pass


def connect(dsn: str = journal_config.JOURNAL_DB_DSN) -> sqlite3.Connection:
    # Three pollers and the closer write concurrently: WAL + busy timeout.
    conn = sqlite3.connect(dsn, timeout=30)
    conn.row_factory = sqlite3.Row
    if dsn != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    with conn:
        conn.executescript(_DDL)
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        now = datetime.now(timezone.utc).isoformat()
        if version is not None and version > CURRENT_SCHEMA_VERSION:
            raise JournalSchemaError(f"journal.db schema_version={version}, expected {CURRENT_SCHEMA_VERSION}")
        existing = {row[1] for row in conn.execute("PRAGMA table_info(journal_signals)")}
        for column, sql_type in _ADDED_COLUMNS.items():
            if column not in existing:  # older database: add the column, keep every row
                conn.execute(f"ALTER TABLE journal_signals ADD COLUMN {column} {sql_type}")
        if version != CURRENT_SCHEMA_VERSION:
            conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (CURRENT_SCHEMA_VERSION, now))
        _init_state(conn, now)
    return conn


def _init_state(conn: sqlite3.Connection, now_iso: str) -> None:
    for scenario in journal_config.SCENARIOS:
        conn.execute(
            "INSERT OR IGNORE INTO journal_state "
            "(scenario, balance_nano, withdrawn_nano, equity_nano, open_positions, updated_at) "
            "VALUES (?, ?, 0, ?, 0, ?)",
            (scenario, journal_config.limits(scenario)[0], journal_config.limits(scenario)[0], now_iso),
        )


def reset(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    """Starts a new observation period: every signal, uptime row and
    equity point is deleted, every scenario goes back to START_BALANCE.
    Running pollers re-create their uptime row on the next heartbeat.
    """
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    with conn:
        conn.execute("DELETE FROM journal_signals")
        conn.execute("DELETE FROM journal_uptime")
        conn.execute("DELETE FROM journal_equity_log")
        conn.execute("DELETE FROM journal_state")
        _init_state(conn, now_iso)
