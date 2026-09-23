"""Telegram view of the paper journal (/magazine, /magazine_full).
Read-only, base scenario only -- the other scenarios live in the console
report (journal_report.py).

Realized profit (closed trades) and the current estimate of open
positions are always shown as separate figures, never added into one.
"""
from __future__ import annotations

import html
import sqlite3
from decimal import Decimal

from . import config, journal_config

SCENARIO = "base"
TELEGRAM_LIMIT = 4096
MAX_LISTED = 5
FINISHED = ("CLOSED", "UNSOLD")


def _ton(nano) -> Decimal:
    return Decimal(nano) / config.NANO


def _name(row) -> str:
    number = f" #{row['gift_number']}" if row["gift_number"] is not None else ""
    return html.escape(f"{row['collection'] or '?'}{number}")


def _pct_of_bank(nano) -> Decimal:
    return Decimal(nano) * 100 / journal_config.START_BALANCE_NANO


def _group_lines(title: str, rows: list[sqlite3.Row], key, order: list[str]) -> list[str]:
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(key(r) or "?", []).append(r)
    names = [n for n in order if n in groups] + sorted(n for n in groups if n not in order)
    lines = [title]
    if not names:
        lines.append("· сделок пока нет")
    for name in names:
        group = groups[name]
        closed = [r for r in group if r["status"] in FINISHED]
        open_count = sum(1 for r in group if r["status"] == "OPEN")
        if closed:
            wins = sum(1 for r in closed if r["pnl_nano"] > 0)
            realized = sum(r["pnl_nano"] for r in closed)
            lines.append(
                f"· {html.escape(name)}: закрыто {len(closed)} · прибыльных {wins} ({wins * 100 // len(closed)}%) "
                f"· прибыль {_ton(realized):+.2f} TON · открыто {open_count}"
            )
        else:
            lines.append(f"· {html.escape(name)}: сделок пока нет · открыто {open_count}")
    return lines


def magazine_text(jconn: sqlite3.Connection, full: bool = False, limit: int = TELEGRAM_LIMIT) -> str:
    state = jconn.execute("SELECT * FROM journal_state WHERE scenario = ?", (SCENARIO,)).fetchone()
    rows = jconn.execute("SELECT * FROM journal_signals WHERE scenario = ?", (SCENARIO,)).fetchall()
    bank = journal_config.START_BALANCE_NANO
    balance, equity, withdrawn = state["balance_nano"], state["equity_nano"], state["withdrawn_nano"]
    now_total = equity + withdrawn  # everything the bank is worth now, withdrawn profit included

    closed = sorted((r for r in rows if r["status"] in FINISHED), key=lambda r: r["ts_close"], reverse=True)
    open_rows = sorted((r for r in rows if r["status"] == "OPEN"), key=lambda r: r["ts_open"], reverse=True)

    lines = [
        "<b>ЖУРНАЛ</b>",
        "",
        f"Банк: {_ton(bank):.2f} TON",
        f"Сейчас: {_ton(now_total):.2f} TON  ({_pct_of_bank(now_total - bank):+.2f}%)",
        f"Свободно: {_ton(balance):.2f} TON  ·  в позициях: {_ton(equity - balance):.2f} TON",
    ]
    if withdrawn:
        lines.append(f"Выведено из оборота: {_ton(withdrawn):.2f} TON")
    lines.append("")

    if closed:
        wins = sum(1 for r in closed if r["pnl_nano"] > 0)
        realized = sum(r["pnl_nano"] for r in closed)
        lines += [
            f"Сделок закрыто: {len(closed)}  ·  прибыльных: {wins} ({wins * 100 // len(closed)}%)",
            f"Прибыль по закрытым: {_ton(realized):+.2f} TON ({_pct_of_bank(realized):+.1f}% от банка)",
            f"Средняя сделка: {_ton(Decimal(realized) / len(closed)):+.2f} TON",
        ]
    else:
        lines.append("Сделок закрыто: сделок пока нет")
    lines.append("")

    lines.append(f"Открыто позиций: {len(open_rows)}")
    if open_rows:
        unrealized = sum((r["mark_nano"] if r["mark_nano"] is not None else r["position_size_nano"])
                         - r["position_size_nano"] for r in open_rows)
        lines.append(f"Оценка открытых: {_ton(unrealized):+.2f} TON ({_pct_of_bank(unrealized):+.1f}% от банка)")
        for r in open_rows[:MAX_LISTED]:
            now_value = f"{_ton(r['mark_nano']):.2f}" if r["mark_nano"] is not None else "нет оценки"
            lines.append(f"· {_name(r)} — куплен {_ton(r['position_size_nano']):.2f}, сейчас {now_value}")

    if full:
        lines.append("")
        lines += _group_lines("По площадкам:", rows, lambda r: r["marketplace"], ["portals", "tonnel", "mrkt"])
        lines.append("")
        lines += _group_lines("По уровню флора:", rows, lambda r: r["floor_level"], ["pair", "model"])

    summary = "\n".join(lines)
    if not closed:
        return summary[:limit]

    # The trade list is what gets cut when the message is too long, never the summary.
    text = summary + "\n\nПоследние сделки:"
    shown = 0
    for r in closed[:MAX_LISTED]:
        line = (f"\n· {_name(r)}  {_ton(r['position_size_nano']):.2f} -> {_ton(r['exit_price_nano']):.2f}  "
                f"{_ton(r['pnl_nano']):+.2f}")
        if len(text) + len(line) > limit:
            break
        text += line
        shown += 1
    if shown == 0:
        return summary[:limit]
    return text
