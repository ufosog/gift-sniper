"""Правка 3 (unified-notification delivery): cross_check.cross_check()
as a pure pre-send filter, both directions. See КАК ТЕСТИРОВАТЬ items
4-8.
"""
from datetime import datetime, timezone
from decimal import Decimal

from gift_sniper import config, db
from gift_sniper.cross_check import (
    BLOCKING_VERDICTS,
    VERDICT_ERROR,
    VERDICT_NEIGHBOUR_THIN,
    VERDICT_SENT_NEIGHBOUR_HIGHER,
    VERDICT_SENT_NO_NEIGHBOUR,
    VERDICT_SKIPPED_NEIGHBOUR_CHEAPER,
    cross_check,
)
from gift_sniper.errors import PortalsError
from gift_sniper.pair_floor import _floor_from_response
from gift_sniper.signals import Signal
from gift_sniper.tonnel_client import TonnelError, TonnelFloor


def _signal(marketplace, price, collection_name="CollA", model_name="M", backdrop_name="B",
            listing_external_id="sig-1", gift_number=1, collection_id=None):
    now = datetime.now(timezone.utc)
    return Signal(
        listing_external_id=listing_external_id, tg_id="tg-1", collection_id=collection_id,
        collection_name=collection_name, model_name=model_name, backdrop_name=backdrop_name,
        symbol_name=None, gift_number=gift_number, photo_url=None, animation_url=None,
        currency="TON", old_price_nano=int(Decimal(price) * config.NANO) * 2,
        new_price_nano=int(Decimal(price) * config.NANO), delta_pct=Decimal("-50"),
        observed_at=now, floor_nano=int(Decimal(price) * config.NANO) * 2,
        floor_source="snapshot", floor_level="model", listed_count=5,
        ratio=Decimal("2"), discount=Decimal("0.5"),
        profit_before_withdrawal_nano=None, profit_nano=None, profit_usd=None,
        marketplace=marketplace,
    )


_NO_TONNEL_FLOOR = TonnelFloor(floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=[])


class FakeTonnelClient:
    def __init__(self, floor=None, raises=None, model_floor=None, model_raises=None):
        self._floor = floor
        self._raises = raises
        self._model_floor = model_floor or _NO_TONNEL_FLOOR
        self._model_raises = model_raises
        self.calls = []
        self.model_calls = []

    def pair_floor(self, gift_name=None, model=None, backdrop=None, exclude_gift_num=None):
        self.calls.append({"gift_name": gift_name, "model": model, "backdrop": backdrop, "exclude_gift_num": exclude_gift_num})
        if self._raises is not None:
            raise self._raises
        return self._floor

    def model_floor(self, gift_name=None, model=None, exclude_gift_num=None):
        self.model_calls.append({"gift_name": gift_name, "model": model, "exclude_gift_num": exclude_gift_num})
        if self._model_raises is not None:
            raise self._model_raises
        return self._model_floor


class FakePortalsClient:
    def __init__(self, response=None, raises=None, model_response=None):
        self._response = response
        self._raises = raises
        self._model_response = model_response or {"results": []}
        self.calls = []
        self.model_calls = []

    def search_pair_floor(self, collection_id, model_name, backdrop_name, limit=20, offset=0):
        self.calls.append({"collection_id": collection_id, "model_name": model_name, "backdrop_name": backdrop_name})
        if self._raises is not None:
            raise self._raises
        return self._response

    def search_model_floor(self, collection_id, model_name, limit=50, offset=0):
        self.model_calls.append({"collection_id": collection_id, "model_name": model_name})
        return self._model_response


def _portals_result(*prices):
    return {"results": [{"id": f"p-{i}", "status": "listed", "price": p} for i, p in enumerate(prices)]}


def _seed_portals_collection(conn, collection_name="CollA", collection_id="col-portals-1"):
    """So db.get_portals_collection_id_by_name() can resolve a Tonnel
    signal's collection_name to a real Portals collection_id."""
    from .test_price_drops_report import _listing, _snapshot
    listing = _listing("portals-anchor", int(Decimal("1.0") * config.NANO))
    listing.collection_name = collection_name
    listing.collection_id = collection_id
    db.upsert_listing_with_floor(conn, listing, _snapshot(listing, int(Decimal("1.0") * config.NANO)))


# --- item 4/6: Portals signal vs. Tonnel neighbour, with the 10% fee -----


def test_item4_portals_signal_skipped_when_tonnel_neighbour_cheaper(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 4: P=17.80, N=11.44 (with fee) -> not sent."""
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("10.4") * config.NANO),
        floor_with_fee_nano=int(Decimal("11.44") * config.NANO),
        listed_count=5, status="ok", raw=[],
    ))
    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_SKIPPED_NEIGHBOUR_CHEAPER
    assert signal.cross_verdict in BLOCKING_VERDICTS


def test_item5_portals_signal_skipped_when_gap_below_threshold(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 5: P=17.80, N=19.00 (gap 6.7% < 10%) -> not sent."""
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("17.27") * config.NANO),
        floor_with_fee_nano=int(Decimal("19.00") * config.NANO),
        listed_count=5, status="ok", raw=[],
    ))
    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_SKIPPED_NEIGHBOUR_CHEAPER


def test_item6_portals_signal_sent_when_gap_above_threshold(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 6: P=17.80, N=25.00 (gap 40%) -> sent."""
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("22.73") * config.NANO),
        floor_with_fee_nano=int(Decimal("25.00") * config.NANO),
        listed_count=5, status="ok", raw=[],
    ))
    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER
    assert signal.cross_verdict not in BLOCKING_VERDICTS


# --- item 4/6 mirror: Tonnel signal vs. Portals neighbour, no fee --------


def test_tonnel_signal_skipped_when_portals_neighbour_cheaper(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)
    signal = _signal("tonnel", "25.70")
    portals = FakePortalsClient(response=_portals_result("10.21", "10.50", "11.00"))
    cross_check(conn, signal, portals_client=portals)
    assert signal.cross_verdict == VERDICT_SKIPPED_NEIGHBOUR_CHEAPER


def test_tonnel_signal_sent_when_portals_neighbour_higher(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)
    signal = _signal("tonnel", "17.25")
    portals = FakePortalsClient(response=_portals_result("21.90", "22.00", "23.00"))
    cross_check(conn, signal, portals_client=portals)
    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER


# --- item 7: no comparable neighbour -> send -------------------------------


def test_item7_no_tonnel_neighbour_sends(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    conn = db.connect(":memory:")
    signal = _signal("portals", "40.0")
    tonnel = FakeTonnelClient(floor=TonnelFloor(floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=[]))
    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_SENT_NO_NEIGHBOUR
    assert signal.cross_verdict not in BLOCKING_VERDICTS


def test_item7_no_portals_neighbour_sends(monkeypatch):
    """No Portals listing for this pair at all -> sent_no_neighbour."""
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)
    signal = _signal("tonnel", "40.0")
    portals = FakePortalsClient(response={"results": []})
    cross_check(conn, signal, portals_client=portals)
    assert signal.cross_verdict == VERDICT_SENT_NO_NEIGHBOUR


def test_item7_tonnel_signal_no_portals_collection_known_sends(monkeypatch):
    """No Portals listing ever seen for this collection_name -> collection_id
    lookup fails -> sent_no_neighbour, same as any other "nothing to compare"."""
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    conn = db.connect(":memory:")  # nothing seeded
    signal = _signal("tonnel", "40.0")
    portals = FakePortalsClient(response=_portals_result("10.0"))
    cross_check(conn, signal, portals_client=portals)
    assert signal.cross_verdict == VERDICT_SENT_NO_NEIGHBOUR
    assert portals.calls == []  # never even reached the network call


# --- item 8: min neighbour count -------------------------------------------


def test_item8_portals_signal_thin_neighbour_below_min_count(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 8: 1 Tonnel listing, threshold 3 ->
    neighbour_thin, treated like "no neighbour" (send)."""
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    conn = db.connect(":memory:")
    signal = _signal("portals", "21.50")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("19.55") * config.NANO),
        floor_with_fee_nano=int(Decimal("21.50") * config.NANO),  # equal -- would be "worse" if trusted
        listed_count=1, status="ok", raw=[],
    ))
    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_NEIGHBOUR_THIN
    assert signal.cross_verdict not in BLOCKING_VERDICTS


def test_item8_tonnel_signal_thin_neighbour_below_min_count(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)
    signal = _signal("tonnel", "21.50")
    portals = FakePortalsClient(response=_portals_result("21.50"))  # equal, 1 listing only
    cross_check(conn, signal, portals_client=portals)
    assert signal.cross_verdict == VERDICT_NEIGHBOUR_THIN


def test_item8_three_listings_clears_the_threshold(monkeypatch):
    """At exactly the threshold (3), the neighbour price IS used."""
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("22.73") * config.NANO),
        floor_with_fee_nano=int(Decimal("25.00") * config.NANO),
        listed_count=3, status="ok", raw=[],
    ))
    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER


# --- neighbour query failure never blocks the send -------------------------


def test_tonnel_neighbour_error_yields_error_verdict_not_blocking(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    conn = db.connect(":memory:")
    signal = _signal("portals", "40.0")
    tonnel = FakeTonnelClient(raises=TonnelError("boom"))
    cross_check(conn, signal, tonnel_client=tonnel)
    assert signal.cross_verdict == VERDICT_ERROR
    assert signal.cross_verdict not in BLOCKING_VERDICTS


def test_portals_neighbour_error_yields_error_verdict(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)
    signal = _signal("tonnel", "40.0")
    portals = FakePortalsClient(raises=PortalsError("boom"))
    cross_check(conn, signal, portals_client=portals)
    assert signal.cross_verdict == VERDICT_ERROR


# --- CROSS_CHECK_ENABLED=false -> zero neighbour queries --------------------


def test_disabled_makes_zero_neighbour_queries(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", False)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)

    tonnel = FakeTonnelClient(floor=TonnelFloor(floor_nano=1, floor_with_fee_nano=1, listed_count=1, status="ok", raw=[]))
    portals = FakePortalsClient(response=_portals_result("1.0"))

    portals_signal = _signal("portals", "40.0")
    cross_check(conn, portals_signal, tonnel_client=tonnel, portals_client=portals)
    tonnel_signal = _signal("tonnel", "40.0", listing_external_id="sig-2")
    cross_check(conn, tonnel_signal, tonnel_client=tonnel, portals_client=portals)

    assert tonnel.calls == []
    assert portals.calls == []
    assert portals_signal.cross_verdict == "not_checked"
    assert tonnel_signal.cross_verdict == "not_checked"
    assert "not_checked" not in BLOCKING_VERDICTS  # not_checked never blocks a send


# --- fee applies only when Tonnel is the neighbour --------------------------


def test_fee_applies_only_when_tonnel_is_neighbour(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 8 (spec, cross-check delivery): comparing the
    SAME raw floor number (45.0) against the SAME price (47.0) either way
    -- as a Tonnel neighbour it's fee-adjusted to 49.50 (Portals 47 <
    49.50*(1+10%)=54.45 -> skipped, gap too small); as a Portals neighbour
    there's no fee (Tonnel signal 47 vs Portals floor 45.0 unadjusted,
    47 > 45*(1.10)=49.5 is false -> skipped too, but via a DIFFERENT,
    unadjusted number -- proves no fee inflation happens on the Portals
    side).
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)

    portals_signal = _signal("portals", "47.0")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("45.0") * config.NANO),
        floor_with_fee_nano=int(Decimal("49.50") * config.NANO),
        listed_count=5, status="ok", raw=[],
    ))
    cross_check(conn, portals_signal, tonnel_client=tonnel)
    assert portals_signal.neighbour_floor_nano == int(Decimal("49.50") * config.NANO)  # fee-adjusted

    tonnel_signal = _signal("tonnel", "47.0", listing_external_id="sig-2")
    portals = FakePortalsClient(response=_portals_result("45.0", "45.5", "46.0"))
    cross_check(conn, tonnel_signal, portals_client=portals)
    assert tonnel_signal.neighbour_floor_nano == int(Decimal("45.0") * config.NANO)  # NOT fee-adjusted


# --- snapshot persistence ---------------------------------------------


def test_snapshot_written_for_both_directions(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)

    portals_signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("22.73") * config.NANO),
        floor_with_fee_nano=int(Decimal("25.00") * config.NANO),
        listed_count=5, status="ok", raw=[],
    ))
    cross_check(conn, portals_signal, tonnel_client=tonnel)

    tonnel_signal = _signal("tonnel", "17.25", listing_external_id="sig-2")
    portals = FakePortalsClient(response=_portals_result("21.90", "22.00", "23.00"))
    cross_check(conn, tonnel_signal, portals_client=portals)

    rows = db.latest_cross_check_snapshots(conn)
    by_marketplace = {r["signal_marketplace"]: r for r in rows}
    assert by_marketplace["portals"]["checked_marketplace"] == "tonnel"
    assert by_marketplace["portals"]["verdict"] == VERDICT_SENT_NEIGHBOUR_HIGHER
    assert by_marketplace["tonnel"]["checked_marketplace"] == "portals"
    assert by_marketplace["tonnel"]["verdict"] == VERDICT_SENT_NEIGHBOUR_HIGHER


# --- MRKT as a THIRD neighbour (multi-neighbour combining) ----------------


from gift_sniper.mrkt_client import MrktError, MrktFloor


class FakeMrktClient:
    def __init__(self, floor=None, raises=None, model_floor=None):
        self._floor = floor
        self._raises = raises
        self._model_floor = model_floor or MrktFloor(floor_nano=None, listed_count=0, status="no_data", raw=[])
        self.calls = []
        self.model_calls = []

    def model_floor(self, collection_name=None, model_name=None, exclude_number=None):
        self.model_calls.append({"collection_name": collection_name, "model_name": model_name})
        return self._model_floor

    def pair_floor(self, collection_name=None, model_name=None, backdrop_name=None, exclude_number=None):
        self.calls.append({
            "collection_name": collection_name, "model_name": model_name,
            "backdrop_name": backdrop_name, "exclude_number": exclude_number,
        })
        if self._raises is not None:
            raise self._raises
        return self._floor


def test_item5_portals_signal_tonnel_silent_mrkt_cheaper_blocks(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 5: Tonnel has no comparable pair (no_data),
    MRKT is cheaper -> the signal is blocked (ONE bad neighbour is enough).
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(floor_nano=None, floor_with_fee_nano=None, listed_count=0, status="no_data", raw=[]))
    mrkt = FakeMrktClient(floor=MrktFloor(floor_nano=int(Decimal("11.44") * config.NANO), listed_count=5, status="ok", raw=[]))

    cross_check(conn, signal, tonnel_client=tonnel, mrkt_client=mrkt)

    assert signal.cross_verdict == VERDICT_SKIPPED_NEIGHBOUR_CHEAPER
    assert signal.cross_verdict in BLOCKING_VERDICTS


def test_item6_portals_signal_both_neighbours_higher_sends(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 6: both Tonnel and MRKT are meaningfully
    pricier -> sent.
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("22.73") * config.NANO),
        floor_with_fee_nano=int(Decimal("25.00") * config.NANO),
        listed_count=5, status="ok", raw=[],
    ))
    mrkt = FakeMrktClient(floor=MrktFloor(floor_nano=int(Decimal("24.00") * config.NANO), listed_count=5, status="ok", raw=[]))

    cross_check(conn, signal, tonnel_client=tonnel, mrkt_client=mrkt)

    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER
    assert signal.cross_verdict not in BLOCKING_VERDICTS

    rows = db.latest_cross_check_snapshots(conn, signal_marketplace="portals")
    checked = {r["checked_marketplace"] for r in rows}
    assert checked == {"tonnel", "mrkt"}


def test_item7_mrkt_error_does_not_block_and_tonnel_still_queried(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 7: MRKT raises -> cross-check still proceeds
    via Tonnel, signal not blocked by MRKT's failure alone.
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("22.73") * config.NANO),
        floor_with_fee_nano=int(Decimal("25.00") * config.NANO),
        listed_count=5, status="ok", raw=[],
    ))
    mrkt = FakeMrktClient(raises=MrktError("boom"))

    cross_check(conn, signal, tonnel_client=tonnel, mrkt_client=mrkt)

    assert len(tonnel.calls) == 1  # Tonnel WAS queried
    assert len(mrkt.calls) == 1  # MRKT was attempted too
    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER  # Tonnel's real vote wins over MRKT's error

    rows = db.latest_cross_check_snapshots(conn, signal_marketplace="portals")
    by_checked = {r["checked_marketplace"]: r for r in rows}
    assert by_checked["mrkt"]["verdict"] == VERDICT_ERROR
    assert by_checked["tonnel"]["verdict"] == VERDICT_SENT_NEIGHBOUR_HIGHER


def test_item7_mrkt_error_alone_never_blocks_even_with_no_other_neighbour(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)

    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    mrkt = FakeMrktClient(raises=MrktError("boom"))

    cross_check(conn, signal, mrkt_client=mrkt)

    assert signal.cross_verdict == VERDICT_ERROR
    assert signal.cross_verdict not in BLOCKING_VERDICTS


def test_item8_mrkt_access_token_unset_means_no_mrkt_client_passed_at_all(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 8: when MRKT isn't configured, callers simply
    never pass an mrkt_client -- cross_check() treats mrkt_client=None as
    "skip this neighbour" (same as any other absent client), and
    everything else (Tonnel) keeps working normally.
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)

    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    tonnel = FakeTonnelClient(floor=TonnelFloor(
        floor_nano=int(Decimal("22.73") * config.NANO),
        floor_with_fee_nano=int(Decimal("25.00") * config.NANO),
        listed_count=5, status="ok", raw=[],
    ))

    cross_check(conn, signal, tonnel_client=tonnel, mrkt_client=None)

    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER
    rows = db.latest_cross_check_snapshots(conn, signal_marketplace="portals")
    assert {r["checked_marketplace"] for r in rows} == {"tonnel"}  # never even attempted mrkt


def test_mrkt_cross_check_disabled_flag_skips_mrkt_even_if_client_given(monkeypatch):
    """MRKT_CROSS_CHECK_ENABLED=false must skip MRKT even when a real
    client instance was passed in -- independent off-switch from
    CROSS_CHECK_ENABLED (which still gates Tonnel/Portals).
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", False)

    conn = db.connect(":memory:")
    signal = _signal("portals", "17.80")
    mrkt = FakeMrktClient(floor=MrktFloor(floor_nano=1, listed_count=5, status="ok", raw=[]))

    cross_check(conn, signal, mrkt_client=mrkt)

    assert mrkt.calls == []
    assert signal.cross_verdict == "not_checked"


def test_tonnel_signal_mrkt_neighbour_uses_gift_number_exclusion(monkeypatch):
    """The Tonnel/Portals->MRKT direction passes the signal's own
    gift_number as exclude_number -- a genuine self-exclusion should the
    same lot also be listed on MRKT under the same number.
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)

    conn = db.connect(":memory:")
    _seed_portals_collection(conn)
    signal = _signal("tonnel", "17.25", gift_number=555)
    portals = FakePortalsClient(response=_portals_result("21.90", "22.00", "23.00"))
    mrkt = FakeMrktClient(floor=MrktFloor(floor_nano=None, listed_count=0, status="no_data", raw=[]))

    cross_check(conn, signal, portals_client=portals, mrkt_client=mrkt)

    assert mrkt.calls[0]["exclude_number"] == 555
    assert mrkt.calls[0]["collection_name"] == "CollA"


# --- unit-conversion bug fix: real MrktClient through cross_check() -------


def test_portals_to_mrkt_cross_check_writes_snapshot_without_overflow(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 2: a Portals signal cross-checked against a
    REAL MrktClient (not the FakeMrktClient stub -- exercises the actual
    salePrice parsing) writes its snapshot without OverflowError. Uses
    the exact measured example: salePrice=16289400000 (16.29 TON).
    """
    import json as json_module
    from gift_sniper.mrkt_client import MrktClient

    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 1)

    class FakeMrktResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body
            self.text = json_module.dumps(body)

        def json(self):
            return self._body

    class FakeMrktSession:
        def post(self, url, json=None, headers=None, impersonate=None, timeout=None):
            gift = {
                "id": "uuid-1", "name": "SnoopCigar-1", "number": 999,  # != signal's gift_number=1, not self-excluded
                "collectionName": "CollA", "modelName": "M", "backdropName": "B",
                "symbolName": "Star", "salePrice": 16289400000, "salePriceWithoutFee": 15970000000,
                "isOnSale": True, "isOnAuction": False, "isLocked": False, "isLockedForSale": False,
                "salesCount": 0, "premarketStatus": "None",
                "floorPriceNanoTONsByCollection": None, "floorPriceNanoTONsByBackdropModel": None,
            }
            return FakeMrktResponse({"gifts": [gift], "cursor": "", "total": 1})

    real_mrkt_client = MrktClient(token_provider=lambda: "tok", session=FakeMrktSession(), request_delay_ms=0)

    conn = db.connect(":memory:")
    signal = _signal("portals", "10.0")  # well below 16.29 -> a real "higher neighbour" case

    cross_check(conn, signal, mrkt_client=real_mrkt_client)  # must not raise OverflowError

    rows = db.latest_cross_check_snapshots(conn, signal_marketplace="portals")
    mrkt_row = next(r for r in rows if r["checked_marketplace"] == "mrkt")
    assert mrkt_row["neighbour_floor_nano"] == 16289400000
    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER


# --- item 4: all three marketplaces' unit conversion, in one place --------


def test_all_three_marketplaces_convert_price_to_nano_correctly(monkeypatch):
    """КАК ТЕСТИРОВАТЬ item 4: Portals (decimal string), Tonnel (float),
    and MRKT (already-nano int) each reach the SAME correct nano value
    (20.00 TON/GRAM = 20_000_000_000 nano) through their own client's
    conversion path -- confirmed side by side so a regression in any one
    marketplace's conversion is caught here, not just in isolation.
    """
    from gift_sniper.mrkt_client import MrktClient
    import json as json_module

    expected_nano = int(Decimal("20.0") * config.NANO)

    # Portals: search_pair_floor's price field is a decimal STRING.
    portals_resp = {"results": [{"id": "p-1", "status": "listed", "price": "20.0"}]}
    parsed = _floor_from_response(portals_resp, exclude_external_id="nonexistent")
    assert parsed.floor_excluding_self_nano == expected_nano

    # Tonnel: pair_floor()'s price field is a JSON FLOAT.
    tonnel_client_real = _RealTonnelClientForConversionTest(price=20.0)
    tonnel_floor = tonnel_client_real.pair_floor(gift_name="X", model="M", backdrop="B")
    assert tonnel_floor.floor_nano == expected_nano

    # MRKT: salePrice is ALREADY a nano-TON int -- no multiplication.
    class FakeMrktResponse:
        def __init__(self, body):
            self.status_code = 200
            self._body = body
            self.text = json_module.dumps(body)

        def json(self):
            return self._body

    class FakeMrktSession:
        def post(self, url, json=None, headers=None, impersonate=None, timeout=None):
            gift = {
                "id": "uuid-1", "name": "X-1", "number": 1,
                "collectionName": "X", "modelName": "M", "backdropName": "B", "symbolName": None,
                "salePrice": expected_nano, "salePriceWithoutFee": int(expected_nano / 1.02),
                "isOnSale": True, "isOnAuction": False, "isLocked": False, "isLockedForSale": False,
                "salesCount": 0, "premarketStatus": "None",
                "floorPriceNanoTONsByCollection": None, "floorPriceNanoTONsByBackdropModel": None,
            }
            return FakeMrktResponse({"gifts": [gift], "cursor": "", "total": 1})

    mrkt_client = MrktClient(token_provider=lambda: "tok", session=FakeMrktSession(), request_delay_ms=0)
    mrkt_floor = mrkt_client.pair_floor("X", "M", "B")
    assert mrkt_floor.floor_nano == expected_nano


class _RealTonnelClientForConversionTest:
    """Exercises tonnel_client.TonnelClient.pair_floor()'s REAL price
    parsing (JSON float -> Decimal(str(...)) -> nano), via a fake HTTP
    session, for the side-by-side comparison above.
    """

    def __init__(self, price: float):
        self._price = price

    def pair_floor(self, gift_name, model, backdrop, exclude_gift_num=None):
        from gift_sniper.tonnel_client import TonnelClient

        class FakeTonnelResponse:
            def __init__(self, body):
                self.status_code = 200
                self._body = body
                self.text = str(body)

            def json(self):
                return self._body

        price = self._price

        class FakeTonnelSession:
            def post(self, url, json=None, headers=None, impersonate=None, timeout=None):
                item = {
                    "gift_num": 1, "gift_id": 1001, "name": "X",
                    "model": model, "backdrop": backdrop, "symbol": "Star",
                    "price": price, "status": "forsale", "asset": "TON",
                    "underLoan": False, "premarketData": None,
                }
                return FakeTonnelResponse([item])

        real_client = TonnelClient(session=FakeTonnelSession(), request_delay_ms=0)
        return real_client.pair_floor(gift_name=gift_name, model=model, backdrop=backdrop, exclude_gift_num=exclude_gift_num)


# --- ДЕФЕКТ 5 (systemic-check delivery), КАК ТЕСТИРОВАТЬ items 7/8 ---
# --- CROSS_AGREEMENT_PCT: two thin neighbours whose floors AGREE form ---
# --- their own vote, bypassing CROSS_MIN_NEIGHBOUR_COUNT. ---

def test_item7_two_agreeing_thin_neighbours_block_the_signal(monkeypatch):
    """Two neighbours, 11.44 and 12.00 (4.9% apart, both < CROSS_MIN_
    NEIGHBOUR_COUNT=3 lots on their own) -> their agreement is used as a
    vote regardless of depth; at signal price 15.00 (threshold with
    CROSS_MIN_GAP_PCT=10% -> 16.50), both floors are <= that threshold
    -> skipped_neighbour_cheaper (BLOCKED).
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    monkeypatch.setattr(config, "CROSS_AGREEMENT_PCT", Decimal("25"))

    conn = db.connect(":memory:")
    signal = _signal("portals", "15.00")
    tonnel = FakeTonnelClient(
        floor=TonnelFloor(floor_nano=int(Decimal("11.44") * config.NANO), floor_with_fee_nano=int(Decimal("11.44") * config.NANO), listed_count=1, status="ok", raw=[])
    )
    mrkt = FakeMrktClient(floor=MrktFloor(floor_nano=int(Decimal("12.00") * config.NANO), listed_count=2, status="ok", raw=[]))

    cross_check(conn, signal, tonnel_client=tonnel, mrkt_client=mrkt)

    assert signal.cross_verdict == VERDICT_SKIPPED_NEIGHBOUR_CHEAPER
    assert signal.cross_verdict in BLOCKING_VERDICTS


def test_item8_two_disagreeing_thin_neighbours_stay_neighbour_thin(monkeypatch):
    """Two neighbours, 11.44 and 25.00 (118% apart, well over CROSS_
    AGREEMENT_PCT=25%) -> no consensus, each stays its own individual
    (thin) vote -> combined verdict is neighbour_thin, NOT blocked.
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    monkeypatch.setattr(config, "CROSS_AGREEMENT_PCT", Decimal("25"))

    conn = db.connect(":memory:")
    signal = _signal("portals", "15.00")
    tonnel = FakeTonnelClient(
        floor=TonnelFloor(floor_nano=int(Decimal("11.44") * config.NANO), floor_with_fee_nano=int(Decimal("11.44") * config.NANO), listed_count=1, status="ok", raw=[])
    )
    mrkt = FakeMrktClient(floor=MrktFloor(floor_nano=int(Decimal("25.00") * config.NANO), listed_count=2, status="ok", raw=[]))

    cross_check(conn, signal, tonnel_client=tonnel, mrkt_client=mrkt)

    assert signal.cross_verdict == VERDICT_NEIGHBOUR_THIN
    assert signal.cross_verdict not in BLOCKING_VERDICTS


def test_single_thin_neighbour_alone_still_unblocked_unchanged(monkeypatch):
    """Regression guard: a SINGLE thin neighbour (no second neighbour to
    agree with) must behave exactly as before -- neighbour_thin, not
    blocked. The agreement rule must never fire off just one vote.
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", False)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    monkeypatch.setattr(config, "CROSS_AGREEMENT_PCT", Decimal("25"))

    conn = db.connect(":memory:")
    signal = _signal("portals", "15.00")
    tonnel = FakeTonnelClient(
        floor=TonnelFloor(floor_nano=int(Decimal("11.44") * config.NANO), floor_with_fee_nano=int(Decimal("11.44") * config.NANO), listed_count=1, status="ok", raw=[])
    )

    cross_check(conn, signal, tonnel_client=tonnel, mrkt_client=None)

    assert signal.cross_verdict == VERDICT_NEIGHBOUR_THIN


def test_agreeing_neighbours_can_also_produce_sent_neighbour_higher(monkeypatch):
    """Two agreeing neighbours whose price clears CROSS_MIN_GAP_PCT (not
    just close to each other, but genuinely HIGHER than the signal's own
    price by enough) -> sent_neighbour_higher, a real independent
    confirmation -- this is what earns notifier.py's checkmark.
    """
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "MRKT_CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    monkeypatch.setattr(config, "CROSS_AGREEMENT_PCT", Decimal("25"))

    conn = db.connect(":memory:")
    signal = _signal("portals", "10.00")
    tonnel = FakeTonnelClient(
        floor=TonnelFloor(floor_nano=int(Decimal("20.00") * config.NANO), floor_with_fee_nano=int(Decimal("20.00") * config.NANO), listed_count=1, status="ok", raw=[])
    )
    mrkt = FakeMrktClient(floor=MrktFloor(floor_nano=int(Decimal("21.00") * config.NANO), listed_count=2, status="ok", raw=[]))

    cross_check(conn, signal, tonnel_client=tonnel, mrkt_client=mrkt)

    assert signal.cross_verdict == VERDICT_SENT_NEIGHBOUR_HIGHER


def test_tonnel_signal_compared_at_buyer_cost_with_fee(monkeypatch):
    """Live case 2026-09-19: Bonded Ring #7588 on Tonnel at 40.25 costs
    44.28 with the 10% buyer fee; MRKT had the model from 42.13. Raw 40.25
    looked cheaper and passed as model_bound_inconclusive; at buyer cost
    the neighbour is cheaper and the signal must be blocked."""
    from gift_sniper.cross_check import _buy_cost_nano, _decide_model_bound
    signal = _signal("tonnel", "40.25")
    assert _buy_cost_nano(signal) == int(Decimal("44.275") * config.NANO)
    assert _decide_model_bound(_buy_cost_nano(signal), int(Decimal("42.13") * config.NANO)) \
        == VERDICT_SKIPPED_NEIGHBOUR_CHEAPER
    assert _buy_cost_nano(_signal("portals", "40.25")) == int(Decimal("40.25") * config.NANO)
    assert _buy_cost_nano(_signal("mrkt", "40.25")) == int(Decimal("40.25") * config.NANO)


def test_tonnel_signal_blocked_when_portals_only_5pct_above_buyer_cost(monkeypatch):
    monkeypatch.setattr(config, "CROSS_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "CROSS_MIN_GAP_PCT", Decimal("10"))
    monkeypatch.setattr(config, "CROSS_MIN_NEIGHBOUR_COUNT", 3)
    conn = db.connect(":memory:")
    _seed_portals_collection(conn)
    signal = _signal("tonnel", "17.25")  # buyer cost 18.975
    cross_check(conn, signal, portals_client=FakePortalsClient(response=_portals_result("19.90", "20.00", "21.00")))
    assert signal.cross_verdict == VERDICT_SKIPPED_NEIGHBOUR_CHEAPER
