"""КАК ТЕСТИРОВАТЬ items 8-10 (realization-rate delivery, ПРАВКА 3):
a neighbour without the PAIR is asked for its MODEL floor, used only as a
lower bound -- it can block a signal, never confirm one.
"""
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.cross_check import (
    BLOCKING_VERDICTS,
    VERDICT_ERROR,
    VERDICT_MODEL_BOUND_INCONCLUSIVE,
    VERDICT_SENT_NEIGHBOUR_HIGHER,
    VERDICT_SKIPPED_NEIGHBOUR_CHEAPER,
    cross_check,
)
from gift_sniper.notifier import format_caption
from gift_sniper.tonnel_client import TonnelError, TonnelFloor
from .test_cross_check import FakeTonnelClient, _signal
from .test_mrkt_client import FakeResponse, FakeSession, _client

_NO_PAIR = TonnelFloor(floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=[])


def _model(floor_with_fee: str, count=4) -> TonnelFloor:
    nano = int(Decimal(floor_with_fee) * config.NANO)
    return TonnelFloor(floor_nano=nano, floor_with_fee_nano=nano, listed_count=count, status="ok", raw=[])


def _enable(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)


def test_item8_no_pair_model_floor_20_price_25_blocks(monkeypatch):
    _enable(monkeypatch)
    conn = db.connect(":memory:")
    signal = _signal("portals", "25")
    tonnel = FakeTonnelClient(floor=_NO_PAIR, model_floor=_model("20"))

    cross_check(conn, signal, tonnel_client=tonnel)

    assert signal.cross_verdict == VERDICT_SKIPPED_NEIGHBOUR_CHEAPER
    assert signal.cross_verdict in BLOCKING_VERDICTS
    assert len(tonnel.model_calls) == 1
    row = conn.execute("SELECT verdict, neighbour_floor_nano FROM cross_check_snapshots").fetchone()
    assert tuple(row) == (VERDICT_SKIPPED_NEIGHBOUR_CHEAPER, int(Decimal("20") * config.NANO))


def test_item9_no_pair_model_floor_20_price_15_inconclusive_sent_without_checkmark(monkeypatch):
    _enable(monkeypatch)
    conn = db.connect(":memory:")
    signal = _signal("portals", "15")
    tonnel = FakeTonnelClient(floor=_NO_PAIR, model_floor=_model("20"))

    cross_check(conn, signal, tonnel_client=tonnel)

    assert signal.cross_verdict == VERDICT_MODEL_BOUND_INCONCLUSIVE
    assert signal.cross_verdict not in BLOCKING_VERDICTS
    assert signal.cross_verdict != VERDICT_SENT_NEIGHBOUR_HIGHER
    caption = format_caption(signal)
    assert caption.startswith("<b>ЛИСТИНГ</b>")
    assert "✓" not in caption


def test_item10_neighbour_with_pair_never_queried_for_model_floor(monkeypatch):
    _enable(monkeypatch)

    class PairOnlyTonnel(FakeTonnelClient):
        def model_floor(self, *args, **kwargs):
            raise AssertionError("model floor must not be requested when the neighbour has the pair")

    conn = db.connect(":memory:")
    signal = _signal("portals", "15")
    pair = TonnelFloor(floor_nano=int(Decimal("30") * config.NANO), floor_with_fee_nano=int(Decimal("33") * config.NANO),
                       listed_count=5, status="ok", raw=[])
    cross_check(conn, signal, tonnel_client=PairOnlyTonnel(floor=pair))
    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER


def test_model_floor_query_error_votes_error_not_block(monkeypatch):
    _enable(monkeypatch)
    conn = db.connect(":memory:")
    signal = _signal("portals", "25")
    tonnel = FakeTonnelClient(floor=_NO_PAIR, model_raises=TonnelError("boom"))

    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_ERROR
    assert signal.cross_verdict not in BLOCKING_VERDICTS


def test_neighbour_error_on_pair_query_does_not_trigger_model_query(monkeypatch):
    _enable(monkeypatch)
    conn = db.connect(":memory:")
    signal = _signal("portals", "25")
    tonnel = FakeTonnelClient(raises=TonnelError("down"), model_floor=_model("20"))

    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_ERROR
    assert tonnel.model_calls == []


def test_mrkt_model_floor_query_has_no_backdrop_filter():
    gift = {"number": 7, "salePrice": int(Decimal("12.5") * config.NANO), "isOnSale": True,
            "isOnAuction": False, "isLocked": False, "isLockedForSale": False, "premarketStatus": "None"}
    session = FakeSession([FakeResponse(200, {"gifts": [gift], "cursor": "", "total": 9})])
    floor = _client(session).model_floor("Snoop Cigar", "Classic")

    _url, body, _headers = session.calls[0]
    assert body["collectionNames"] == ["Snoop Cigar"]
    assert body["modelNames"] == ["Classic"]
    assert body["backdropNames"] == []
    assert floor.status == "ok"
    assert floor.floor_nano == int(Decimal("12.5") * config.NANO)
    assert floor.listed_count == 9
