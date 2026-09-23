from decimal import Decimal

from gift_sniper.config import NANO
from gift_sniper.parsing import parse_listing
from .conftest import load_fixture


def test_full_listing():
    item = load_fixture("listing_full.json")
    listing = parse_listing(item)
    assert listing.external_id == "a1b2c3d4-0001"
    assert listing.tg_id == "IceCream-45374"
    assert listing.model_name == "Inception"
    assert listing.backdrop_name == "Onyx Black"
    assert listing.symbol_name == "Royal Crown"
    assert listing.model_rarity_raw == Decimal("1.5")
    assert listing.price_nano == int(Decimal("17.99") * NANO)
    assert listing.collection_floor_nano == int(Decimal("22.49") * NANO)
    assert listing.currency  # falls back to CURRENCY_DEFAULT, never empty


def test_listing_without_symbol():
    item = load_fixture("listing_no_symbol.json")
    listing = parse_listing(item)
    assert listing.symbol_name is None
    assert listing.symbol_rarity_raw is None
    assert listing.model_name == "Emperor"
    assert listing.backdrop_name == "Copper"


def test_price_with_float_artifact_stays_exact():
    item = load_fixture("listing_price_artifact.json")
    listing = parse_listing(item)
    # Decimal("4.079999995") * 10**9 must be exact -- no float rounding drift.
    assert listing.price_nano == 4079999995
    assert listing.collection_floor_nano == 6367346940


def test_null_price_is_none_not_error():
    item = load_fixture("listing_price_null.json")
    listing = parse_listing(item)
    assert listing.price_nano is None
    assert listing.collection_floor_nano == int(Decimal("8.0") * NANO)


def test_missing_tg_id_is_none_not_guessed():
    item = load_fixture("listing_full.json")
    item = dict(item)
    del item["tg_id"]
    listing = parse_listing(item)
    assert listing.tg_id is None
