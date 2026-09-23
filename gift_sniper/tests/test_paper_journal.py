"""КАК ТЕСТИРОВАТЬ 1-13 (paper journal) plus guards: formula parity with
signals.compute_profit_nano, balance reconciliation, same-level floor at
close, network errors postponing instead of guessing, poller hook.
"""
import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gift_sniper import config, db, journal_config, journal_db, paper_journal
from gift_sniper.errors import PortalsError
from gift_sniper.journal_report import generate_report, reconciliation_ok
from gift_sniper.paper_journal import JournalClients, draw, pnl_nano, record_signal, run_closer, signal_id
from gift_sniper.signals import Signal, compute_profit_nano, realization_rate
from gift_sniper.tonnel_client import TonnelFloor

T0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def _n(ton) -> int:
    return int(Decimal(str(ton)) * config.NANO)


def _sig(ext_id="L1", price="30", floor="40", depth=5, marketplace="portals", level="pair",
         observed_at=T0, verdict="sent_no_neighbour", gift_number=7):
    return Signal(
        listing_external_id=ext_id, tg_id="tg", collection_id="col-1", collection_name="CollA",
        model_name="M", backdrop_name="B", symbol_name=None, gift_number=gift_number,
        photo_url=None, animation_url=None, currency="TON",
        old_price_nano=_n(price) * 2, new_price_nano=_n(price), delta_pct=Decimal("-10"),
        observed_at=observed_at, floor_nano=_n(floor), floor_source="at_drop", floor_level=level,
        listed_count=depth, ratio=Decimal(floor) / Decimal(price), discount=Decimal("0.2"),
        profit_before_withdrawal_nano=None, profit_nano=None, profit_usd=None,
        marketplace=marketplace, cross_verdict=verdict,
    )


def _book(*prices):
    return {"results": [{"id": f"o-{i}", "status": "listed", "price": p} for i, p in enumerate(prices)]}


class FakePortals:
    def __init__(self, by_ids=None, pair=None, model=None, pair_error=None, model_error=None):
        self.by_ids = by_ids if by_ids is not None else {"results": [{"id": "L1", "status": "listed", "price": "30"}]}
        self.pair = pair if pair is not None else _book("40", "40", "40", "40", "40")
        self.model = model if model is not None else _book("40", "40", "40", "40", "40")
        self.pair_error = pair_error
        self.model_error = model_error
        self.by_ids_calls = []

    def search_by_ids(self, ids, limit=None):
        self.by_ids_calls.append(ids)
        if isinstance(self.by_ids, Exception):
            raise self.by_ids
        return self.by_ids

    def search_pair_floor(self, collection_id, model_name, backdrop_name, limit=20, offset=0):
        if self.pair_error is not None:
            raise self.pair_error
        return self.pair

    def search_model_floor(self, collection_id, model_name, limit=50, offset=0):
        if self.model_error is not None:
            raise self.model_error
        return self.model


@pytest.fixture
def jconn():
    return journal_db.connect(":memory:")


# The "all" scenario deliberately has no portfolio limits (see
# journal_config.SCENARIOS), so balance/slot assertions apply to the
# portfolio scenarios only.
PORTFOLIO = [s for s in journal_config.SCENARIOS if s != "all"]


def _portfolio(rows: dict):
    return [r for name, r in rows.items() if name in PORTFOLIO]


def _rows(jconn, sid=None):
    if sid is None:
        return jconn.execute("SELECT * FROM journal_signals ORDER BY ts, scenario").fetchall()
    return {r["scenario"]: r for r in jconn.execute("SELECT * FROM journal_signals WHERE signal_id = ?", (sid,))}


def _balance(jconn, scenario):
    return jconn.execute("SELECT balance_nano FROM journal_state WHERE scenario = ?", (scenario,)).fetchone()[0]


def _open(jconn, signal, clients):
    record_signal(jconn, signal)
    run_closer(jconn, clients, now=signal.observed_at + timedelta(seconds=60))


# --- 1, 2: one row per scenario, idempotent ---------------------------------

def test_item1_one_signal_three_rows(jconn):
    assert record_signal(jconn, _sig()) is True
    rows = _rows(jconn)
    assert sorted(r["scenario"] for r in rows) == sorted(journal_config.SCENARIOS)
    assert all(r["status"] == "PENDING_EXEC" for r in rows)
    assert all(r["ts_open"] is None for r in rows)


def test_item2_repeat_record_no_duplicates(jconn):
    signal = _sig()
    record_signal(jconn, signal)
    assert record_signal(jconn, signal) is False
    assert len(_rows(jconn)) == len(journal_config.SCENARIOS)


def test_entry_uses_only_signal_data(jconn):
    signal = _sig(price="30", floor="40", depth=5, verdict="model_bound_inconclusive")
    record_signal(jconn, signal)
    row = _rows(jconn, signal_id(signal))["base"]
    assert row["floor_nano"] == _n(40)
    assert row["floor_depth"] == 5
    assert row["cross_verdict"] == "model_bound_inconclusive"
    assert row["expected_exit_nano"] == _n(38)
    assert row["expected_pnl_nano"] == _n("6.89")


# --- 3, 4, 5: eligibility rejections ---------------------------------------

def test_item3_depth_1_rejected_thin_book(jconn):
    # floor 100 keeps the expected spread high even at rate 0.61, so the
    # thin book is the only reason to reject.
    signal = _sig(price="30", floor="100", depth=1)
    record_signal(jconn, signal)
    rows = _rows(jconn, signal_id(signal))
    assert {r["status"] for r in rows.values()} == {"REJECTED"}
    assert {r["reject_reason"] for r in rows.values()} == {"thin_book"}


def test_item4_lot_130_at_balance_100_rejected_insufficient_balance(jconn):
    signal = _sig(price="130", floor="200", depth=5)
    record_signal(jconn, signal)
    assert {r["reject_reason"] for r in _portfolio(_rows(jconn, signal_id(signal)))} == {"insufficient_balance"}


def test_item3_lot_90_at_balance_100_opens_whole_lot(jconn):
    signal = _sig(price="90", floor="120", depth=5)
    by_ids = {"results": [{"id": "L1", "status": "listed", "price": "90"}]}
    _open(jconn, signal, JournalClients(portals=FakePortals(by_ids=by_ids)))
    rows = _rows(jconn, signal_id(signal))
    assert {r["status"] for r in rows.values()} == {"OPEN"}
    assert {r["position_size_nano"] for r in rows.values()} == {_n(90)}
    assert all(_balance(jconn, s) == _n(10) for s in PORTFOLIO)


def test_no_percentage_position_limit_left():
    assert not hasattr(journal_config, "POSITION_PCT")


class _AnyListed(FakePortals):
    """Every requested id is listed at 10."""
    def search_by_ids(self, ids, limit=None):
        self.by_ids_calls.append(ids)
        return {"results": [{"id": ids[0], "status": "listed", "price": "10"}]}


def _open_five(jconn):
    clients = JournalClients(portals=_AnyListed())
    for i in range(5):
        _open(jconn, _sig(ext_id=f"L{i}", price="10", floor="20", observed_at=T0 + timedelta(seconds=i)), clients)
    return clients


def test_item5_sixth_position_rejected_no_slot(jconn):
    _open_five(jconn)
    sixth = _sig(ext_id="L5", price="10", floor="20", observed_at=T0 + timedelta(seconds=5))
    record_signal(jconn, sixth)
    assert {r["reject_reason"] for r in _portfolio(_rows(jconn, signal_id(sixth)))} == {"no_slot"}


def test_pending_rows_do_not_hold_slots(jconn):
    # Live defect 2026-09-17: 5 pending rows waited 46 h for a closer that
    # was down and blocked every slot -> 311 no_slot rows.
    for i in range(6):
        record_signal(jconn, _sig(ext_id=f"L{i}", price="10", floor="20", observed_at=T0 + timedelta(seconds=i)))
    assert {r["status"] for r in _rows(jconn)} == {"PENDING_EXEC"}


def test_sixth_pending_rejected_no_slot_when_opening(jconn):
    for i in range(6):
        record_signal(jconn, _sig(ext_id=f"L{i}", price="10", floor="20", observed_at=T0 + timedelta(seconds=i)))
    run_closer(jconn, JournalClients(portals=_AnyListed()), now=T0 + timedelta(seconds=60))
    base = [r for r in _rows(jconn) if r["scenario"] == "base"]
    assert [r["status"] for r in base].count("OPEN") == 5
    assert [r["reject_reason"] for r in base if r["status"] == "REJECTED"] == ["no_slot"]


def test_pending_older_than_max_age_rejected_exec_check_missed_without_lookup(jconn):
    fake = FakePortals()
    record_signal(jconn, _sig())
    run_closer(jconn, JournalClients(portals=fake),
               now=T0 + timedelta(seconds=journal_config.EXEC_MAX_AGE_SEC + 1))
    assert fake.by_ids_calls == []
    assert {(r["status"], r["reject_reason"]) for r in _rows(jconn)} == {("REJECTED", "exec_check_missed")}
    assert all(_balance(jconn, s) == journal_config.limits(s)[0] for s in journal_config.SCENARIOS)


def test_pending_within_window_is_checked(jconn):
    fake = FakePortals()
    record_signal(jconn, _sig())
    run_closer(jconn, JournalClients(portals=fake), now=T0 + timedelta(seconds=journal_config.EXEC_MAX_AGE_SEC))
    assert fake.by_ids_calls == [["L1"]]
    assert {r["status"] for r in _rows(jconn)} == {"OPEN"}


def test_repriced_down_lot_opened_at_signal_price(jconn):
    signal = _sig()
    by_ids = {"results": [{"id": "L1", "status": "listed", "price": "29"}]}
    _open(jconn, signal, JournalClients(portals=FakePortals(by_ids=by_ids)))
    rows = _rows(jconn, signal_id(signal)).values()
    assert {r["status"] for r in rows} == {"OPEN"}
    assert {r["exec_state"] for r in rows} == {"repriced_down"}
    assert {r["exec_price_nano"] for r in rows} == {_n(29)}
    assert {r["position_size_nano"] for r in rows} == {_n(30)}


def test_exec_check_records_state_and_time(jconn):
    signal = _sig()
    _open(jconn, signal, JournalClients(portals=FakePortals(by_ids={"results": []})))
    rows = _rows(jconn, signal_id(signal)).values()
    assert {r["exec_state"] for r in rows} == {"gone"}
    assert {r["exec_checked_at"] for r in rows} == {(T0 + timedelta(seconds=60)).isoformat()}


def test_second_signal_on_held_lot_rejected_lot_already_held(jconn):
    # Live defect: MRKT Chill Flame 9d63b46a was OPEN twice from two signals.
    clients = JournalClients(portals=_AnyListed())
    first = _sig(ext_id="L1", price="10", floor="20", observed_at=T0)
    _open(jconn, first, clients)
    second = _sig(ext_id="L1", price="10", floor="20", observed_at=T0 + timedelta(hours=1))
    _open(jconn, second, clients)
    assert {r["reject_reason"] for r in _rows(jconn, signal_id(second)).values()} == {"lot_already_held"}


def test_thin_book_reported_before_spread_too_low(jconn):
    # Depth 1 AND a spread far below every scenario's minimum: the reason
    # must be the thin book, the cause of the low realization rate.
    signal = _sig(price="30", floor="33", depth=1)
    record_signal(jconn, signal)
    assert {r["reject_reason"] for r in _rows(jconn, signal_id(signal)).values()} == {"thin_book"}


def test_quality_reason_reported_before_no_slot(jconn):
    _open_five(jconn)
    signal = _sig(ext_id="L9", price="30", floor="33", depth=5, observed_at=T0 + timedelta(seconds=9))
    record_signal(jconn, signal)
    assert {r["reject_reason"] for r in _rows(jconn, signal_id(signal)).values()} == {"spread_too_low"}


def test_run_exec_check_does_not_close_positions(jconn, monkeypatch):
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)
    signal = _sig()
    clients = JournalClients(portals=FakePortals())
    _open(jconn, signal, clients)
    paper_journal.run_exec_check(jconn, clients, now=T0 + timedelta(hours=49))
    assert {r["status"] for r in _rows(jconn)} == {"OPEN"}


def test_low_expected_spread_rejected(jconn):
    signal = _sig(price="30", floor="33", depth=5)  # 33*0.95*0.98 - 30 - 0.35 < 0
    record_signal(jconn, signal)
    assert {r["reject_reason"] for r in _rows(jconn, signal_id(signal)).values()} == {"spread_too_low"}


# --- 6, 7: execution check -------------------------------------------------

def test_item6_lot_gone_rejected_sniped_balance_untouched(jconn):
    signal = _sig()
    _open(jconn, signal, JournalClients(portals=FakePortals(by_ids={"results": []})))
    rows = _rows(jconn, signal_id(signal))
    assert {(r["status"], r["reject_reason"]) for r in rows.values()} == {("REJECTED", "sniped")}
    assert all(_balance(jconn, s) == journal_config.limits(s)[0] for s in journal_config.SCENARIOS)


def test_repriced_lot_rejected_sniped(jconn):
    signal = _sig()
    by_ids = {"results": [{"id": "L1", "status": "listed", "price": "31"}]}
    _open(jconn, signal, JournalClients(portals=FakePortals(by_ids=by_ids)))
    assert {r["reject_reason"] for r in _rows(jconn, signal_id(signal)).values()} == {"sniped"}


def test_item7_lot_still_there_opened_balance_reserved(jconn):
    signal = _sig()
    _open(jconn, signal, JournalClients(portals=FakePortals()))
    rows = _rows(jconn, signal_id(signal))
    assert {r["status"] for r in rows.values()} == {"OPEN"}
    assert all(r["ts_open"] is not None for r in rows.values())
    assert all(_balance(jconn, s) == _n(70) for s in PORTFOLIO)


def test_pending_younger_than_exec_min_age_not_checked(jconn):
    fake = FakePortals()
    record_signal(jconn, _sig())
    run_closer(jconn, JournalClients(portals=fake), now=T0 + timedelta(seconds=10))
    assert fake.by_ids_calls == []
    assert {r["status"] for r in _rows(jconn)} == {"PENDING_EXEC"}


def test_execution_check_network_error_stays_pending(jconn):
    record_signal(jconn, _sig())
    run_closer(jconn, JournalClients(portals=FakePortals(by_ids=PortalsError("down"))), now=T0 + timedelta(seconds=60))
    assert {r["status"] for r in _rows(jconn)} == {"PENDING_EXEC"}


# --- 8, 9, 10, 11: closing ---------------------------------------------------

def test_item8_sold_portals_exit_38_pnl_6_89(jconn, monkeypatch):
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)
    signal = _sig()
    clients = JournalClients(portals=FakePortals())
    _open(jconn, signal, clients)
    run_closer(jconn, clients, now=T0 + timedelta(hours=49))

    row = _rows(jconn, signal_id(signal))["base"]
    assert row["status"] == "CLOSED"
    assert row["sold_flag"] == 1
    assert row["floor_at_close_nano"] == _n(40)
    assert row["depth_at_close"] == 5
    assert row["exit_price_nano"] == _n(38)
    assert row["pnl_nano"] == _n("6.89")


def test_item9_not_sold_unsold_at_liquidation_rate(jconn, monkeypatch):
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.99)
    signal = _sig()
    clients = JournalClients(portals=FakePortals())
    _open(jconn, signal, clients)
    run_closer(jconn, clients, now=T0 + timedelta(hours=49))

    row = _rows(jconn, signal_id(signal))["base"]
    assert row["status"] == "UNSOLD"
    assert row["sold_flag"] == 0
    assert row["exit_price_nano"] == _n(28)  # 40 * 0.70


def test_no_floor_at_close_unsold_at_price_times_liquidation(jconn):
    signal = _sig()
    fake = FakePortals()
    _open(jconn, signal, JournalClients(portals=fake))
    fake.pair = {"results": []}
    run_closer(jconn, JournalClients(portals=fake), now=T0 + timedelta(hours=49))
    row = _rows(jconn, signal_id(signal))["base"]
    assert row["status"] == "UNSOLD"
    assert row["floor_at_close_nano"] is None
    assert row["exit_price_nano"] == _n(21)  # 30 * 0.70


def test_item10_draw_is_deterministic():
    sid = signal_id(_sig())
    assert draw(sid, "base") == draw(sid, "base")
    assert 0 <= draw(sid, "base") < 1
    expected = int.from_bytes(hashlib.sha256(f"{sid}base".encode()).digest()[:8], "big") / 2**64
    assert draw(sid, "base") == expected


def test_item10_two_independent_runs_give_identical_outcomes():
    outcomes = []
    for _run in range(2):
        conn = journal_db.connect(":memory:")
        clients = JournalClients(portals=FakePortals())
        for i in range(3):
            _open(conn, _sig(ext_id="L1" if i == 0 else f"X{i}", price="10", floor="20",
                             observed_at=T0 + timedelta(seconds=i)), clients)
        fake = FakePortals(by_ids={"results": [{"id": "any", "status": "listed", "price": "10"}]})
        run_closer(conn, JournalClients(portals=fake), now=T0 + timedelta(minutes=5))
        run_closer(conn, JournalClients(portals=fake), now=T0 + timedelta(hours=49))
        outcomes.append([(r["signal_id"], r["scenario"], r["status"], r["pnl_nano"])
                         for r in conn.execute("SELECT * FROM journal_signals ORDER BY signal_id, scenario")])
    assert outcomes[0] == outcomes[1]


def test_item11_equity_recomputed_after_close(jconn, monkeypatch):
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)
    signal = _sig()
    clients = JournalClients(portals=FakePortals())
    _open(jconn, signal, clients)
    equity_open = jconn.execute("SELECT equity_nano FROM journal_state WHERE scenario='base'").fetchone()[0]
    assert equity_open == _n(70) + _n(38)  # balance + open position at floor*rate

    run_closer(jconn, clients, now=T0 + timedelta(hours=49))
    state = jconn.execute("SELECT * FROM journal_state WHERE scenario='base'").fetchone()
    # 70 + 30 + 6.89 - 50% of 6.89 withdrawn
    assert state["balance_nano"] == _n("103.445")
    assert state["withdrawn_nano"] == _n("3.445")
    assert state["equity_nano"] == state["balance_nano"]
    assert state["equity_nano"] != equity_open
    assert state["open_positions"] == 0


def test_balance_reconciles_after_open_and_close(jconn, monkeypatch):
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0 if scenario != "pess" else 0.99)
    clients = JournalClients(portals=FakePortals())
    _open(jconn, _sig(), clients)
    for scenario in journal_config.SCENARIOS:
        assert reconciliation_ok(jconn, scenario)
    run_closer(jconn, clients, now=T0 + timedelta(hours=49))
    for scenario in journal_config.SCENARIOS:
        assert reconciliation_ok(jconn, scenario)


def test_hold_hours_per_scenario(jconn):
    signal = _sig()
    clients = JournalClients(portals=FakePortals())
    _open(jconn, signal, clients)
    run_closer(jconn, clients, now=T0 + timedelta(hours=13))
    statuses = {s: r["status"] for s, r in _rows(jconn, signal_id(signal)).items()}
    assert statuses["opt"] in ("CLOSED", "UNSOLD")
    assert statuses["base"] == "OPEN"
    assert statuses["pess"] == "OPEN"


def test_close_network_error_keeps_position_open(jconn):
    signal = _sig()
    fake = FakePortals()
    _open(jconn, signal, JournalClients(portals=fake))
    fake.pair_error = PortalsError("down")
    run_closer(jconn, JournalClients(portals=fake), now=T0 + timedelta(hours=49))
    assert {r["status"] for r in _rows(jconn, signal_id(signal)).values()} == {"OPEN"}


def test_model_level_signal_closes_on_model_floor_not_pair(jconn, monkeypatch):
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)
    signal = _sig(level="model")
    fake = FakePortals(pair_error=AssertionError("pair floor must not be queried for a model-level signal"))
    _open(jconn, signal, JournalClients(portals=fake))
    run_closer(jconn, JournalClients(portals=fake), now=T0 + timedelta(hours=49))
    assert _rows(jconn, signal_id(signal))["base"]["status"] == "CLOSED"


def test_tonnel_close_uses_floor_without_buyer_fee(jconn, monkeypatch):
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)

    class FakeTonnel:
        def search_minimal_by_gift_ids(self, gift_ids, limit):
            return [{"gift_id": gift_ids[0], "status": "forsale", "price": 30.0}]

        def model_floor(self, gift_name=None, model=None, exclude_gift_num=None):
            return TonnelFloor(floor_nano=_n(40), floor_with_fee_nano=_n(44), listed_count=5, status="ok", raw=[])

    signal = _sig(ext_id="2001", marketplace="tonnel", level="model")
    clients = JournalClients(tonnel=FakeTonnel())
    _open(jconn, signal, clients)
    run_closer(jconn, clients, now=T0 + timedelta(hours=49))
    row = _rows(jconn, signal_id(signal))["base"]
    assert row["exit_price_nano"] == _n(38)
    assert row["pnl_nano"] == _n(5)  # 38 - 30*1.1


def test_pnl_formula_matches_compute_profit_nano_for_every_marketplace():
    floor, price = _n("41.37"), _n("29.10")
    for depth in (1, 3, 5, 12):
        for marketplace in ("portals", "tonnel", "mrkt"):
            _before, expected = compute_profit_nano(marketplace, floor, price, depth)
            assert pnl_nano(marketplace, Decimal(floor) * realization_rate(depth), price) == expected


# --- 12: report --------------------------------------------------------------

def test_item12_rejected_signals_in_report_with_reasons(jconn):
    record_signal(jconn, _sig(ext_id="big", price="130", floor="200"))
    record_signal(jconn, _sig(ext_id="thin", price="30", floor="100", depth=1, observed_at=T0 + timedelta(seconds=1)))
    text = generate_report(jconn, now=T0 + timedelta(hours=1))
    assert "insufficient_balance=1" in text
    assert "thin_book=1" in text
    for scenario in ("pess", "base", "opt"):
        assert f"=== scenario {scenario}" in text
    assert "-- by marketplace --" in text
    assert "-- by floor level --" in text
    assert "-- by cross verdict --" in text
    assert "reconciliation: ok" in text


# --- 13: journal.db never touches gift_sniper.db ------------------------------

def test_item13_journal_does_not_touch_main_db(monkeypatch):
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)
    main_fd, main_path = tempfile.mkstemp(suffix=".db")
    journal_fd, journal_path = tempfile.mkstemp(suffix=".db")
    os.close(main_fd)
    os.close(journal_fd)
    try:
        db.connect(main_path).close()
        before = hashlib.sha256(open(main_path, "rb").read()).hexdigest()

        jconn = journal_db.connect(journal_path)
        clients = JournalClients(portals=FakePortals())
        _open(jconn, _sig(), clients)
        run_closer(jconn, clients, now=T0 + timedelta(hours=49))
        generate_report(jconn)
        jconn.close()

        assert hashlib.sha256(open(main_path, "rb").read()).hexdigest() == before
    finally:
        for path in (main_path, journal_path, journal_path + "-wal", journal_path + "-shm"):
            if os.path.exists(path):
                os.remove(path)


# --- closer robustness (ЧАСТЬ 4) -------------------------------------------------

def test_item9_closer_processes_tonnel_and_mrkt_when_portals_unavailable(jconn, caplog):
    from gift_sniper.mrkt_client import MrktFloor

    class BrokenPortals(FakePortals):
        def search_by_ids(self, ids, limit=None):
            raise RuntimeError("PORTALS_AUTH missing")

    class FakeTonnel:
        def search_minimal_by_gift_ids(self, gift_ids, limit):
            return [{"gift_id": gift_ids[0], "status": "forsale", "price": 10.0}]

        def pair_floor(self, gift_name=None, model=None, backdrop=None, exclude_gift_num=None):
            return TonnelFloor(floor_nano=_n(20), floor_with_fee_nano=_n(22), listed_count=5, status="ok", raw=[])

    class FakeMrkt:
        def find_by_number(self, collection_name, number):
            return {"isOnSale": True, "salePrice": _n(10)}

        def pair_floor(self, collection_name=None, model_name=None, backdrop_name=None, exclude_number=None):
            return MrktFloor(floor_nano=_n(20), listed_count=5, status="ok", raw=[])

    record_signal(jconn, _sig(ext_id="p-1", price="10", floor="20"))
    record_signal(jconn, _sig(ext_id="2001", price="10", floor="20", marketplace="tonnel", observed_at=T0 + timedelta(seconds=1)))
    record_signal(jconn, _sig(ext_id="m-1", price="10", floor="20", marketplace="mrkt", observed_at=T0 + timedelta(seconds=2)))

    clients = JournalClients(portals=BrokenPortals(), tonnel=FakeTonnel(), mrkt=FakeMrkt())
    with caplog.at_level("INFO", logger="gift_sniper.paper_journal"):
        stats = run_closer(jconn, clients, now=T0 + timedelta(seconds=60))

    by_marketplace = {r["marketplace"]: r["status"] for r in _rows(jconn) if r["scenario"] == "base"}
    assert by_marketplace == {"portals": "PENDING_EXEC", "tonnel": "OPEN", "mrkt": "OPEN"}
    assert stats["opened"] == 2 * len(journal_config.SCENARIOS)
    assert stats["skipped_unavailable"]["portals"] == len(journal_config.SCENARIOS)
    assert any("closer pass" in r.message and f"opened={2 * len(journal_config.SCENARIOS)}" in r.message for r in caplog.records)


def test_closer_without_portals_client_still_processes_others(jconn):
    class FakeTonnel:
        def search_minimal_by_gift_ids(self, gift_ids, limit):
            return [{"gift_id": gift_ids[0], "status": "forsale", "price": 10.0}]

        def pair_floor(self, **kwargs):
            return TonnelFloor(floor_nano=_n(20), floor_with_fee_nano=_n(22), listed_count=5, status="ok", raw=[])

    record_signal(jconn, _sig(ext_id="p-1", price="10", floor="20"))
    record_signal(jconn, _sig(ext_id="2001", price="10", floor="20", marketplace="tonnel", observed_at=T0 + timedelta(seconds=1)))
    stats = run_closer(jconn, JournalClients(tonnel=FakeTonnel()), now=T0 + timedelta(seconds=60))
    assert stats["skipped_unavailable"]["portals"] == len(journal_config.SCENARIOS)
    assert {r["status"] for r in _rows(jconn) if r["marketplace"] == "tonnel"} == {"OPEN"}


def test_open_position_mark_written_by_closer(jconn):
    signal = _sig()
    _open(jconn, signal, JournalClients(portals=FakePortals()))
    row = _rows(jconn, signal_id(signal))["base"]
    assert row["mark_nano"] == _n(38)  # floor 40 * rate 0.95
    assert row["mark_at"] is not None


def test_v1_journal_db_migrates_to_v2_keeping_rows():
    import sqlite3

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        v1_ddl = journal_db._DDL.replace("    mark_nano INTEGER,\n    mark_at TEXT,\n", "")
        raw = sqlite3.connect(path)
        raw.executescript(v1_ddl)
        raw.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, 'x')")
        raw.execute(
            "INSERT INTO journal_signals (signal_id, scenario, ts, marketplace, listing_external_id, price_nano, "
            "floor_nano, status) VALUES ('s1', 'base', 'x', 'mrkt', 'e1', 1, 2, 'PENDING_EXEC')"
        )
        raw.commit()
        raw.close()

        migrated = journal_db.connect(path)
        columns = {r[1] for r in migrated.execute("PRAGMA table_info(journal_signals)")}
        assert {"mark_nano", "mark_at"} <= columns
        assert migrated.execute("SELECT status FROM journal_signals WHERE signal_id='s1'").fetchone()[0] == "PENDING_EXEC"
        assert migrated.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == journal_db.CURRENT_SCHEMA_VERSION
        migrated.close()
    finally:
        for p in (path, path + "-wal", path + "-shm"):
            if os.path.exists(p):
                os.remove(p)


# --- poller hook and uptime ----------------------------------------------------

def test_record_signals_safely_never_raises():
    broken = journal_db.connect(":memory:")
    broken.close()
    paper_journal.record_signals_safely(broken, [_sig()])  # logged, not raised
    paper_journal.record_signals_safely(None, [_sig()])


def test_poller_records_clean_signals_without_notifier():
    from gift_sniper.tonnel_poller import TonnelPoller
    from .test_tonnel_poller import _conn, _seed_tonnel_clean_signal

    conn = _conn()
    _seed_tonnel_clean_signal(conn, price="20.0", floor="30.0")
    jconn = journal_db.connect(":memory:")
    poller = TonnelPoller(conn, notifier=None, journal_conn=jconn)
    poller._last_notify_since = datetime.now(timezone.utc) - timedelta(hours=1)
    poller._maybe_notify()

    rows = jconn.execute("SELECT marketplace, scenario FROM journal_signals").fetchall()
    assert sorted(r["scenario"] for r in rows) == sorted(journal_config.SCENARIOS)
    assert {r["marketplace"] for r in rows} == {"tonnel"}


def test_uptime_tracker_one_row_per_run_ended_at_moves():
    jconn = journal_db.connect(":memory:")
    tracker = paper_journal.UptimeTracker(jconn, "mrkt")
    tracker.heartbeat(T0)
    tracker.heartbeat(T0 + timedelta(minutes=5))
    rows = jconn.execute("SELECT * FROM journal_uptime").fetchall()
    assert len(rows) == 1
    assert rows[0]["started_at"] == T0.isoformat()
    assert rows[0]["ended_at"] == (T0 + timedelta(minutes=5)).isoformat()


def test_close_capped_by_cheaper_same_model_on_other_marketplace(jconn, monkeypatch):
    """Live case 2026-09-19: Tonnel Bonded Ring bought at 40.25, Tonnel
    model floor 150 (one lot), MRKT model floor 42.13. The exit must be
    capped at 42.13 / 1.1 (Tonnel buyer fee), not booked near 150."""
    from gift_sniper.mrkt_client import MrktFloor
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)

    class FakeTonnel:
        def search_minimal_by_gift_ids(self, gift_ids, limit):
            return [{"gift_id": gift_ids[0], "status": "forsale", "price": 40.25}]

        def model_floor(self, gift_name=None, model=None, exclude_gift_num=None):
            return TonnelFloor(floor_nano=_n(150), floor_with_fee_nano=_n(165), listed_count=5, status="ok", raw=[])

    class FakeMrkt:
        def model_floor(self, collection_name=None, model_name=None, exclude_number=None):
            return MrktFloor(floor_nano=_n("42.13"), listed_count=12, status="ok", raw=[])

    clients = JournalClients(tonnel=FakeTonnel(), mrkt=FakeMrkt())
    signal = _sig(ext_id="7588", price="40.25", floor="150", marketplace="tonnel", level="model")
    _open(jconn, signal, clients)
    run_closer(jconn, clients, now=T0 + timedelta(hours=49))
    row = _rows(jconn, signal_id(signal))["base"]
    cap = int(Decimal(_n("42.13")) / Decimal("1.1"))
    assert row["exit_cap_nano"] == cap
    assert row["exit_price_nano"] == int(Decimal(cap) * realization_rate(5))
    assert row["pnl_nano"] < 0  # 40.25 * 1.1 = 44.28 in, ~36.4 out


def test_competitor_query_error_postpones_close(jconn, monkeypatch):
    from gift_sniper.mrkt_client import MrktError

    class FakeTonnel:
        def search_minimal_by_gift_ids(self, gift_ids, limit):
            return [{"gift_id": gift_ids[0], "status": "forsale", "price": 10.0}]

        def model_floor(self, **kwargs):
            return TonnelFloor(floor_nano=_n(20), floor_with_fee_nano=_n(22), listed_count=5, status="ok", raw=[])

    class BrokenMrkt:
        def model_floor(self, **kwargs):
            raise MrktError("401")

    clients = JournalClients(tonnel=FakeTonnel(), mrkt=BrokenMrkt())
    signal = _sig(ext_id="2001", price="10", floor="20", marketplace="tonnel", level="model")
    _open(jconn, signal, clients)
    stats = run_closer(jconn, clients, now=T0 + timedelta(hours=49))
    assert stats["close_postponed"]["tonnel"] == len(journal_config.SCENARIOS)
    assert {r["status"] for r in _rows(jconn)} == {"OPEN"}


def test_signal_older_than_exec_window_rejected_at_record(jconn):
    """After a restart the poller re-reads the last hour of signals; such a
    lot can no longer be checked at 45-120 s, so it must not occupy a
    pending row (live noise 2026-09-20: 6 of 8 rows)."""
    signal = _sig(observed_at=T0)
    record_signal(jconn, signal, now=T0 + timedelta(seconds=journal_config.EXEC_MAX_AGE_SEC + 1))
    rows = _rows(jconn, signal_id(signal))
    assert {(r["status"], r["reject_reason"]) for r in rows.values()} == {("REJECTED", "exec_check_missed")}


def test_fresh_signal_still_pending_at_record(jconn):
    signal = _sig(observed_at=T0)
    record_signal(jconn, signal, now=T0 + timedelta(seconds=5))
    assert {r["status"] for r in _rows(jconn, signal_id(signal)).values()} == {"PENDING_EXEC"}


def test_poller_hook_applies_the_age_check(jconn):
    from gift_sniper.paper_journal import record_signals_safely
    record_signals_safely(jconn, [_sig(observed_at=T0)], now=T0 + timedelta(hours=1))
    assert {r["reject_reason"] for r in _rows(jconn)} == {"exec_check_missed"}


def test_position_cap_rejects_one_huge_lot(jconn, monkeypatch):
    """Live 2026-09-20: one 285 TON lot took 95% of the 300 TON bank and
    every later signal was rejected for lack of money."""
    monkeypatch.setattr(journal_config, "MAX_POSITION_NANO", _n(60))
    record_signal(jconn, _sig(ext_id="big", price="285", floor="340", depth=5))
    assert {r["reject_reason"] for r in _portfolio(_rows(jconn, signal_id(_sig(ext_id="big", price="285", floor="340", depth=5))))} == {"position_too_big"}
    small = _sig(ext_id="small", price="50", floor="80", depth=5, observed_at=T0 + timedelta(seconds=1))
    record_signal(jconn, small)
    assert {r["reject_reason"] for r in _rows(jconn, signal_id(small)).values()} == {None}


def test_position_cap_off_by_default(jconn):
    assert journal_config.MAX_POSITION_NANO == 0
    record_signal(jconn, _sig(ext_id="big", price="90", floor="140", depth=5))
    assert {r["status"] for r in _rows(jconn)} == {"PENDING_EXEC"}


def test_all_scenario_has_no_portfolio_limits(jconn, monkeypatch):
    """The 'all' scenario answers "are the signals profitable", so no slot,
    balance or size limit may reject a signal there (added 2026-09-21: the
    portfolio scenarios closed ~10 trades a day, too slow to conclude)."""
    monkeypatch.setattr(journal_config, "MAX_POSITION_NANO", _n(60))
    clients = JournalClients(portals=_AnyListed())
    for i in range(12):  # more than MAX_POSITIONS, and one lot far over the cap
        price = "285" if i == 0 else "10"
        _open(jconn, _sig(ext_id=f"L{i}", price=price, floor=str(int(price) * 2),
                          observed_at=T0 + timedelta(seconds=i)), clients)
    all_rows = [r for r in _rows(jconn) if r["scenario"] == "all"]
    assert [r["status"] for r in all_rows].count("OPEN") == 12
    base_rows = [r for r in _rows(jconn) if r["scenario"] == "base"]
    assert [r["status"] for r in base_rows].count("OPEN") == journal_config.MAX_POSITIONS
    assert "position_too_big" in {r["reject_reason"] for r in base_rows}


def test_all_scenario_starts_with_its_own_balance(jconn):
    assert _balance(jconn, "all") == journal_config.limits("all")[0]
    assert _balance(jconn, "base") == journal_config.START_BALANCE_NANO


def test_no_network_call_while_a_transaction_is_open(jconn, monkeypatch):
    """Live defect 2026-09-22: the closer held journal.db's write lock
    across HTTP calls while valuing open positions, and the three pollers
    failed with "database is locked" 212 times in 24 h."""
    monkeypatch.setattr(paper_journal, "draw", lambda sid, scenario: 0.0)

    class GuardedPortals(FakePortals):
        def _guard(self):
            assert not jconn.in_transaction, "сетевой запрос внутри открытой транзакции journal.db"

        def search_by_ids(self, ids, limit=None):
            self._guard()
            return super().search_by_ids(ids, limit)

        def search_pair_floor(self, *a, **k):
            self._guard()
            return super().search_pair_floor(*a, **k)

        def search_model_floor(self, *a, **k):
            self._guard()
            return super().search_model_floor(*a, **k)

    clients = JournalClients(portals=GuardedPortals())
    _open(jconn, _sig(), clients)
    run_closer(jconn, clients, now=T0 + timedelta(hours=1))    # marks only
    run_closer(jconn, clients, now=T0 + timedelta(hours=49))   # closing too


def test_unlimited_scenario_is_not_marked(jconn):
    clients = JournalClients(portals=_AnyListed())
    _open(jconn, _sig(ext_id="L1", price="10", floor="20"), clients)
    marks_all = paper_journal.fresh_marks(jconn, "all", clients, {})
    marks_base = paper_journal.fresh_marks(jconn, "base", clients, {})
    assert marks_all == {}
    assert marks_base != {}
