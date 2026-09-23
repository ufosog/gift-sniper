"""Turn the collected SQLite databases into a public dataset.

The raw files are a working database: nano-unit integers, internal ids and
columns nobody outside this project can interpret. This writes plain CSVs
with prices in TON, readable column names and no identifiers that point at
a person.

Four files:
  listings.csv        every lot seen, with its attributes
  price_changes.csv   every price change, with the floor at that moment
  confirmed_sales.csv the rare part: sales with a CONFIRMED price (MRKT)
  paper_trades.csv    the paper journal's own trades and their outcome

Usage: python diag/export_dataset.py --db final_gift_sniper.db
                                     --journal final_journal.db --out dataset/
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

NANO = 10 ** 9


def ton(value):
    return None if value is None else round(int(value) / NANO, 6)


def dump(conn, sql: str, path: Path, transform) -> int:
    rows = conn.execute(sql)
    header = None
    written = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        for row in rows:
            record = transform(row)
            if header is None:
                header = list(record.keys())
                writer.writerow(header)
            writer.writerow([record[k] for k in header])
            written += 1
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export the dataset as CSV")
    parser.add_argument("--db", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--out", default="dataset")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    g = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    g.row_factory = sqlite3.Row
    j = sqlite3.connect(f"file:{args.journal}?mode=ro", uri=True)
    j.row_factory = sqlite3.Row

    n = dump(g, """
        SELECT marketplace, external_id, collection_name, model_name, backdrop_name,
               symbol_name, gift_number, price_nano, currency, status, first_seen_at, listed_at
        FROM listings
    """, out / "listings.csv", lambda r: {
        "marketplace": r["marketplace"], "lot_id": r["external_id"],
        "collection": r["collection_name"], "model": r["model_name"],
        "backdrop": r["backdrop_name"], "symbol": r["symbol_name"],
        "gift_number": r["gift_number"], "price_ton": ton(r["price_nano"]),
        "currency": r["currency"], "status": r["status"],
        "first_seen_at": r["first_seen_at"], "listed_at": r["listed_at"]})
    print(f"listings.csv: {n}")

    n = dump(g, """
        SELECT marketplace, listing_external_id, observed_at, old_price_nano, new_price_nano,
               delta_pct, is_noise, floor_at_drop_nano, floor_listed_count_at_drop, floor_level_at_drop
        FROM price_history
    """, out / "price_changes.csv", lambda r: {
        "marketplace": r["marketplace"], "lot_id": r["listing_external_id"],
        "observed_at": r["observed_at"], "old_price_ton": ton(r["old_price_nano"]),
        "new_price_ton": ton(r["new_price_nano"]), "change_pct": r["delta_pct"],
        "is_noise": r["is_noise"], "floor_at_change_ton": ton(r["floor_at_drop_nano"]),
        "floor_depth": r["floor_listed_count_at_drop"], "floor_level": r["floor_level_at_drop"]})
    print(f"price_changes.csv: {n}")

    n = dump(g, """
        SELECT lc.marketplace, lc.listing_external_id, l.collection_name, l.model_name,
               l.backdrop_name, l.gift_number, lc.sold_price_nano, lc.disappeared_at,
               lc.first_seen_at, lc.floor_at_sale_nano, lc.floor_listed_count_at_sale
        FROM listing_lifecycle lc
        LEFT JOIN listings l ON l.marketplace = lc.marketplace AND l.external_id = lc.listing_external_id
        WHERE lc.final_status = 'sold' AND lc.sold_price_nano IS NOT NULL
    """, out / "confirmed_sales.csv", lambda r: {
        "marketplace": r["marketplace"], "lot_id": r["listing_external_id"],
        "collection": r["collection_name"], "model": r["model_name"],
        "backdrop": r["backdrop_name"], "gift_number": r["gift_number"],
        "sold_price_ton": ton(r["sold_price_nano"]), "sold_at": r["disappeared_at"],
        "listed_at": r["first_seen_at"], "floor_at_sale_ton": ton(r["floor_at_sale_nano"]),
        "floor_depth_at_sale": r["floor_listed_count_at_sale"]})
    print(f"confirmed_sales.csv: {n}")

    n = dump(j, """
        SELECT scenario, ts, marketplace, collection, model, backdrop, gift_number,
               price_nano, floor_nano, floor_level, floor_depth, cross_verdict, ratio,
               status, reject_reason, exec_state, ts_open, ts_close, exit_price_nano,
               pnl_nano, pnl_pct, sold_flag
        FROM journal_signals
    """, out / "paper_trades.csv", lambda r: {
        "scenario": r["scenario"], "signal_at": r["ts"], "marketplace": r["marketplace"],
        "collection": r["collection"], "model": r["model"], "backdrop": r["backdrop"],
        "gift_number": r["gift_number"], "buy_price_ton": ton(r["price_nano"]),
        "floor_ton": ton(r["floor_nano"]), "floor_level": r["floor_level"],
        "floor_depth": r["floor_depth"], "cross_check": r["cross_verdict"],
        "floor_to_price_ratio": r["ratio"], "status": r["status"],
        "reject_reason": r["reject_reason"], "execution_check": r["exec_state"],
        "opened_at": r["ts_open"], "closed_at": r["ts_close"],
        "sell_price_ton": ton(r["exit_price_nano"]), "pnl_ton": ton(r["pnl_nano"]),
        "pnl_pct": r["pnl_pct"], "sold": r["sold_flag"]})
    print(f"paper_trades.csv: {n}")

    for f in sorted(out.iterdir()):
        print(f"  {f.name}: {f.stat().st_size / 1048576:.1f} МБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
