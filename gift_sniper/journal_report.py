"""Paper journal report. Read-only, no network.

Per scenario: totals, then the SAME metrics split by marketplace, by
floor level (pair/model) and by cross-check verdict -- without these
splits there is no way to tell whether model-level signals or unconfirmed
signals are worth sending at all.

Run: python -m gift_sniper.journal_report --db journal.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from . import config, journal_config, journal_db
from .report import _percentile

VERDICT_BUCKETS = ["sent_neighbour_higher", "sent_no_neighbour", "model_bound_inconclusive", "neighbour_thin"]
FINISHED = ("CLOSED", "UNSOLD")


def _ton(nano) -> str:
    return f"{Decimal(nano) / config.NANO:.2f}"


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stats_line(label: str, values: list[float], fmt: str) -> str:
    if not values:
        return f"  {label}: n/a"
    ordered = sorted(values)
    mean = sum(ordered) / len(ordered)
    return f"  {label}: mean {mean:{fmt}}  median {_percentile(ordered, 0.5):{fmt}}"


def _block(rows: list[sqlite3.Row], indent: str = "") -> list[str]:
    out: list[str] = []
    statuses = Counter(r["status"] for r in rows)
    rejected = [r for r in rows if r["status"] == "REJECTED"]
    reasons = Counter(r["reject_reason"] for r in rejected).most_common()
    finished = [r for r in rows if r["status"] in FINISHED]
    closed = [r for r in rows if r["status"] == "CLOSED"]

    out.append(f"signals: {len(rows)}  accepted: {len(rows) - len(rejected)}  REJECTED: {len(rejected)}"
               + (f" ({', '.join(f'{k}={v}' for k, v in reasons)})" if reasons else ""))
    out.append(f"PENDING_EXEC: {statuses['PENDING_EXEC']}  OPEN: {statuses['OPEN']}  "
               f"CLOSED: {statuses['CLOSED']}  UNSOLD: {statuses['UNSOLD']}")
    if finished:
        wins = sum(1 for r in finished if r["pnl_nano"] > 0)
        out.append(f"win rate incl. UNSOLD: {wins}/{len(finished)} = {wins * 100 / len(finished):.1f}%")
    else:
        out.append("win rate incl. UNSOLD: n/a")
    if closed:
        wins_closed = sum(1 for r in closed if r["pnl_nano"] > 0)
        out.append(f"win rate CLOSED only: {wins_closed}/{len(closed)} = {wins_closed * 100 / len(closed):.1f}%")
    else:
        out.append("win rate CLOSED only: n/a")
    out.append(_stats_line("pnl TON", [float(Decimal(r["pnl_nano"]) / config.NANO) for r in finished], ".2f").strip())
    out.append(_stats_line("pnl % of position", [float(r["pnl_pct"]) for r in finished], ".2f").strip())
    out.append(_stats_line(
        "hold hours",
        [(_dt(r["ts_close"]) - _dt(r["ts_open"])).total_seconds() / 3600 for r in finished if r["ts_open"]],
        ".1f",
    ).strip())
    return [f"{indent}{line}" for line in out]


def _split(title: str, rows: list[sqlite3.Row], key, order: list[str] | None = None) -> list[str]:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    names = [n for n in (order or []) if n in groups] + sorted(n for n in groups if n not in (order or []))
    out = [f"-- by {title} --"]
    if not names:
        out.append("  (no data)")
    for name in names:
        out.append(f"  [{name}]")
        out += _block(groups[name], indent="    ")
    return out


def _verdict_bucket(row) -> str:
    verdict = row["cross_verdict"] or "not_checked"
    return verdict if verdict in VERDICT_BUCKETS else f"other:{verdict}"


def _max_drawdown(jconn, scenario: str) -> tuple[int, Decimal]:
    peak = journal_config.limits(scenario)[0]
    worst, worst_pct = 0, Decimal(0)
    for (equity,) in jconn.execute(
        "SELECT equity_nano FROM journal_equity_log WHERE scenario = ? ORDER BY ts, rowid", (scenario,)
    ):
        peak = max(peak, equity)
        drop = peak - equity
        if drop > worst:
            worst, worst_pct = drop, Decimal(drop) * 100 / Decimal(peak)
    return worst, worst_pct


def reconciliation_ok(jconn, scenario: str) -> bool:
    """balance + withdrawn + OPEN positions at entry == START + realized pnl."""
    state = jconn.execute("SELECT * FROM journal_state WHERE scenario = ?", (scenario,)).fetchone()
    open_cost = jconn.execute(
        "SELECT COALESCE(SUM(position_size_nano), 0) FROM journal_signals WHERE scenario = ? AND status = 'OPEN'",
        (scenario,),
    ).fetchone()[0]
    realized = jconn.execute(
        "SELECT COALESCE(SUM(pnl_nano), 0) FROM journal_signals WHERE scenario = ? AND status IN ('CLOSED', 'UNSOLD')",
        (scenario,),
    ).fetchone()[0]
    return (state["balance_nano"] + state["withdrawn_nano"] + open_cost
            == journal_config.limits(scenario)[0] + realized)


def _top_pairs(rows: list[sqlite3.Row]) -> list[str]:
    totals: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["status"] in FINISHED:
            totals[f"{r['collection'] or '?'} / {r['model'] or '?'} / {r['backdrop'] or '?'}"] += r["pnl_nano"]
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    out = ["-- top-10 pairs by profit --"]
    out += [f"  {name}: {_ton(v)} TON" for name, v in ranked[:10] if v > 0] or ["  (none)"]
    out.append("-- top-10 pairs by loss --")
    out += [f"  {name}: {_ton(v)} TON" for name, v in sorted(ranked, key=lambda kv: kv[1])[:10] if v < 0] or ["  (none)"]
    return out


def _uptime(jconn, now: datetime) -> list[str]:
    first = jconn.execute(
        "SELECT MIN(t) FROM (SELECT MIN(ts) AS t FROM journal_signals UNION ALL SELECT MIN(started_at) FROM journal_uptime)"
    ).fetchone()[0]
    out = ["=== poller uptime ==="]
    if first is None:
        return out + ["  (no data)"]
    period_start = _dt(first)
    period = (now - period_start).total_seconds()
    out.append(f"period: {period_start.isoformat()} .. {now.isoformat()}")
    for marketplace in ("portals", "tonnel", "mrkt"):
        covered = sum(
            max(0.0, (min(_dt(end), now) - max(_dt(start), period_start)).total_seconds())
            for start, end in jconn.execute(
                "SELECT started_at, ended_at FROM journal_uptime WHERE marketplace = ?", (marketplace,)
            )
        )
        pct = covered * 100 / period if period > 0 else 0.0
        out.append(f"  {marketplace}: {covered / 3600:.1f} h = {pct:.1f}%")
    return out


def generate_report(jconn: sqlite3.Connection, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    out = ["=== paper journal ===", f"generated_at: {now.isoformat()}",
           f"start balance: {journal_config.START_BALANCE_TON} TON  position = full lot price  "
           f"max positions {journal_config.MAX_POSITIONS}  reinvest {journal_config.REINVEST_PCT}%  "
           f"slippage {journal_config.EXEC_SLIPPAGE}  unsold liquidation {journal_config.UNSOLD_LIQUIDATION_RATE} "
           f"(assumption)", ""]
    out += _uptime(jconn, now)

    for scenario, params in journal_config.SCENARIOS.items():
        rows = jconn.execute("SELECT * FROM journal_signals WHERE scenario = ? ORDER BY ts", (scenario,)).fetchall()
        state = jconn.execute("SELECT * FROM journal_state WHERE scenario = ?", (scenario,)).fetchone()
        start_balance = journal_config.limits(scenario)[0]
        roi = (Decimal(state["equity_nano"]) - start_balance) * 100 / start_balance
        dd, dd_pct = _max_drawdown(jconn, scenario)

        out += ["", f"=== scenario {scenario}: hold {params['hold_hours']}h, "
                    f"min spread {params['min_net_spread_pct']}%, P(sell) {params['sell_probability']} ==="]
        out.append(f"balance: {_ton(state['balance_nano'])}  equity: {_ton(state['equity_nano'])}  "
                   f"withdrawn: {_ton(state['withdrawn_nano'])}  ROI: {roi:.2f}%")
        out.append(f"max drawdown: {_ton(dd)} TON ({dd_pct:.2f}%)")
        out.append(f"reconciliation: {'ok' if reconciliation_ok(jconn, scenario) else 'MISMATCH'}")
        out.append("-- total --")
        out += _block(rows, indent="  ")
        out += _split("marketplace", rows, lambda r: r["marketplace"], ["portals", "tonnel", "mrkt"])
        out += _split("floor level", rows, lambda r: r["floor_level"] or "?", ["pair", "model"])
        out += _split("cross verdict", rows, _verdict_bucket, VERDICT_BUCKETS)
        out += _top_pairs(rows)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=journal_config.JOURNAL_DB_DSN)
    args = parser.parse_args(argv)
    try:
        jconn = journal_db.connect(args.db)
    except journal_db.JournalSchemaError as exc:
        print(f"journal schema check failed: {exc}", file=sys.stderr)
        return 1
    print(generate_report(jconn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
