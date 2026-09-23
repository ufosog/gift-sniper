"""Turns one raw Tonnel pageGifts result item into a Listing -- the SAME
Listing dataclass parsing.py produces for Portals (marketplace="tonnel"
distinguishes the rows; see models.py / db.py schema v13). Isolated from
tonnel_poller.py so it can be unit-tested against fixtures without any
network or DB dependency, mirroring parsing.py's own isolation.

ALL facts below are confirmed by live requests (see tonnel_client.py's
module docstring for the full set), not assumed:
- Response fields: gift_num (int), name (str, collection), model/
  backdrop/symbol (str, WITH rarity baked in, e.g. "Fried Chicken
  (1.5%)"), price (float, no fee), gift_id (int, internal Tonnel id),
  status ("forsale"), asset ("TON"), limited, underLoan, premarketData,
  dutchAuctionData, auction, export_at.
- Tonnel adds a 10% BUYER fee on top of `price` -- stored as-is here
  (price_nano is the RAW price, no fee added); fee conversion happens at
  the point of comparison (tonnel_client.py / signals.py), never here.
- There is NO listing-time field in the response. export_at is the time
  the gift becomes MINTABLE, not when it was listed for sale -- using it
  as listed_at would be wrong, not just imprecise. listed_at is always
  None for a Tonnel-sourced Listing; first_seen_at (set by the caller,
  same as Portals) is the only timing signal available. See README.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .models import Listing

_TRAIT_RARITY_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<rarity>[\d.]+)%\)\s*$")


class UnparseableTonnelListing(Exception):
    pass


def _split_trait(raw: str | None) -> tuple[str | None, Decimal | None]:
    """"Fried Chicken (1.5%)" -> ("Fried Chicken", Decimal("1.5")).
    A trait string with no rarity suffix is returned as-is with a None
    rarity -- Tonnel is confirmed to always include it on real listings,
    but this must never raise on an unexpected shape (a defensive
    fallback, not the expected path).
    """
    if not raw:
        return None, None
    m = _TRAIT_RARITY_RE.match(raw)
    if not m:
        return raw, None
    name = m.group("name")
    try:
        rarity = Decimal(m.group("rarity"))
    except InvalidOperation:
        return name, None
    return name, rarity


def is_purchasable_tonnel_item(item: dict) -> bool:
    """Правка 2: what tonnel_poller.py filters BEFORE writing to the DB
    at all -- these are other trading mechanics (loan collateral,
    pre-market, auction), not an ordinary "buy now" listing, mirroring
    tonnel_client.pair_floor()'s own usable-listing filter.
    """
    if item.get("underLoan"):
        return False
    if item.get("premarketData"):
        return False
    if item.get("auction"):
        return False
    if item.get("dutchAuctionData"):
        return False
    return True


def _tg_id_for(collection_name: str | None, gift_num: int | None) -> str | None:
    """<NameБезПробелов>-<gift_num> -- CONFIRMED live for Tonnel (8/8
    lots checked via tonnel_tgid_probe.py, e.g. "Skull Flower" #8238 ->
    "SkullFlower-8238", "Vice Cream" #427292 -> "ViceCream-427292",
    "Desk Calendar" #175454 -> "DeskCalendar-175454"), same rule as
    Portals (see parsing.py). Apostrophes (both ' and the curly ’) are
    also stripped, same treatment as spaces -- per spec, matching the
    Portals convention (none of the 8 confirmed examples happened to
    contain one, so this specific piece isn't independently verified,
    only applied by the same rule). Returns None if either input is
    missing -- never fabricates a partial id.
    """
    if not collection_name or gift_num is None:
        return None
    cleaned = collection_name.replace(" ", "").replace("'", "").replace("’", "")
    return f"{cleaned}-{gift_num}"


def parse_tonnel_listing(item: dict) -> Listing:
    """Raises UnparseableTonnelListing for structurally broken items
    (missing gift_id). A missing/null price is NOT an error -- becomes a
    None field, same contract as parsing.py.

    Does NOT apply is_purchasable_tonnel_item() -- that filter is the
    caller's (tonnel_poller.py's) decision, kept separate so this
    function stays a pure, total mapping from item -> Listing, testable
    against ANY item shape including ones that would be filtered.
    """
    try:
        gift_id = item["gift_id"]
    except KeyError as exc:
        raise UnparseableTonnelListing(f"missing required field: {exc}") from exc

    gift_num = item.get("gift_num")
    collection_name = item.get("name")

    price_nano: int | None = None
    if item.get("price") is not None:
        try:
            # str() first: item["price"] is a JSON float (already
            # binary-imprecise by the time it reaches us) -- routing it
            # through str() avoids compounding that with a second,
            # avoidable float->Decimal conversion artifact (same
            # discipline as tonnel_client.py's pair_floor()).
            price_nano = int(Decimal(str(item["price"])) * 10**9)
        except InvalidOperation:
            price_nano = None

    model_name, model_rarity_raw = _split_trait(item.get("model"))
    backdrop_name, backdrop_rarity_raw = _split_trait(item.get("backdrop"))
    symbol_name, symbol_rarity_raw = _split_trait(item.get("symbol"))

    return Listing(
        marketplace="tonnel",
        external_id=str(gift_id),
        tg_id=_tg_id_for(collection_name, gift_num),
        collection_id=None,  # Tonnel has no collection_id concept -- see README
        collection_name=collection_name,
        gift_number=int(gift_num) if gift_num is not None else None,
        price_nano=price_nano,
        currency="TON",  # NEVER "GRAM" -- see money.py / README on not mixing currencies
        collection_floor_nano=None,  # Tonnel has no equivalent of Portals' floor_price field
        model_name=model_name,
        symbol_name=symbol_name,
        backdrop_name=backdrop_name,
        model_rarity_raw=model_rarity_raw,
        symbol_rarity_raw=symbol_rarity_raw,
        backdrop_rarity_raw=backdrop_rarity_raw,
        image_url=None,  # not present in pageGifts responses
        animation_url=None,
        listed_at=None,  # NO listing-time field exists -- see module docstring / README
        unlocks_at=None,
        status=item.get("status", "unknown"),
        first_seen_at=datetime.now(timezone.utc),
        raw=item,
    )
