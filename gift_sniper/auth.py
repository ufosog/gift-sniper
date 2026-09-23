"""authData storage and invalidity tracking. No refresh/Pyrogram logic --
that is an explicitly separate future phase. authData NEVER appears in
logging output, not even partially, not even at DEBUG level.
"""
from __future__ import annotations

import logging

from . import config

logger = logging.getLogger("gift_sniper.auth")


class AuthManager:
    def __init__(self, initial_token: str | None = None):
        # None: anonymous requests (Portals' read endpoints need no token,
        # see config.get_portals_auth).
        self._token = initial_token or config.get_portals_auth()
        self._valid = True

    def get(self) -> str:
        return self._token or ""

    def mark_invalid(self) -> None:
        self._valid = False
        # Deliberately no token value in this log line.
        if self._token:
            logger.critical("PORTALS_AUTH marked invalid; manual refresh required")
        else:
            logger.critical("Portals rejected an anonymous request (401/403): "
                            "it now requires auth, set PORTALS_AUTH")

    @property
    def is_valid(self) -> bool:
        return self._valid
