"""Decimal-only price handling. float is forbidden anywhere in this chain:
API prices arrive as decimal strings with float artifacts like
"4.079999995" and must never pass through a Python float.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .config import NANO


def to_nano(raw: str | Decimal | None) -> int | None:
    """Convert a decimal-string (or Decimal) ordinary-unit price to an
    integer nano amount. Returns None for missing/unparseable input --
    that is a valid, expected case (e.g. price: null), not an error the
    caller should crash on.
    """
    if raw is None:
        return None
    try:
        d = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return int(d * NANO)
