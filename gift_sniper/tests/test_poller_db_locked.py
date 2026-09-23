"""Правка 4 (two-writer SQLite contention): 'database is locked' must
not crash poller.py's run loop -- counted in db_locked_count, logged,
and the run loop continues. See tonnel_poller.py's matching tests.
"""
import sqlite3

import pytest

from gift_sniper import db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def _poller(monkeypatch):
    monkeypatch.setattr("gift_sniper.poller.time.sleep", lambda s: None)
    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[[], [], []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    return Poller(conn, client, auth, floor_cache)


def test_db_locked_error_counted_and_does_not_crash_run_forever(monkeypatch):
    poller = _poller(monkeypatch)

    calls = {"n": 0}
    real_poll_once = poller.poll_once

    def flaky_poll_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_poll_once()

    poller.poll_once = flaky_poll_once
    poller.run_forever(run_seconds=0.05)

    assert poller.stats["db_locked_count"] == 1


def test_other_operational_errors_are_not_mistaken_for_locking(monkeypatch):
    poller = _poller(monkeypatch)

    def broken_poll_once():
        raise sqlite3.OperationalError("no such table: bogus")

    poller.poll_once = broken_poll_once
    with pytest.raises(sqlite3.OperationalError):
        poller.run_forever(run_seconds=0.05)
    assert poller.stats["db_locked_count"] == 0
