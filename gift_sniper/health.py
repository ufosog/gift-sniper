"""System health: one set of checks shared by the CLI (`python health.py`),
the bot's /health command and the pollers' self-check alerts.

Read-only on both databases. `--online` adds one cheap request per
marketplace to test the tokens.

Every threshold below states where it comes from. Measured values were
taken from gift_sniper.db / journal.db on 2026-09-19 (diag/collect_gaps.py).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote

from . import journal_config

OK, WARN, FAIL = "OK", "WARN", "FAIL"
MARKETPLACES = ("portals", "tonnel", "mrkt")

# Poller heartbeat (journal_uptime.ended_at) moves on every poll cycle
# (poll interval 8-15 s). ASSUMPTION: a normal cycle is under a minute;
# 5 minutes means several missed cycles. Not measured: ended_at keeps only
# the last beat.
HEARTBEAT_STALE_MIN = 5

# No newly seen listing for this long, while the poller is alive, is a
# collection problem. Measured max gap between new listings inside
# continuous runs: portals 1289 s, tonnel 2851 s, mrkt 344 s (p99 792 /
# 1051 / 127 s). Threshold = max x 1.5, rounded up.
COLLECTION_STALE_MIN = {"portals": 33, "tonnel": 72, "mrkt": 9}

# The closer writes journal_equity_log on every full pass
# (CLOSER_INTERVAL_MIN). Two missed passes plus 5 minutes = stopped.
CLOSER_STALE_MIN = 2 * journal_config.CLOSER_INTERVAL_MIN + 5

# Owner's alert rules (product decisions, not measurements).
SNIPED_SHARE_ALERT = 0.90
SNIPED_MIN_SAMPLE = 5  # below this a share says nothing
NO_SLOT_SHARE_ALERT = 0.50  # journal "clogged": most signals turned away for lack of a slot


@dataclass(frozen=True)
class Check:
    name: str
    level: str
    text: str
    key: str = ""  # stable id for alert de-duplication


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ago(now: datetime, then: datetime | None) -> str:
    if then is None:
        return "никогда"
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 48 * 60:
        return f"{minutes // 60} ч {minutes % 60} мин назад"
    return f"{minutes // 1440} дн назад"


def _minutes(now: datetime, then: datetime | None) -> float:
    return float("inf") if then is None else (now - then).total_seconds() / 60


# --- checks ---------------------------------------------------------------

def check_pollers(jconn: sqlite3.Connection, now: datetime) -> list[Check]:
    out = []
    for m in MARKETPLACES:
        last = _dt(jconn.execute("SELECT MAX(ended_at) FROM journal_uptime WHERE marketplace = ?", (m,)).fetchone()[0])
        alive = _minutes(now, last) <= HEARTBEAT_STALE_MIN
        out.append(Check(f"процесс {m}", OK if alive else FAIL,
                         f"пульс {_ago(now, last)}" + ("" if alive else " — процесс не работает"), f"poller_down:{m}"))
    return out


def check_closer(jconn: sqlite3.Connection, now: datetime) -> Check:
    """Heartbeat every loop (15 s). The equity log is only the fallback for
    a closer older than this heartbeat: it is written every 30 min and a
    journal reset wipes it (false alarm 2026-09-19 12:36)."""
    beat = _dt(jconn.execute("SELECT MAX(ended_at) FROM journal_uptime WHERE marketplace = 'closer'").fetchone()[0])
    if beat is not None:
        alive = _minutes(now, beat) <= HEARTBEAT_STALE_MIN
        text = f"пульс {_ago(now, beat)}"
    else:
        last = _dt(jconn.execute("SELECT MAX(ts) FROM journal_equity_log").fetchone()[0])
        alive = _minutes(now, last) <= CLOSER_STALE_MIN
        text = f"последний проход {_ago(now, last)}"
    return Check("процесс closer", OK if alive else FAIL,
                 text + ("" if alive else " — журнал не закрывает сделки"), "closer_down")


def check_collection(gconn: sqlite3.Connection, now: datetime) -> list[Check]:
    out = []
    for m in MARKETPLACES:
        last = _dt(gconn.execute(
            "SELECT MAX(first_seen_at) FROM listing_lifecycle WHERE marketplace = ?", (m,)).fetchone()[0])
        fresh = _minutes(now, last) <= COLLECTION_STALE_MIN[m]
        out.append(Check(f"сбор {m}", OK if fresh else FAIL,
                         f"новый лот {_ago(now, last)}", f"collection_stale:{m}"))
    return out


def check_signals(gconn: sqlite3.Connection, jconn: sqlite3.Connection, now: datetime) -> list[Check]:
    since = (now - timedelta(hours=24)).isoformat()
    sent = dict(gconn.execute(
        "SELECT marketplace, COUNT(*) FROM alerts_sent WHERE status = 'sent' AND sent_at >= ? GROUP BY 1",
        (since,)).fetchall())
    journal = dict(jconn.execute(
        "SELECT marketplace, COUNT(DISTINCT signal_id) FROM journal_signals WHERE ts >= ? GROUP BY 1",
        (since,)).fetchall())
    total_journal = sum(journal.values())
    parts = ", ".join(f"{m} {journal.get(m, 0)}/{sent.get(m, 0)}" for m in MARKETPLACES)
    # "Zero in 24 h" means something only after 24 h of observation: right
    # after a start or a journal reset it is always zero (false alarm
    # 2026-09-19 12:36).
    first = _dt(jconn.execute("SELECT MIN(started_at) FROM journal_uptime WHERE marketplace != 'closer'").fetchone()[0])
    observed_h = 0.0 if first is None else (now - first).total_seconds() / 3600
    if total_journal or observed_h < 24:
        note = "" if observed_h >= 24 else f" (наблюдение {observed_h:.1f} ч из 24)"
        return [Check("сигналы 24ч", OK, f"чистых/отправлено: {parts}{note}", "no_signals_24h")]
    return [Check("сигналы 24ч", FAIL, f"чистых/отправлено: {parts} — ноль сигналов за сутки", "no_signals_24h")]


def check_journal(jconn: sqlite3.Connection, now: datetime) -> list[Check]:
    out = []
    since = (now - timedelta(hours=24)).isoformat()
    pending_old = jconn.execute(
        "SELECT COUNT(DISTINCT signal_id), MIN(ts) FROM journal_signals WHERE status = 'PENDING_EXEC' AND ts < ?",
        ((now - timedelta(seconds=journal_config.EXEC_MAX_AGE_SEC * 2)).isoformat(),)).fetchone()
    if pending_old[0]:
        out.append(Check("журнал: зависшие", FAIL,
                         f"{pending_old[0]} сигналов ждут проверки с {pending_old[1][:16]} — closer не работает?",
                         "journal_pending_stuck"))
    else:
        out.append(Check("журнал: зависшие", OK, "нет", "journal_pending_stuck"))

    # health opens journal.db read-only and never migrates it: before the
    # closer's first v3 start the exec_state column does not exist yet.
    columns = {r[1] for r in jconn.execute("PRAGMA table_info(journal_signals)")}
    exec_col = "exec_state" if "exec_state" in columns else "NULL AS exec_state"
    base = jconn.execute(
        f"SELECT reject_reason, {exec_col}, status FROM journal_signals WHERE scenario = 'base' AND ts >= ?",
        (since,)).fetchall()
    n = len(base)
    no_slot = sum(1 for r in base if r["reject_reason"] == "no_slot")
    if n and no_slot / n > NO_SLOT_SHARE_ALERT:
        out.append(Check("журнал: слоты", WARN, f"no_slot {no_slot} из {n} за 24ч — журнал забит", "journal_clogged"))
    else:
        out.append(Check("журнал: слоты", OK, f"no_slot {no_slot} из {n} за 24ч", "journal_clogged"))

    checked = [r for r in base if r["exec_state"] is not None]
    sniped = sum(1 for r in checked if r["reject_reason"] == "sniped")
    if len(checked) >= SNIPED_MIN_SAMPLE and sniped / len(checked) > SNIPED_SHARE_ALERT:
        out.append(Check("журнал: sniped", WARN, f"{sniped} из {len(checked)} проверенных за 24ч уведены",
                         "sniped_high"))
    else:
        note = "" if len(checked) >= SNIPED_MIN_SAMPLE else " (мало данных)"
        out.append(Check("журнал: sniped", OK, f"{sniped} из {len(checked)} проверенных за 24ч{note}", "sniped_high"))

    state = {r["scenario"]: r for r in jconn.execute("SELECT * FROM journal_state")}
    if "base" in state:
        s = state["base"]
        out.append(Check("журнал: банк base", OK,
                         f"свободно {s['balance_nano'] / 1e9:.2f}, оценка {s['equity_nano'] / 1e9:.2f}, "
                         f"позиций {s['open_positions']}", "journal_bank"))
    return out


def portals_token_age_hours(token: str | None, now: datetime) -> float | None:
    """authData is Telegram initData: its auth_date is when the mini-app
    issued it. Offline, no request."""
    if not token:
        return None
    raw = token[4:] if token.lower().startswith("tma ") else token
    try:
        auth_date = int(parse_qs(unquote(raw) if "auth_date%3D" in raw else raw)["auth_date"][0])
    except (KeyError, ValueError, IndexError):
        return None
    return (now - datetime.fromtimestamp(auth_date, tz=timezone.utc)).total_seconds() / 3600


def check_tokens_offline(now: datetime) -> list[Check]:
    from . import config
    out = []
    # Portals needs no token for reading (config.get_portals_auth).
    out.append(Check("токен portals", OK, "не нужен (анонимный доступ)", "token:portals"))
    mrkt = config.get_mrkt_access_token()
    out.append(Check("токен mrkt", OK if mrkt else FAIL, "задан" if mrkt else "MRKT_ACCESS_TOKEN не задан",
                     "token:mrkt"))
    return out


def check_tokens_online() -> list[Check]:
    """One cheap authorized request per marketplace."""
    out = []
    try:
        from .portals_client import PortalsClient
        # Anonymous on purpose: this is what the pollers rely on.
        PortalsClient(auth_provider=lambda: "").search_by_ids(["00000000-0000-0000-0000-000000000000"])
        out.append(Check("доступ portals (сеть)", OK, "анонимный запрос прошёл", "token:portals"))
    except Exception as exc:
        out.append(Check("доступ portals (сеть)", FAIL, f"ошибка: {type(exc).__name__}: {str(exc)[:120]}",
                         "token:portals"))
    try:
        from .mrkt_client import build_default_mrkt_client
        client = build_default_mrkt_client()
        if client is None:
            raise RuntimeError("клиент не создан (нет токена или MRKT выключен)")
        client.feed(1, "")
        out.append(Check("токен mrkt (сеть)", OK, "запрос прошёл", "token:mrkt"))
    except Exception as exc:
        out.append(Check("токен mrkt (сеть)", FAIL,
                         f"ошибка: {type(exc).__name__}: {str(exc)[:80]} — пришлите боту /mrkt_token НОВЫЙ_ТОКЕН",
                         "token:mrkt"))
    return out


def collect(gconn: sqlite3.Connection, jconn: sqlite3.Connection, now: datetime | None = None,
            online: bool = False) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    checks: list[Check] = []
    checks += check_pollers(jconn, now)
    checks.append(check_closer(jconn, now))
    checks += check_collection(gconn, now)
    checks += check_signals(gconn, jconn, now)
    checks += check_journal(jconn, now)
    checks += check_tokens_online() if online else check_tokens_offline(now)
    return checks


def format_report(checks: list[Check]) -> str:
    mark = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
    bad = [c for c in checks if c.level != OK]
    head = "Всё в порядке" if not bad else f"Проблем: {len(bad)}"
    lines = [head, ""] + [f"{mark[c.level]} {c.name}: {c.text}" for c in checks]
    return "\n".join(lines)


def open_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    from . import config
    parser = argparse.ArgumentParser(description="Gift Sniper health report")
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--journal", default=journal_config.JOURNAL_DB_DSN)
    parser.add_argument("--online", action="store_true", help="test tokens with one real request each")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    checks = collect(open_readonly(args.db), open_readonly(args.journal), online=args.online)
    print(format_report(checks))
    return 1 if any(c.level == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
