"""Supervisor: restart on crash with backoff, owner stop/run without
false crash alerts, health alerts on transitions only, grace after start."""
import sys
import time

import pytest

from gift_sniper import health, supervisor
from gift_sniper.health import Check


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_text(self, text, chat_id=None):
        self.sent.append(text)
        return True


@pytest.fixture
def procs(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "LOG_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "PROCESSES", {
        "crasher": ["-c", "import sys; print('boom line'); sys.exit(3)"],
        "sleeper": ["-c", "import time; time.sleep(60)"],
    })


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _wait_exit(child):
    child.proc.wait(timeout=20)
    time.sleep(0.2)  # let the pump thread read the output


def test_crash_restarts_with_backoff_and_one_alert(procs):
    clock, notifier = Clock(), FakeNotifier()
    sup = supervisor.Supervisor(["crasher"], notifier=notifier, owner_id="1", clock=clock)
    sup.supervise_once()
    child = sup.children["crasher"]
    _wait_exit(child)
    sup.supervise_once()  # records the crash
    assert child.last_exit_code == 3
    assert child.backoff == 20  # doubled from 10: it ran < STABLE_RUN_SEC
    assert len(notifier.sent) == 1 and "crasher упал (код 3" in notifier.sent[0]
    assert "boom line" in notifier.sent[0]
    assert not child.running  # not before the backoff
    clock.t += 21
    sup.supervise_once()
    assert child.restarts == 1
    _wait_exit(child)
    sup.supervise_once()
    assert len(notifier.sent) == 1  # crash loop: one message per window
    sup.shutdown()


def test_owner_stop_and_run_no_crash_alert(procs):
    clock, notifier = Clock(), FakeNotifier()
    sup = supervisor.Supervisor(["sleeper"], notifier=notifier, owner_id="1", clock=clock)
    sup.supervise_once()
    assert sup.children["sleeper"].running
    assert "остановлен" in sup.control("stop", "sleeper")
    sup.supervise_once()
    assert not sup.children["sleeper"].running
    assert "остановлен владельцем" in sup.status_text()
    assert sup.control("start", "sleeper") == "sleeper запущен"
    assert sup.children["sleeper"].running
    assert sup.control("restart", "sleeper") == "sleeper перезапущен"
    assert sup.children["sleeper"].running
    assert notifier.sent == []
    assert "нет процесса" in sup.control("stop", "nope")
    sup.shutdown()


def test_health_alerts_only_on_transitions(procs, monkeypatch):
    clock, notifier = Clock(), FakeNotifier()
    sup = supervisor.Supervisor([], notifier=notifier, owner_id="1", clock=clock)
    state = {"level": health.FAIL}
    monkeypatch.setattr(health, "collect", lambda g, j, online=False: [
        Check("сигналы 24ч", state["level"], "x", "no_signals_24h")])
    sup.check_health_once()
    sup.check_health_once()
    assert len(notifier.sent) == 1 and notifier.sent[0].startswith("⚠️")
    clock.t += supervisor.ALERT_REPEAT_SEC
    sup.check_health_once()
    assert len(notifier.sent) == 2  # reminder
    state["level"] = health.OK
    sup.check_health_once()
    sup.check_health_once()
    assert len(notifier.sent) == 3 and notifier.sent[2].startswith("✅")


def test_checks_of_stopped_or_just_started_process_do_not_alert(procs, monkeypatch):
    monkeypatch.setattr(supervisor, "PROCESSES", {"mrkt": ["-c", "import time; time.sleep(60)"]})
    clock, notifier = Clock(), FakeNotifier()
    sup = supervisor.Supervisor(["mrkt"], notifier=notifier, owner_id="1", clock=clock)
    monkeypatch.setattr(health, "collect", lambda g, j, online=False: [
        Check("процесс mrkt", health.FAIL, "x", "poller_down:mrkt"),
        Check("сбор mrkt", health.FAIL, "x", "collection_stale:mrkt")])
    sup.supervise_once()
    sup.check_health_once()
    assert notifier.sent == []  # just started: grace
    clock.t += (health.HEARTBEAT_STALE_MIN + 1) * 60
    sup.check_health_once()
    assert notifier.sent == ["⚠️ процесс mrkt: x"]  # heartbeat grace over, collection grace not yet
    sup.control("stop", "mrkt")
    clock.t += supervisor.ALERT_REPEAT_SEC
    sup.check_health_once()
    assert len(notifier.sent) == 1  # stopped by owner: silent
    sup.shutdown()


def _jconn_with(rows_uptime=(), equity=()):
    from gift_sniper import journal_db
    j = journal_db.connect(":memory:")
    for m, a, b in rows_uptime:
        j.execute("INSERT INTO journal_uptime VALUES (?, ?, ?)", (m, a, b))
    for ts in equity:
        j.execute("INSERT INTO journal_equity_log VALUES ('base', ?, 0, 0, 0)", (ts,))
    return j


def test_closer_alive_by_heartbeat_after_reset_wiped_equity_log():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 19, 10, 36, tzinfo=timezone.utc)
    j = _jconn_with(rows_uptime=[("closer", "2026-09-19T10:31:00+00:00", "2026-09-19T10:35:50+00:00")])
    assert health.check_closer(j, now).level == health.OK


def test_zero_signals_not_alerted_before_24h_of_observation():
    import sqlite3
    from datetime import datetime, timezone
    now = datetime(2026, 9, 19, 10, 36, tzinfo=timezone.utc)
    g = sqlite3.connect(":memory:")
    g.execute("CREATE TABLE alerts_sent (marketplace, listing_external_id, observed_at, sent_at, status)")
    fresh = _jconn_with(rows_uptime=[("portals", "2026-09-19T10:31:00+00:00", "2026-09-19T10:36:00+00:00")])
    assert health.check_signals(g, fresh, now)[0].level == health.OK
    old = _jconn_with(rows_uptime=[("portals", "2026-09-18T09:00:00+00:00", "2026-09-19T10:36:00+00:00")])
    assert health.check_signals(g, old, now)[0].level == health.FAIL


def test_floor_snapshots_index_exists_after_connect():
    from gift_sniper import db
    conn = db.connect(":memory:")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_floor_snapshots_pair_time" in names


def test_mrkt_token_command_owner_only_writes_file(tmp_path, monkeypatch):
    from gift_sniper import config
    from gift_sniper.notifier import CommandHandler

    class N(FakeNotifier):
        def __init__(self, updates):
            super().__init__()
            self.updates = updates
            self.posts = []

        def get_updates(self, offset=None, timeout=0):
            u, self.updates = self.updates, []
            return u

        def _post(self, method, data):
            self.posts.append((method, data))
            return {}

    path = tmp_path / "t.txt"
    monkeypatch.setattr(config, "MRKT_TOKEN_FILE", str(path))
    monkeypatch.setattr(config, "_mrkt_token_cache", None)
    msg = lambda uid, text, mid: {"update_id": mid, "message": {"message_id": mid, "from": {"id": uid}, "text": text}}
    n = N([msg("9", "/mrkt_token viewer-token-0000000", 1), msg("1", "/mrkt_token 0123456789abcdef-uuid", 2)])
    CommandHandler(n, None, owner_id="1", viewer_ids=["9"]).poll_once()
    assert path.read_text() == "0123456789abcdef-uuid"
    assert n.posts == [("deleteMessage", {"chat_id": "1", "message_id": 2})]
    assert n.sent == ["токен MRKT сохранён, процессы подхватят его сами"]


def test_daily_digest_once_per_day_after_hour(procs, monkeypatch, tmp_path):
    from datetime import datetime, timezone
    from gift_sniper import digest
    calls = []
    monkeypatch.setattr(digest, "write", lambda g, j, now: calls.append(now) or tmp_path / "d.md")
    monkeypatch.setattr(digest, "short_text", lambda g, j, now: "short")
    notifier = FakeNotifier()
    sup = supervisor.Supervisor([], notifier=notifier, owner_id="1")
    early = datetime(2026, 9, 20, supervisor.DIGEST_HOUR_UTC - 1, 59, tzinfo=timezone.utc)
    late = datetime(2026, 9, 20, supervisor.DIGEST_HOUR_UTC, 1, tzinfo=timezone.utc)
    assert sup.maybe_digest(early) is False
    assert sup.maybe_digest(late) is True
    assert sup.maybe_digest(late) is False
    assert notifier.sent == ["short"] and len(calls) == 1


def test_digest_builds_on_empty_databases():
    from gift_sniper import db, digest, journal_db
    text = digest.build(db.connect(":memory:"), journal_db.connect(":memory:"))
    assert "## Sent to the bot, 24 h: 0" in text and "## Journal" in text


def test_digest_works_on_plain_tuple_connection():
    """Live failure 2026-09-19: supervisor passes db.connect() (no
    row_factory); digest read columns by name -> TypeError."""
    import sqlite3
    from gift_sniper import db, digest, journal_db
    g = db.connect(":memory:")
    g.row_factory = None
    g.execute("INSERT INTO alerts_sent VALUES ('tonnel', 'x1', '2099-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00', 'sent')")
    from datetime import datetime, timezone
    text = digest.build(g, journal_db.connect(":memory:"), now=datetime(2099, 1, 1, 12, tzinfo=timezone.utc))
    assert "## Sent to the bot, 24 h: 1" in text


def test_bare_token_message_from_owner_is_accepted(tmp_path, monkeypatch):
    """Live 2026-09-22: the owner sent the token and nothing was saved --
    the message has to split into exactly two words before. A bare token,
    or a token on the next line, must work too."""
    from gift_sniper import config
    from gift_sniper.notifier import CommandHandler

    class N(FakeNotifier):
        def __init__(self, updates):
            super().__init__()
            self.updates = updates
            self.posts = []

        def get_updates(self, offset=None, timeout=0):
            u, self.updates = self.updates, []
            return u

        def _post(self, method, data):
            self.posts.append((method, data))
            return {}

    path = tmp_path / "t.txt"
    monkeypatch.setattr(config, "MRKT_TOKEN_FILE", str(path))
    monkeypatch.setattr(config, "_mrkt_token_cache", None)
    msg = lambda uid, text, mid: {"update_id": mid, "message": {"message_id": mid, "from": {"id": uid}, "text": text}}

    n = N([msg("1", "4c667e31-e667-40ed-a41d-641791998bb9", 1)])          # bare token
    CommandHandler(n, None, owner_id="1").poll_once()
    assert path.read_text() == "4c667e31-e667-40ed-a41d-641791998bb9"

    n = N([msg("1", "/mrkt_token\nabcdef0123456789abcdef", 2)])           # token on the next line
    CommandHandler(n, None, owner_id="1").poll_once()
    assert path.read_text() == "abcdef0123456789abcdef"

    n = N([msg("1", "/mrkt_token", 3)])                                   # nothing to save
    CommandHandler(n, None, owner_id="1").poll_once()
    assert "не вижу токен" in n.sent[0]
    assert path.read_text() == "abcdef0123456789abcdef"  # unchanged

    n = N([msg("9", "4c667e31-e667-40ed-a41d-000000000000", 4)])          # a viewer, not the owner
    CommandHandler(n, None, owner_id="1", viewer_ids=["9"]).poll_once()
    assert path.read_text() == "abcdef0123456789abcdef"


def test_ordinary_message_is_not_mistaken_for_a_token():
    from gift_sniper.notifier import _looks_like_mrkt_token
    assert not _looks_like_mrkt_token("/status")
    assert not _looks_like_mrkt_token("как дела")
    assert not _looks_like_mrkt_token("короткий")
    assert _looks_like_mrkt_token("4c667e31-e667-40ed-a41d-641791998bb9")
