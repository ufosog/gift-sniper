"""Turns one raw MRKT gift object (from a feed event's `gift` field, or
from /gifts/saling) into a Listing -- the SAME Listing dataclass
parsing.py/tonnel_parsing.py produce for Portals/Tonnel
(marketplace="mrkt" distinguishes the rows; see models.py / db.py).
Isolated from mrkt_poller.py so it can be unit-tested against fixtures
without any network or DB dependency, mirroring parsing.py/
tonnel_parsing.py's own isolation.

ALL facts below are confirmed by live requests (see mrkt_client.py's
module docstring for the full set), not assumed:
- Fields: id (uuid), name ("SnoopCigar-46116" -- ALREADY the tg_id,
  never constructed the way Tonnel's is), number (int),
  collectionName/modelName/backdropName/symbolName (EXACT strings, no
  rarity baked in, unlike Tonnel), salePrice (int, ALREADY nano-TON,
  NEVER multiplied), isOnSale/isOnAuction/isLocked/isLockedForSale,
  premarketStatus, salesCount.
- There is no listing-time field in the gift object itself -- listed_at
  is always None for an MRKT-sourced Listing, same discipline as Tonnel
  (see tonnel_parsing.py); first_seen_at (set by the caller) is the only
  timing signal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import Listing


class UnparseableMrktListing(Exception):
    pass


def is_purchasable_mrkt_gift(gift: dict) -> bool:
    """What mrkt_poller.py filters BEFORE writing to the DB at all --
    same usable-listing discipline as mrkt_client.pair_floor()'s own
    filter (see mrkt_client.py): auction/locked/pre-market lots are
    other trading mechanics, not an ordinary "buy now" listing.
    """
    if gift.get("isOnSale") is False:
        return False
    if gift.get("isOnAuction"):
        return False
    if gift.get("isLocked"):
        return False
    if gift.get("isLockedForSale"):
        return False
    if gift.get("premarketStatus") not in (None, "None"):
        return False
    return True


def parse_mrkt_listing(gift: dict) -> Listing:
    """Raises UnparseableMrktListing for structurally broken items
    (missing id). A missing/null salePrice is NOT an error -- becomes a
    None field, same contract as parsing.py/tonnel_parsing.py.

    Does NOT apply is_purchasable_mrkt_gift() -- that filter is the
    caller's (mrkt_poller.py's) decision, kept separate so this function
    stays a pure, total mapping from gift -> Listing, testable against
    ANY shape including ones that would be filtered.
    """
    try:
        gift_id = gift["id"]
    except KeyError as exc:
        raise UnparseableMrktListing(f"missing required field: {exc}") from exc

    price_nano: int | None = None
    if gift.get("salePrice") is not None:
        try:
            # salePrice is ALREADY nano-TON (an int, confirmed live) --
            # NO * config.NANO here, same fixed bug as mrkt_client.py's
            # pair_floor(). str() first only to avoid a float/Decimal
            # construction surprise if the API ever sends a JSON float.
            price_nano = int(Decimal(str(gift["salePrice"])))
        except InvalidOperation:
            price_nano = None

    gift_number = gift.get("number")

    return Listing(
        marketplace="mrkt",
        external_id=str(gift_id),
        tg_id=gift.get("name"),  # ALREADY a ready-made tg_id -- never constructed, see module docstring
        collection_id=None,  # MRKT has no collection_id concept, same as Tonnel -- see README
        collection_name=gift.get("collectionName"),
        gift_number=int(gift_number) if gift_number is not None else None,
        price_nano=price_nano,
        currency="TON",  # NEVER "GRAM" -- see money.py / README on not mixing currencies
        collection_floor_nano=None,  # not used -- floorPriceNanoTONsByCollection is confirmed available but unused this delivery
        model_name=gift.get("modelName"),
        symbol_name=gift.get("symbolName"),
        backdrop_name=gift.get("backdropName"),
        model_rarity_raw=None,  # MRKT names carry no rarity suffix, unlike Tonnel -- see mrkt_client.py
        symbol_rarity_raw=None,
        backdrop_rarity_raw=None,
        image_url=None,  # not present in feed/saling responses
        animation_url=None,
        listed_at=None,  # NO listing-time field exists -- see module docstring / README
        unlocks_at=None,
        status="forsale" if gift.get("isOnSale") else "unknown",
        first_seen_at=datetime.now(timezone.utc),
        raw=gift,
    )
