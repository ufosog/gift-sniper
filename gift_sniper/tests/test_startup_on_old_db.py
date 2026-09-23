import os
import sqlite3
import tempfile

from gift_sniper import db
from gift_sniper.poller import build_default_poller


def test_build_default_poller_works_on_fresh_db_file():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # let db.connect create it fresh
    poller = None
    try:
        poller = build_default_poller(dsn=path)
        assert poller is not None
    finally:
        if poller is not None:
            poller.conn.close()
        if os.path.exists(path):
            os.remove(path)


def test_build_default_poller_migrates_and_starts_on_old_schema_db_file():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    poller = None
    verify_conn = None
    try:
        # Build a DB file with the OLD (pre-rename) floor_snapshots schema,
        # simulating one created by a previous version of this code.
        raw_conn = sqlite3.connect(path)
        raw_conn.execute(
            """
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL,
                external_id TEXT NOT NULL,
                tg_id TEXT,
                collection_id TEXT,
                collection_name TEXT,
                gift_number INTEGER,
                price_nano INTEGER,
                currency TEXT NOT NULL,
                collection_floor_nano INTEGER,
                model_name TEXT,
                symbol_name TEXT,
                backdrop_name TEXT,
                model_rarity_raw TEXT,
                symbol_rarity_raw TEXT,
                backdrop_rarity_raw TEXT,
                image_url TEXT,
                animation_url TEXT,
                listed_at TEXT,
                unlocks_at TEXT,
                status TEXT,
                first_seen_at TEXT NOT NULL,
                raw TEXT,
                UNIQUE(marketplace, external_id)
            )
            """
        )
        raw_conn.execute(
            """
            CREATE TABLE floor_snapshots (
                listing_external_id TEXT PRIMARY KEY,
                model_name TEXT NOT NULL,
                backdrop_name TEXT,
                combo_floor_nano INTEGER,
                model_min_floor_nano INTEGER,
                floor_fetched_at TEXT NOT NULL,
                floor_age_sec INTEGER NOT NULL,
                raw_model_block TEXT NOT NULL,
                name_collision INTEGER NOT NULL DEFAULT 0,
                floor_skip_reason TEXT
            )
            """
        )
        raw_conn.commit()
        raw_conn.close()

        # This must NOT raise sqlite3.OperationalError -- it must migrate
        # the DB in place and start cleanly.
        poller = build_default_poller(dsn=path)
        assert poller is not None

        verify_conn = sqlite3.connect(path)
        db.verify_schema(verify_conn)  # must pass post-migration
    finally:
        if poller is not None:
            poller.conn.close()
        if verify_conn is not None:
            verify_conn.close()
        if os.path.exists(path):
            os.remove(path)
