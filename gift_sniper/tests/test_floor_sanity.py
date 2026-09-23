from decimal import Decimal

from gift_sniper import config
from gift_sniper.poller import _compute_floor_sanity


def test_suspect_when_api_floor_far_above_collection_floor():
    collection_floor_nano = int(Decimal("9.35") * config.NANO)
    api_combo_floor_nano = int(Decimal("200.0") * config.NANO)
    assert config.FLOOR_SANITY_MAX_RATIO == 8
    assert _compute_floor_sanity(collection_floor_nano, api_combo_floor_nano) == "suspect"


def test_ok_when_api_floor_within_ratio():
    collection_floor_nano = int(Decimal("4.39") * config.NANO)
    api_combo_floor_nano = int(Decimal("20") * config.NANO)
    assert _compute_floor_sanity(collection_floor_nano, api_combo_floor_nano) == "ok"


def test_no_data_when_either_missing():
    assert _compute_floor_sanity(None, 100) == "no_data"
    assert _compute_floor_sanity(100, None) == "no_data"
    assert _compute_floor_sanity(None, None) == "no_data"
