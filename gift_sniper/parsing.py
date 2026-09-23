"""Turns one raw /nfts/search result item into a Listing. Isolated from
poller.py so it can be unit-tested against fixtures without any network
or DB dependency.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import config
from .models import Listing
from .money import to_nano

logger = logging.getLogger("gift_sniper.parsing")


class UnparseableListing(Exception):
    pass


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _decimal_or_none(raw) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def parse_listing(item: dict) -> Listing:
    """Raises UnparseableListing for structurally broken items (missing
    required identity fields). A missing/null price or a missing trait is
    NOT an error -- those become None fields, per contract.
    """
    try:
        external_id = item["id"]
        collection_id = item["collection_id"]
    except KeyError as exc:
        raise UnparseableListing(f"missing required field: {exc}") from exc

    attrs = {a.get("type"): a for a in item.get("attributes", []) if isinstance(a, dict)}

    def attr_value(kind: str) -> str | None:
        a = attrs.get(kind)
        return a.get("value") if a else None

    def attr_rarity(kind: str) -> Decimal | None:
        a = attrs.get(kind)
        return _decimal_or_none(a.get("rarity_per_mille")) if a else None

    price_nano = to_nano(item.get("price"))
    floor_nano = to_nano(item.get("floor_price"))

    tg_id = item.get("tg_id")
    collection_name = item.get("name")
    gift_number = item.get("external_collection_number")
    if tg_id is None:
        # Do NOT construct a guessed value. Observed live: name="Ice Cream"
        # -> tg_id="IceCream-45374", name="Pretty Posy" -> "PrettyPosy-57354"
        # (spaces stripped; apostrophe/other-punctuation rule unknown). A
        # wrong guess produces a broken deep link, which is worse than a
        # missing one.
        logger.warning("tg_id missing in response, external_id=%s", item.get("id"))

    return Listing(
        marketplace="portals",
        external_id=str(external_id),
        tg_id=tg_id,
        collection_id=str(collection_id),
        collection_name=collection_name,
        gift_number=int(gift_number) if gift_number is not None else None,
        price_nano=price_nano,
        currency=item.get("currency") or config.CURRENCY_DEFAULT,
        collection_floor_nano=floor_nano,
        model_name=attr_value("model"),
        symbol_name=attr_value("symbol"),
        backdrop_name=attr_value("backdrop"),
        model_rarity_raw=attr_rarity("model"),
        symbol_rarity_raw=attr_rarity("symbol"),
        backdrop_rarity_raw=attr_rarity("backdrop"),
        image_url=item.get("photo_url"),
        animation_url=item.get("animation_url"),
        listed_at=_parse_dt(item.get("listed_at")),
        unlocks_at=_parse_dt(item.get("unlocks_at")),
        status=item.get("status", "unknown"),
        first_seen_at=datetime.now(timezone.utc),
        raw=item,
    )
