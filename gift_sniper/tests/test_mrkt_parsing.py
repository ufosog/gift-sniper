from gift_sniper.mrkt_parsing import (
    UnparseableMrktListing,
    is_purchasable_mrkt_gift,
    parse_mrkt_listing,
)


def _gift(**overrides):
    base = dict(
        id="4c667e31-e667-40ed-a41d-641791998bb9",
        name="SnoopCigar-46116",
        number=46116,
        collectionName="Snoop Cigar",
        modelName="Classic",
        backdropName="Black",
        symbolName="Star",
        salePrice=16289400000,
        salePriceWithoutFee=15970000000,
        isOnSale=True,
        isOnAuction=False,
        isLocked=False,
        isLockedForSale=False,
        salesCount=0,
        premarketStatus="None",
        floorPriceNanoTONsByCollection=None,
        floorPriceNanoTONsByBackdropModel=None,
    )
    base.update(overrides)
    return base


def test_parse_mrkt_listing_tg_id_taken_as_is():
    """КАК ТЕСТИРОВАТЬ item 2: tg_id comes straight from gift.name, no
    construction."""
    listing = parse_mrkt_listing(_gift())
    assert listing.tg_id == "SnoopCigar-46116"
    assert listing.marketplace == "mrkt"
    assert listing.external_id == "4c667e31-e667-40ed-a41d-641791998bb9"


def test_parse_mrkt_listing_price_not_multiplied():
    """КАК ТЕСТИРОВАТЬ item 7: salePrice is already nano-TON."""
    listing = parse_mrkt_listing(_gift(salePrice=16289400000))
    assert listing.price_nano == 16289400000


def test_parse_mrkt_listing_fields_map_directly_no_rarity_stripping():
    listing = parse_mrkt_listing(_gift())
    assert listing.collection_name == "Snoop Cigar"
    assert listing.model_name == "Classic"
    assert listing.backdrop_name == "Black"
    assert listing.symbol_name == "Star"
    assert listing.gift_number == 46116
    assert listing.currency == "TON"
    assert listing.collection_id is None


def test_parse_mrkt_listing_missing_id_raises():
    gift = _gift()
    del gift["id"]
    try:
        parse_mrkt_listing(gift)
        assert False, "must raise"
    except UnparseableMrktListing:
        pass


def test_parse_mrkt_listing_null_price_becomes_none():
    listing = parse_mrkt_listing(_gift(salePrice=None))
    assert listing.price_nano is None


def test_is_purchasable_excludes_auction_locked_premarket_not_on_sale():
    assert is_purchasable_mrkt_gift(_gift()) is True
    assert is_purchasable_mrkt_gift(_gift(isOnAuction=True)) is False
    assert is_purchasable_mrkt_gift(_gift(isLocked=True)) is False
    assert is_purchasable_mrkt_gift(_gift(isLockedForSale=True)) is False
    assert is_purchasable_mrkt_gift(_gift(premarketStatus="Active")) is False
    assert is_purchasable_mrkt_gift(_gift(isOnSale=False)) is False
