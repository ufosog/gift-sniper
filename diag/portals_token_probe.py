"""Does each Portals endpoint need auth, and is the current token accepted?
Compares: real token, garbage token, no token. Prints token age from
auth_date. Never prints the token."""
import os
from datetime import datetime, timezone
from gift_sniper.portals_client import PortalsClient
from gift_sniper.health import portals_token_age_hours

tok = os.environ.get("PORTALS_AUTH")
print("token age h:", portals_token_age_hours(tok, datetime.now(timezone.utc)))
for label, t in (("real", tok), ("garbage", "tma query_id=x&user=%7B%7D&auth_date=1&hash=00"), ("empty", "")):
    c = PortalsClient(auth_provider=lambda t=t: t)
    for name, call in (("market_config", lambda: c.market_config()),
                       ("search", lambda: c._get("/nfts/search", ordered_params=[("limit", "1"), ("offset", "0")]))):
        try:
            r = call()
            print(label, name, "OK", type(r).__name__, len(r) if hasattr(r, "__len__") else "")
        except Exception as e:
            print(label, name, "ERR", type(e).__name__, str(e)[:100])
