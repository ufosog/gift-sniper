"""For every journal row that reached an execution verdict (sniped or OPEN),
show what gift_sniper.db knows about the lot afterwards: disappearance time
and status, later price changes. Distinguishes the hypotheses:
  H1 lot really gone (sold/unlisted) before the check  -> real snipe
  H2 lot repriced before the check                     -> "sniped" by reprice
  H3 lot alive at the same price at check time         -> false snipe
Read-only.
"""
import sqlite3
from datetime import datetime, timezone

def dt(s):
    if s is None: return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

j = sqlite3.connect("file:journal.db?mode=ro", uri=True); j.row_factory = sqlite3.Row
g = sqlite3.connect("file:gift_sniper.db?mode=ro", uri=True); g.row_factory = sqlite3.Row

rows = j.execute("""select signal_id, marketplace, listing_external_id, ts, price_nano, status, reject_reason, ts_open,
  min(scenario) sc from journal_signals where reject_reason='sniped' or status in ('OPEN','CLOSED','UNSOLD')
  group by signal_id order by ts""").fetchall()
for r in rows:
    ts = dt(r["ts"])
    lc = g.execute("select * from listing_lifecycle where marketplace=? and listing_external_id=?",
                   (r["marketplace"], r["listing_external_id"])).fetchone()
    ph = g.execute("select observed_at, old_price_nano, new_price_nano from price_history where marketplace=? and listing_external_id=? and observed_at>? order by observed_at",
                   (r["marketplace"], r["listing_external_id"], r["ts"])).fetchall()
    verdict = r["reject_reason"] or r["status"]
    gone = None
    if lc and lc["disappeared_at"]:
        gone = (dt(lc["disappeared_at"]) - ts).total_seconds() / 60
    later = [(round((dt(p["observed_at"]) - ts).total_seconds()/60, 1), p["new_price_nano"]/1e9) for p in ph[:4]]
    print(f"{r['marketplace']:7} {verdict:8} price={r['price_nano']/1e9:7.2f} ts={r['ts'][:16]} "
          f"lc={'-' if not lc else (lc['final_status'] or 'alive')} gone_after_min={None if gone is None else round(gone,1)} "
          f"last_seen={None if not lc else lc['last_seen_at'][:16]} last_price={None if not lc or lc['last_price_nano'] is None else lc['last_price_nano']/1e9} "
          f"reprices(min,price)={later}")
