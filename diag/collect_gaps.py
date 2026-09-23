"""Max / p99 gap between consecutive newly-seen listings per marketplace
inside continuous poller runs (journal_uptime periods). Basis for the
health.py staleness threshold. Read-only."""
import sqlite3
from datetime import datetime
g = sqlite3.connect("file:gift_sniper.db?mode=ro", uri=True)
j = sqlite3.connect("file:journal.db?mode=ro", uri=True)
runs = j.execute("select marketplace, started_at, ended_at from journal_uptime").fetchall()
for m in ("portals", "tonnel", "mrkt"):
    gaps = []
    for mm, a, b in runs:
        if mm != m: continue
        ts = [datetime.fromisoformat(r[0]) for r in g.execute(
            "select first_seen_at from listing_lifecycle where marketplace=? and first_seen_at between ? and ? order by 1", (m, a, b))]
        gaps += [(y - x).total_seconds() for x, y in zip(ts, ts[1:])]
    gaps.sort()
    n = len(gaps)
    if n:
        print(f"{m}: n={n} median={gaps[n//2]:.0f}s p99={gaps[int(n*0.99)]:.0f}s p999={gaps[int(n*0.999)]:.0f}s max={gaps[-1]:.0f}s")
