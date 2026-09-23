"""Supervisor: ONE command runs the whole system.

    python -m gift_sniper.supervisor

- starts the three pollers and the journal closer as child processes;
- restarts a child that exits (backoff 10 s doubling to 5 min, reset after
  10 min of stable work) and tells the owner in the bot;
- writes each child's output to logs/<name>.log with rotation;
- every HEALTH_INTERVAL_SEC runs health.collect() and sends the owner a
  message when a check goes bad and again when it recovers;
- serves the bot commands (the pollers are started with
  BOT_COMMANDS_IN_POLLER=0): the usual ones plus /health, /procs and,
  owner only, /run, /stop, /restart <name>.

A process stopped by the owner (/stop) stays stopped until /run, and its
checks never alert.
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config, db, health, journal_config, journal_db

logger = logging.getLogger("gift_sniper.supervisor")

PROCESSES: dict[str, list[str]] = {
    "portals": ["-m", "gift_sniper.poller"],
    "tonnel": ["-m", "gift_sniper.tonnel_poller"],
    "mrkt": ["-m", "gift_sniper.mrkt_poller"],
    "closer": ["-m", "gift_sniper.paper_journal", "--closer"],
}

RESTART_BACKOFF_START_SEC = 10
RESTART_BACKOFF_MAX_SEC = 300
# A child that ran this long before exiting is treated as a fresh failure:
# its backoff starts again from RESTART_BACKOFF_START_SEC.
STABLE_RUN_SEC = 600
# One crash message per process per this window; a crash loop gives one
# message, not one every backoff step.
CRASH_ALERT_WINDOW_SEC = 30 * 60

HEALTH_INTERVAL_SEC = int(os.environ.get("HEALTH_INTERVAL_SEC", "300"))
# A still-bad check is reminded about this often.
ALERT_REPEAT_SEC = 6 * 3600

# MRKT token refresh through the Telegram session (mrkt_auth.py), when one
# is set up. ASSUMPTION: the token's lifetime is unknown ("hours", from the
# owner's experience); refreshing hourly keeps it fresh at the cost of one
# Telegram API call per hour. A failed MRKT check triggers a refresh at
# once, at most every MRKT_REFRESH_MIN_GAP_SEC. Measure the real lifetime
# from the "MRKT token refreshed" / 401 lines in logs/supervisor.log.
MRKT_REFRESH_EVERY_SEC = int(os.environ.get("MRKT_TOKEN_REFRESH_MIN", "60")) * 60
MRKT_REFRESH_MIN_GAP_SEC = 10 * 60

# Daily digest (digest.py): file in reports/ plus a short bot message.
DIGEST_HOUR_UTC = int(os.environ.get("DIGEST_HOUR_UTC", "6"))  # 09:00 Moscow

LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5

# Checks that belong to one process: silenced while the owner keeps it
# stopped, and right after its start (see _in_grace).
_CHECK_OWNER = {
    "poller_down:portals": "portals", "collection_stale:portals": "portals",
    "poller_down:tonnel": "tonnel", "collection_stale:tonnel": "tonnel",
    "poller_down:mrkt": "mrkt", "collection_stale:mrkt": "mrkt", "token:mrkt": "mrkt",
    "closer_down": "closer", "journal_pending_stuck": "closer",
}


class Child:
    def __init__(self, name: str, args: list[str], env: dict[str, str], clock=time.monotonic):
        self.name = name
        self.clock = clock  # the supervisor's clock: one time base for both
        self.args = args
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.wanted = True
        self.started_at: float | None = None
        self.started_wall: datetime | None = None
        self.next_start_at = 0.0
        self.backoff = RESTART_BACKOFF_START_SEC
        self.restarts = 0
        self.last_exit_code: int | None = None
        self.last_crash_alert = 0.0
        self.tail: deque[str] = deque(maxlen=15)
        self.log = logging.getLogger(f"gift_sniper.child.{name}")
        self.log.propagate = False
        if not self.log.handlers:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(LOG_DIR / f"{name}.log", maxBytes=LOG_MAX_BYTES,
                                          backupCount=LOG_BACKUPS, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.log.addHandler(handler)
            self.log.setLevel(logging.INFO)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, *self.args], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.started_at = self.clock()
        self.started_wall = datetime.now(timezone.utc)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True, name=f"pump-{self.name}").start()
        self.log.info("=== supervisor: started pid %s at %s", self.proc.pid, self.started_wall.isoformat())

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            line = line.rstrip("\n")
            self.tail.append(line)
            self.log.info(line)

    def stop(self, timeout: float = 15) -> None:
        # A stop asked for is never reported as a crash.
        self.started_at = None
        if not self.running:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.log.info("=== supervisor: stopped at %s", datetime.now(timezone.utc).isoformat())


class Supervisor:
    def __init__(self, names: list[str], notifier=None, owner_id: str | None = None,
                 gconn=None, jconn=None, clock=time.monotonic):
        env = dict(os.environ)
        env["BOT_COMMANDS_IN_POLLER"] = "0"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.children = {n: Child(n, PROCESSES[n], env, clock) for n in names}
        self.notifier = notifier
        self.owner_id = owner_id
        self.gconn = gconn
        self.jconn = jconn
        self.clock = clock
        self.alert_state: dict[str, tuple[str, float]] = {}  # key -> (level, last alert time)
        self.mrkt_refreshed_at: float | None = None
        self.mrkt_refresh_failed_at: float | None = None

    # --- messages -------------------------------------------------------
    def notify_owner(self, text: str) -> None:
        logger.warning("owner alert: %s", text.replace("\n", " | "))
        if self.notifier is None:
            return
        from html import escape
        try:
            self.notifier.send_text(escape(text), chat_id=self.owner_id)
        except Exception:
            logger.exception("owner alert could not be sent")

    # --- process control (also used by the bot) -------------------------
    def supervise_once(self) -> None:
        now = self.clock()
        for child in self.children.values():
            if child.running or not child.wanted:
                continue
            if child.proc is not None and child.started_at is not None:
                # It exited on its own: record it once, schedule a restart.
                code = child.proc.returncode
                ran = now - child.started_at
                child.last_exit_code = code
                child.backoff = RESTART_BACKOFF_START_SEC if ran >= STABLE_RUN_SEC else min(
                    child.backoff * 2, RESTART_BACKOFF_MAX_SEC)
                child.next_start_at = now + child.backoff
                child.started_at = None
                logger.error("%s exited with code %s after %.0f s; restart in %d s", child.name, code, ran, child.backoff)
                if now - child.last_crash_alert >= CRASH_ALERT_WINDOW_SEC or child.last_crash_alert == 0:
                    child.last_crash_alert = now
                    tail = "\n".join(list(child.tail)[-5:])
                    self.notify_owner(
                        f"Процесс {child.name} упал (код {code}, проработал {int(ran // 60)} мин). "
                        f"Перезапуск через {child.backoff} с.\nПоследние строки лога:\n{tail}")
            if now >= child.next_start_at:
                if child.proc is not None:
                    child.restarts += 1
                child.start()

    def control(self, action: str, name: str) -> str:
        if name not in self.children:
            return f"нет процесса «{name}». Есть: {', '.join(self.children)}"
        child = self.children[name]
        if action == "stop":
            child.wanted = False
            child.stop()
            return f"{name} остановлен. Запустить: /run {name}"
        if action == "start":
            if child.running:
                return f"{name} уже работает"
            child.wanted = True
            child.next_start_at = 0
            child.backoff = RESTART_BACKOFF_START_SEC
            self.supervise_once()
            return f"{name} запущен"
        if action == "restart":
            child.wanted = True
            child.stop()
            child.next_start_at = 0
            self.supervise_once()
            return f"{name} перезапущен"
        return f"неизвестное действие {action}"

    def status_text(self) -> str:
        lines = []
        now = self.clock()
        for child in self.children.values():
            if child.running:
                minutes = int((now - child.started_at) // 60) if child.started_at else 0
                state = f"работает {minutes // 60} ч {minutes % 60} мин, pid {child.proc.pid}"
            elif not child.wanted:
                state = "остановлен владельцем"
            else:
                state = f"ждёт перезапуска (через {max(0, int(child.next_start_at - now))} с)"
            lines.append(f"{child.name}: {state}, перезапусков {child.restarts}")
        return "\n".join(lines)

    # --- health alerts ---------------------------------------------------
    def _in_grace(self, key: str) -> bool:
        """A check tied to a process says nothing until that process has
        run long enough to satisfy it (heartbeat, first closer pass, or the
        collection threshold of its marketplace)."""
        name = _CHECK_OWNER.get(key)
        if name is None or name not in self.children:
            return False
        child = self.children[name]
        if not child.wanted:
            return True
        if not child.running or child.started_at is None:
            return False
        ran_min = (self.clock() - child.started_at) / 60
        if key.startswith("collection_stale:"):
            return ran_min < health.COLLECTION_STALE_MIN[name]
        if key == "closer_down":
            return ran_min < 2
        return ran_min < health.HEARTBEAT_STALE_MIN

    def check_health_once(self) -> list[health.Check]:
        checks = health.collect(self.gconn, self.jconn, online=True)
        now = self.clock()
        for c in checks:
            key = c.key or c.name
            if self._in_grace(key):
                continue
            prev_level, last_alert = self.alert_state.get(key, (health.OK, 0.0))
            if c.level != health.OK:
                if prev_level == health.OK or now - last_alert >= ALERT_REPEAT_SEC:
                    self.notify_owner(f"⚠️ {c.name}: {c.text}")
                    self.alert_state[key] = (c.level, now)
                else:
                    self.alert_state[key] = (c.level, last_alert)
            elif prev_level != health.OK:
                self.notify_owner(f"✅ восстановлено — {c.name}: {c.text}")
                self.alert_state[key] = (health.OK, now)
        return checks

    # --- MRKT token --------------------------------------------------------
    def refresh_mrkt_token(self, reason: str) -> bool:
        from . import mrkt_auth
        if not mrkt_auth.session_configured():
            return False
        now = self.clock()
        last = max(self.mrkt_refreshed_at or 0, self.mrkt_refresh_failed_at or 0)
        if last and now - last < MRKT_REFRESH_MIN_GAP_SEC:
            return False
        # A subprocess: Telethon's event loop never lives in the supervisor.
        result = subprocess.run([sys.executable, "-m", "gift_sniper.mrkt_auth", "--refresh"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        if result.returncode == 0:
            self.mrkt_refreshed_at = now
            logger.info("MRKT token refreshed (%s)", reason)
            return True
        self.mrkt_refresh_failed_at = now
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        self.notify_owner("Не удалось обновить токен MRKT:\n" + "\n".join(tail))
        return False

    def maybe_refresh_mrkt(self, checks: list[health.Check] | None = None) -> None:
        now = self.clock()
        mrkt_bad = any(c.key == "token:mrkt" and c.level != health.OK for c in checks or [])
        if mrkt_bad:
            self.refresh_mrkt_token("MRKT check failed")
        elif self.mrkt_refreshed_at is None or now - self.mrkt_refreshed_at >= MRKT_REFRESH_EVERY_SEC:
            self.refresh_mrkt_token("scheduled")

    def maybe_digest(self, now_utc: datetime | None = None) -> bool:
        """Once a day at DIGEST_HOUR_UTC."""
        from . import digest
        now_utc = now_utc or datetime.now(timezone.utc)
        today = now_utc.date()
        if now_utc.hour < DIGEST_HOUR_UTC or getattr(self, "_digest_day", None) == today:
            return False
        path = digest.write(self.gconn, self.jconn, now_utc)
        self._digest_day = today  # only after success: a failed digest is retried next tick
        logger.info("daily digest written: %s", path)
        if self.notifier is not None:
            try:
                # magazine_text is already HTML-escaped: send as is.
                self.notifier.send_text(digest.short_text(self.gconn, self.jconn, now_utc), chat_id=self.owner_id)
            except Exception:
                logger.exception("daily digest could not be sent")
        return True

    def shutdown(self) -> None:
        for child in self.children.values():
            child.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gift Sniper supervisor")
    parser.add_argument("--only", default=os.environ.get("SUPERVISE", ",".join(PROCESSES)),
                        help="comma-separated process names (default: all)")
    parser.add_argument("--run-seconds", type=float, default=None, help="stop everything after this many seconds")
    args = parser.parse_args(argv)
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    unknown = [n for n in names if n not in PROCESSES]
    if unknown:
        parser.error(f"unknown process(es): {unknown}; known: {list(PROCESSES)}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  RotatingFileHandler(LOG_DIR / "supervisor.log", maxBytes=LOG_MAX_BYTES,
                                      backupCount=LOG_BACKUPS, encoding="utf-8")],
    )
    if not journal_config.PAPER_JOURNAL_ENABLED:
        logger.warning("PAPER_JOURNAL_ENABLED is off: pollers write no heartbeat, health checks will fail")

    gconn = db.connect(config.DB_DSN)
    jconn = journal_db.connect(journal_config.JOURNAL_DB_DSN)

    notifier = command_handler = None
    owner_id = None
    if config.NOTIFY_ENABLED:
        from .notifier import CommandHandler, TelegramNotifier
        owner_id = config.get_telegram_owner_id()
        viewer_ids = config.get_telegram_viewer_ids()
        notifier = TelegramNotifier(config.get_telegram_bot_token(), chat_id=owner_id, viewer_chat_ids=viewer_ids)
    sup = Supervisor(names, notifier=notifier, owner_id=owner_id, gconn=gconn, jconn=jconn)
    if notifier is not None:
        command_handler = CommandHandler(notifier, gconn, owner_id=owner_id, viewer_ids=viewer_ids,
                                         journal_conn=jconn, process_control=sup)
    logger.info("supervisor: processes %s, logs in %s", names, LOG_DIR.resolve())
    sup.notify_owner(f"Система запущена: {', '.join(names)}")
    if "mrkt" in names:
        sup.maybe_refresh_mrkt()

    next_health = time.monotonic() + HEALTH_INTERVAL_SEC
    started = time.monotonic()
    try:
        while args.run_seconds is None or time.monotonic() - started < args.run_seconds:
            try:
                sup.supervise_once()
                if command_handler is not None:
                    command_handler.poll_once()
                if time.monotonic() >= next_health:
                    next_health = time.monotonic() + HEALTH_INTERVAL_SEC
                    checks = sup.check_health_once()
                    sup.maybe_digest()
                    if "mrkt" in names:
                        sup.maybe_refresh_mrkt(checks)
            except Exception:
                logger.exception("supervisor loop error")  # the supervisor itself never dies on one error
            time.sleep(2)
    except KeyboardInterrupt:
        logger.info("supervisor: stopping children")
    finally:
        sup.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
