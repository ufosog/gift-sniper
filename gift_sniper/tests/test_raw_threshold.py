from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


def _mk_item(ext_id: str, price: str) -> dict:
    return {
        "id": ext_id,
        "tg_id": f"{ext_id}-tg",
        "collection_id": "col-x",
        "name": "Collection X",
        "external_collection_number": 1,
        "price": price,
        "floor_price": "9.0",
        "photo_url": None,
        "animation_url": None,
        "listed_at": "2026-09-05T10:00:00Z",
        "unlocks_at": None,
        "status": "listed",
        "attributes": [
            {"type": "model", "value": "M", "rarity_per_mille": 1.0},
            {"type": "backdrop", "value": "Copper", "rarity_per_mille": 1.0},
        ],
    }


def test_raw_null_below_threshold_present_above_threshold(monkeypatch):
    # This is about FLOOR_MIN_PRICE's raw-nulling behavior specifically;
    # disable the independent COLLECT_MIN_PRICE filter so the cheap
    # listing still reaches the DB (with raw=None) instead of being
    # dropped entirely before this logic even runs.
    monkeypatch.setattr(config, "COLLECT_MIN_PRICE_NANO", 0)

    cheap = _mk_item("cheap-1", "5")   # below FLOOR_MIN_PRICE
    pricey = _mk_item("pricey-1", "20")  # above threshold

    conn = db.connect(":memory:")
    client = FakePortalsClient(pages=[[cheap, pricey], []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    poller = Poller(conn, client, auth, floor_cache)

    poller.poll_once()

    rows = {r[0]: r[1] for r in conn.execute("SELECT external_id, raw FROM listings")}
    assert rows["cheap-1"] is None
    assert rows["pricey-1"] is not None

    # Parsed fields are present for BOTH regardless of raw.
    parsed = {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT external_id, price_nano, model_name FROM listings")
    }
    assert parsed["cheap-1"][0] is not None
    assert parsed["cheap-1"][1] == "M"
    assert parsed["pricey-1"][0] is not None
    assert parsed["pricey-1"][1] == "M"
