"""Is a lot still buyable at the signal price? Interpretation of one
lookup response per marketplace, shared by the pollers' pre-send
freshness checks and the paper journal's execution check -- the network
call and its error handling stay with each caller.

`*_state` functions return a LotState: listed with its current price,
gone (not in the response), not_for_sale (present but not buyable), or
None (response unusable -- never treated as gone).

`*_available` functions return True (listed, same price), False (gone,
not for sale, or repriced) or None (response unusable).
"""
from __future__ import annotations

from dataclasses import dataclass

from .money import to_nano

LISTED = "listed"
GONE = "gone"
NOT_FOR_SALE = "not_for_sale"


@dataclass(frozen=True)
class LotState:
    state: str  # LISTED | GONE | NOT_FOR_SALE
    price_nano: int | None = None


def portals_state(resp: dict) -> LotState | None:
    """`resp`: PortalsClient.search_by_ids([external_id])."""
    results = resp.get("results", [])
    if not results:
        return LotState(GONE)
    item = results[0]
    if item.get("status") != "listed":
        return LotState(NOT_FOR_SALE)
    current = to_nano(item.get("price"))
    return None if current is None else LotState(LISTED, current)


def tonnel_state(results: list[dict]) -> LotState | None:
    """`results`: TonnelClient.search_minimal_by_gift_ids([gift_id], limit=1)."""
    if not results:
        return LotState(GONE)
    item = results[0]
    if item.get("status") != "forsale":
        return LotState(NOT_FOR_SALE)
    current = to_nano(item.get("price"))
    return None if current is None else LotState(LISTED, current)


def mrkt_state(item: dict | None) -> LotState | None:
    """`item`: MrktClient.find_by_number(collection_name, number)."""
    if item is None:
        return LotState(GONE)
    if item.get("isOnSale") is False or item.get("isOnAuction") or item.get("isLocked"):
        return LotState(NOT_FOR_SALE)
    try:
        return LotState(LISTED, int(item["salePrice"]))
    except (KeyError, TypeError, ValueError):
        return None


def _same_price(state: LotState | None, price_nano: int) -> bool | None:
    if state is None:
        return None
    return state.state == LISTED and state.price_nano == price_nano


def portals_available(resp: dict, price_nano: int) -> bool:
    return bool(_same_price(portals_state(resp), price_nano))


def tonnel_available(results: list[dict], price_nano: int) -> bool:
    return bool(_same_price(tonnel_state(results), price_nano))


def mrkt_available(item: dict | None, price_nano: int) -> bool | None:
    return _same_price(mrkt_state(item), price_nano)
