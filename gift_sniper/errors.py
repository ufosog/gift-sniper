class PortalsError(Exception):
    """Base class. Each subclass is a distinct error CATEGORY that must be
    logged and handled differently -- never collapse these into one
    generic 'request failed' branch.
    """


class AuthInvalid(PortalsError):
    """401/403 with a JSON body: the token itself is invalid/expired."""


class WafBlock(PortalsError):
    """403 with an HTML body, or cf-ray / server: cloudflare headers:
    the problem is missing/wrong Origin-Referer-User-Agent, not the token.
    """


class WafReset(PortalsError):
    """Connection reset on an endpoint that previously responded fine.
    Must NOT be treated as a transient network error / lumped into the
    ordinary retry path.
    """


class RateLimited(PortalsError):
    def __init__(self, headers: dict[str, str]):
        super().__init__("429 rate limited")
        self.headers = headers


class TransientError(PortalsError):
    """5xx / timeout / plain connection error -- worth a bounded retry."""
