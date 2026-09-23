"""КАК ТЕСТИРОВАТЬ items 1-5 (realization-rate delivery, ПРАВКА 1):
expected sale price = floor * REALIZATION_RATE(depth), applied before
fees, in the single shared compute_profit_nano().
"""
from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.signals import clean_signals, compute_profit_nano, realization_rate, run_cascade
from .test_min_profit_and_stale_floor import _listing, _record_drop, _snapshot


def _nano(ton: str) -> int:
    return int(Decimal(ton) * config.NANO)


def _ton(nano: int) -> Decimal:
    return (Decimal(nano) / config.NANO).quantize(Decimal("0.01"))


def _seed(conn, marketplace, ext_id, price, floor, depth):
    listing = _listing(ext_id, _nano(price), marketplace=marketplace)
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, _nano(floor)))
    observed_at = datetime(2026, 9, 6, 10, 5, tzinfo=timezone.utc)
    _record_drop(conn, marketplace, ext_id, str(Decimal(price) + 5), price, "-10", observed_at,
                 floor_at_drop=floor, floor_listed_count=depth)
    return observed_at


def test_item1_portals_floor_40_price_30_depth_5_passes():
    _before, profit = compute_profit_nano("portals", _nano("40"), _nano("30"), 5)
    assert _ton(profit) == Decimal("6.89")

    conn = db.connect(":memory:")
    observed_at = _seed(conn, "portals", "p-1", "30", "40", 5)
    sigs = clean_signals(conn, now=observed_at, marketplace="portals")
    assert [s.listing_external_id for s in sigs] == ["p-1"]


def test_item2_portals_floor_32_4_price_30_depth_5_dropped_at_below_min_profit():
    _before, profit = compute_profit_nano("portals", _nano("32.4"), _nano("30"), 5)
    # 32.4*0.95*0.98 - 30 - 0.35 = -0.1856 exactly (the spec's "-0.18" is
    # the same number cut to two digits) -- negative either way.
    assert Decimal(profit) / config.NANO == Decimal("-0.1856")

    conn = db.connect(":memory:")
    observed_at = _seed(conn, "portals", "p-2", "30", "32.4", 5)
    cascade = run_cascade(conn, now=observed_at, marketplace="portals")
    assert [r["listing_external_id"] for r in cascade.below_min_profit] == ["p-2"]
    assert cascade.clean == []


def test_item3_tonnel_floor_42_price_34_depth_8():
    _before, profit = compute_profit_nano("tonnel", _nano("42"), _nano("34"), 8)
    assert _ton(profit) == Decimal("2.50")


def test_item4_mrkt_floor_39_78_price_30_60_depth_4_no_withdrawal_fee():
    _before, profit = compute_profit_nano("mrkt", _nano("39.78"), _nano("30.60"), 4)
    assert _ton(profit) == Decimal("6.44")


def test_item5_rate_by_depth_buckets():
    assert realization_rate(1) == Decimal("0.61")
    assert realization_rate(2) == Decimal("0.90")
    assert realization_rate(3) == Decimal("0.90")
    assert realization_rate(4) == Decimal("0.95")
    assert realization_rate(5) == Decimal("0.95")
    assert realization_rate(9) == Decimal("0.95")
    assert realization_rate(10) == Decimal("0.95")
    assert realization_rate(0) == Decimal("0.61")


def test_rates_are_configurable_without_code_changes(monkeypatch):
    monkeypatch.setattr(config, "REALIZATION_RATE_DEPTH_2_3", Decimal("0.80"))
    assert realization_rate(2) == Decimal("0.80")
    _before, profit = compute_profit_nano("tonnel", _nano("100"), _nano("50"), 2)
    assert _ton(profit) == Decimal("25.00")  # 100*0.80 - 50*1.1
