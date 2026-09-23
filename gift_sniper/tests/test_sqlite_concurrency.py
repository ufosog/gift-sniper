"""Two-writer SQLite contention (poller.py/Portals + tonnel_poller.py/
Tonnel share one DB file) -- КАК ТЕСТИРОВАТЬ items 1-3 for the WAL/
busy_timeout delivery. See README and db.py's connect().
"""
import os
import sqlite3
import tempfile
import threading
import time

from gift_sniper import config, db


def test_connect_enables_wal_mode():
    """КАК ТЕСТИРОВАТЬ item 1: after db.connect(), PRAGMA journal_mode
    reports 'wal'. Uses a real file -- WAL is silently ignored for
    :memory: databases (no separate -wal file possible), so this can't be
    checked against the in-memory DSN the rest of the test suite uses.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = db.connect(path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        conn.close()
    finally:
        os.remove(path)
        for suffix in ("-wal", "-shm"):
            extra = path + suffix
            if os.path.exists(extra):
                os.remove(extra)


def test_connect_sets_busy_timeout_from_config(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 2: busy_timeout is set and reads from
    config.SQLITE_BUSY_TIMEOUT_MS (env-overridable, default 15000)."""
    monkeypatch.setattr(config, "SQLITE_BUSY_TIMEOUT_MS", 7777)
    conn = db.connect(":memory:")
    timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms == 7777


def test_default_busy_timeout_is_15000():
    assert config.SQLITE_BUSY_TIMEOUT_MS == 15000
    conn = db.connect(":memory:")
    timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout_ms == 15000


def test_second_writer_waits_instead_of_failing_immediately():
    """КАК ТЕСТИРОВАТЬ item 3: one connection holds a write transaction
    open, a second connection's write against the SAME on-disk DB waits
    (thanks to busy_timeout) and succeeds once the first commits, rather
    than raising 'database is locked' immediately.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn_a = db.connect(path)

        conn_a.execute("BEGIN IMMEDIATE")
        conn_a.execute("INSERT INTO schema_version (version, applied_at) VALUES (999, 'x')")

        result = {}

        def writer_b():
            # Connects inside the thread -- sqlite3 connections are only
            # usable from the thread that created them by default.
            conn_b = db.connect(path)
            try:
                conn_b.execute("INSERT INTO schema_version (version, applied_at) VALUES (998, 'y')")
                conn_b.commit()
                result["ok"] = True
            except sqlite3.OperationalError as exc:
                result["ok"] = False
                result["error"] = str(exc)
            finally:
                conn_b.close()

        t = threading.Thread(target=writer_b)
        t.start()
        time.sleep(0.3)  # let writer_b actually start blocking on the lock
        conn_a.commit()  # releases the lock -- writer_b's busy_timeout wait should now succeed
        t.join(timeout=5)

        assert result.get("ok") is True, result.get("error")
        conn_a.close()
    finally:
        os.remove(path)
        for suffix in ("-wal", "-shm"):
            extra = path + suffix
            if os.path.exists(extra):
                os.remove(extra)


def test_no_conn_transaction_wraps_a_network_call():
    """КАК ТЕСТИРОВАТЬ item 4: audits poller.py and tonnel_poller.py for
    a manually-opened `with conn:` (or raw BEGIN) block spanning a
    network call -- neither file should manage transactions directly at
    all; every write goes through a db.py helper that wraps its own
    short, self-contained `with conn:` immediately around the SQL only.
    """
    import inspect
    from gift_sniper import poller, tonnel_poller

    for module in (poller, tonnel_poller):
        source = inspect.getsource(module)
        assert "with conn:" not in source and "with self.conn:" not in source
        assert "BEGIN" not in source
