"""Daily digest: what the system did in the last 24 h, in one file.

    python -m gift_sniper.digest            # writes reports/digest-<date>.md, prints it

Sections: health; every signal sent to the bot (all marketplaces) with
price, floor, ratio and cross-check verdict; the signals with the highest
floor/price ratio for manual review (the 2026-09-19 Bonded Ring defect had
ratio 3.7: a lone expensive lot set the floor); the full journal report.

The file is the architect's review input: the supervisor writes it every
day, so a review needs only the reports/ folder, not access to the bot.
Read-only on both databases.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, health, journal_config

REPORT_DIR = Path("reports")
REVIEW_TOP = 5


def _sent_rows(gconn, since_iso: str) -> list[sqlite3.Row]:
    # Own cursor with Row: the caller's connection may return tuples
    # (supervisor passes db.connect(), which sets no row_factory).
    cur = gconn.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(
        """
        SELECT a.marketplace, a.listing_external_id, a.sent_at, l.collection_name, l.model_name,
               l.backdrop_name, l.gift_number, p.new_price_nano, p.floor_at_drop_nano,
               p.floor_listed_count_at_drop, p.floor_level_at_drop
        FROM alerts_sent a
        LEFT JOIN listings l ON l.marketplace = a.marketplace AND l.external_id = a.listing_external_id
        LEFT JOIN price_history p ON p.marketplace = a.marketplace
             AND p.listing_external_id = a.listing_external_id AND p.observed_at = a.observed_at
        WHERE a.status = 'sent' AND a.sent_at >= ?
        ORDER BY a.sent_at
        """, (since_iso,)).fetchall()


def _verdicts(jconn, since_iso: str) -> dict[tuple[str, str], str]:
    # ORDER BY ts: one lot can produce several signals in a day with
    # different verdicts, and the last one must win deterministically
    # (raised by the daily review 2026-09-20; no wrong row observed yet).
    return {(r[0], r[1]): r[2] for r in jconn.cursor().execute(
        "SELECT marketplace, listing_external_id, cross_verdict FROM journal_signals "
        "WHERE scenario = 'base' AND ts >= ? ORDER BY ts", (since_iso,))}


def build(gconn, jconn, now: datetime | None = None) -> str:
    from .journal_report import generate_report
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    out = [f"# Digest {now:%Y-%m-%d %H:%M} UTC", "", "## Health", "",
           health.format_report(health.collect(gconn, jconn, now)), ""]

    rows = _sent_rows(gconn, since)
    verdicts = _verdicts(jconn, since)
    out += [f"## Sent to the bot, 24 h: {len(rows)}", ""]
    ratios = []
    for r in rows:
        price = (r["new_price_nano"] or 0) / 1e9
        floor = (r["floor_at_drop_nano"] or 0) / 1e9
        ratio = floor / price if price else 0
        ratios.append((ratio, r))
        out.append(
            f"- {r['sent_at'][:16]} {r['marketplace']} {r['collection_name']} #{r['gift_number']} "
            f"{r['model_name']} / {r['backdrop_name']}: price {price:.2f}, floor {floor:.2f} "
            f"({r['floor_level_at_drop']}, depth {r['floor_listed_count_at_drop']}), ratio {ratio:.2f}, "
            f"cross {verdicts.get((r['marketplace'], r['listing_external_id']), '?')}")
    out += ["", f"## Highest floor/price ratio (review by hand), top {REVIEW_TOP}", ""]
    for ratio, r in sorted(ratios, key=lambda x: x[0], reverse=True)[:REVIEW_TOP]:
        out.append(f"- {ratio:.2f}  {r['marketplace']} {r['collection_name']} #{r['gift_number']} "
                   f"{r['model_name']} / {r['backdrop_name']}")
    out += ["", "## Journal", "", "```", generate_report(jconn, now=now), "```"]
    return "\n".join(out)


def short_text(gconn, jconn, now: datetime | None = None) -> str:
    """The bot version: health line, sends per marketplace, journal (base)."""
    from .journal_view import magazine_text
    now = now or datetime.now(timezone.utc)
    checks = health.collect(gconn, jconn, now)
    bad = [c for c in checks if c.level != health.OK]
    rows = _sent_rows(gconn, (now - timedelta(hours=24)).isoformat())
    by_m: dict[str, int] = {}
    for r in rows:
        by_m[r["marketplace"]] = by_m.get(r["marketplace"], 0) + 1
    sent = ", ".join(f"{m} {n}" for m, n in sorted(by_m.items())) or "нет"
    head = "Всё в порядке" if not bad else "Проблемы: " + "; ".join(c.name for c in bad)
    return f"Отчёт за сутки\n{head}\nОтправлено сигналов: {sent}\n\n" + magazine_text(jconn)


def write(gconn, jconn, now: datetime | None = None, report_dir: Path = REPORT_DIR) -> Path:
    now = now or datetime.now(timezone.utc)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"digest-{now:%Y-%m-%d}.md"
    path.write_text(build(gconn, jconn, now), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily digest")
    parser.add_argument("--db", default=config.DB_DSN)
    parser.add_argument("--journal", default=journal_config.JOURNAL_DB_DSN)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = write(health.open_readonly(args.db), health.open_readonly(args.journal))
    print(path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
