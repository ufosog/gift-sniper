from decimal import Decimal

import pytest

from gift_sniper.tonnel_parsing import (
    UnparseableTonnelListing,
    is_purchasable_tonnel_item,
    parse_tonnel_listing,
)


def _item(**overrides) -> dict:
    base = dict(
        gift_id=987654,
        gift_num=45374,
        name="Ice Cream",
        model="Fried Chicken (1.5%)",
        backdrop="Onyx Black (3.2%)",
        symbol="Star (0.5%)",
        price=4.08,
        status="forsale",
        asset="TON",
        underLoan=False,
        premarketData=None,
        auction=None,
        dutchAuctionData=None,
        export_at=1234567890,
    )
    base.update(overrides)
    return base


def test_parse_extracts_rarity_from_trait_names():
    """КАК ТЕСТИРОВАТЬ item 1: "Fried Chicken (1.5%)" -> model_name
    "Fried Chicken", model_rarity_raw Decimal("1.5").
    """
    listing = parse_tonnel_listing(_item())
    assert listing.model_name == "Fried Chicken"
    assert listing.model_rarity_raw == Decimal("1.5")
    assert listing.backdrop_name == "Onyx Black"
    assert listing.backdrop_rarity_raw == Decimal("3.2")
    assert listing.symbol_name == "Star"
    assert listing.symbol_rarity_raw == Decimal("0.5")


def test_parse_sets_marketplace_tonnel_and_external_id_from_gift_id():
    listing = parse_tonnel_listing(_item(gift_id=555))
    assert listing.marketplace == "tonnel"
    assert listing.external_id == "555"


def test_parse_tg_id_construction_rule():
    """Правка 2: <NameБезПробелов>-<gift_num>, by analogy with Portals'
    confirmed rule -- NOT confirmed for Tonnel, see tonnel_tgid_probe.py.
    """
    listing = parse_tonnel_listing(_item(name="Ice Cream", gift_num=45374))
    assert listing.tg_id == "IceCream-45374"


def test_parse_tg_id_confirmed_examples():
    """Confirmed live (8/8 lots found at t.me/nft/<tg_id>): the no-space
    concatenation rule is no longer a guess for Tonnel.
    """
    assert parse_tonnel_listing(_item(name="Skull Flower", gift_num=8238)).tg_id == "SkullFlower-8238"
    assert parse_tonnel_listing(_item(name="Vice Cream", gift_num=427292)).tg_id == "ViceCream-427292"
    assert parse_tonnel_listing(_item(name="Desk Calendar", gift_num=175454)).tg_id == "DeskCalendar-175454"


def test_parse_tg_id_strips_apostrophes():
    """Апострофы (' и ’) удаляются, как в Portals -- per spec."""
    assert parse_tonnel_listing(_item(name="Durov's Cap", gift_num=1)).tg_id == "DurovsCap-1"
    assert parse_tonnel_listing(_item(name="Durov’s Cap", gift_num=2)).tg_id == "DurovsCap-2"


def test_parse_tg_id_none_when_name_or_gift_num_missing():
    assert parse_tonnel_listing(_item(name=None)).tg_id is None
    assert parse_tonnel_listing(_item(gift_num=None)).tg_id is None


def test_parse_price_no_fee_added_and_currency_ton():
    """price_nano is the RAW price (no 10% buyer fee) -- fee conversion
    happens at the point of comparison, never in the parser. currency is
    always "TON", never "GRAM".
    """
    listing = parse_tonnel_listing(_item(price=4.08))
    assert listing.price_nano == int(Decimal("4.08") * 10**9)
    assert listing.currency == "TON"


def test_parse_listed_at_is_always_none():
    """No listing-time field exists in the Tonnel response -- export_at
    is mint-eligibility time, not listing time. listed_at must never be
    derived from it.
    """
    listing = parse_tonnel_listing(_item(export_at=999999))
    assert listing.listed_at is None


def test_parse_collection_id_is_none():
    """Tonnel has no collection_id concept."""
    listing = parse_tonnel_listing(_item())
    assert listing.collection_id is None


def test_parse_missing_gift_id_raises():
    item = _item()
    del item["gift_id"]
    with pytest.raises(UnparseableTonnelListing):
        parse_tonnel_listing(item)


def test_parse_missing_price_is_none_not_error():
    listing = parse_tonnel_listing(_item(price=None))
    assert listing.price_nano is None


def test_parse_trait_without_rarity_suffix_falls_back_to_raw_name():
    listing = parse_tonnel_listing(_item(model="PlainModel"))
    assert listing.model_name == "PlainModel"
    assert listing.model_rarity_raw is None


def test_parse_raw_stores_whole_item():
    item = _item()
    listing = parse_tonnel_listing(item)
    assert listing.raw == item


# --- is_purchasable_tonnel_item -----------------------------------------


def test_is_purchasable_true_for_ordinary_forsale_item():
    """КАК ТЕСТИРОВАТЬ item: an ordinary forsale item is purchasable."""
    assert is_purchasable_tonnel_item(_item()) is True


def test_is_purchasable_false_when_under_loan():
    """КАК ТЕСТИРОВАТЬ item 2: underLoan=true -> not purchasable."""
    assert is_purchasable_tonnel_item(_item(underLoan=True)) is False


def test_is_purchasable_false_when_premarket_data_present():
    """КАК ТЕСТИРОВАТЬ item 3: non-empty premarketData -> not purchasable."""
    assert is_purchasable_tonnel_item(_item(premarketData={"foo": "bar"})) is False


def test_is_purchasable_false_when_auction():
    assert is_purchasable_tonnel_item(_item(auction={"end": 123})) is False


def test_is_purchasable_false_when_dutch_auction_data():
    assert is_purchasable_tonnel_item(_item(dutchAuctionData={"start_price": 10})) is False
