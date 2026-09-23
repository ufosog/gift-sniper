from gift_sniper.floors import FloorCache
from .conftest import load_fixture


class ClientReturningFixedFloors:
    def __init__(self, response: dict):
        self._response = response

    def model_backgrounds_floors(self, model_names):
        return self._response


def test_missing_backdrop_in_model_block_is_none_not_error():
    response = load_fixture("floors_response_missing_backdrop.json")
    client = ClientReturningFixedFloors(response)
    cache = FloorCache(client, ttl_sec=600)

    cache.warm({"Inception"})
    snapshot = cache.snapshot_for("listing-x", "Inception", "Onyx Black")  # not in block

    assert snapshot.api_combo_floor_nano is None
    assert snapshot.model_min_floor_nano is not None  # min across Copper/Emerald exists
