"""Every Portals endpoint the project uses, called with: the real token,
no Authorization header at all, and an expired/garbage token. Compares
response content (ids, prices), not just status. Never prints the token."""
import os, json
import requests
from gift_sniper import config
from gift_sniper.portals_client import PortalsClient

real = os.environ.get("PORTALS_AUTH", "")

class NoAuth(PortalsClient):
    def _headers(self):
        return dict(config.HEADERS_STATIC)

clients = {
    "real": PortalsClient(auth_provider=lambda: real),
    "noheader": NoAuth(auth_provider=lambda: ""),
    "garbage": PortalsClient(auth_provider=lambda: "query_id=x&user=%7B%7D&auth_date=1&hash=00"),
}
page = clients["real"].search(limit=5, offset=0) if hasattr(clients["real"], "search") else None
item = page["results"][0]
cid, model, backdrop, eid = item["collection_id"], None, None, item["id"]
for a in item.get("attributes", []):
    if a.get("type") == "model": model = a["value"]
    if a.get("type") == "backdrop": backdrop = a["value"]
print("probe lot", item.get("name"), model, backdrop)

def sig(r):
    if isinstance(r, dict) and "results" in r:
        return [(x.get("id"), x.get("price"), x.get("status")) for x in r["results"]][:5]
    return json.dumps(r, sort_keys=True)[:200]

calls = {
    "search":          lambda c: c.search(limit=5, offset=0),
    "search_by_ids":   lambda c: c.search_by_ids([eid]),
    "pair_floor":      lambda c: c.search_pair_floor(cid, model, backdrop),
    "model_floor":     lambda c: c.search_model_floor(cid, model),
    "model_bg_floors": lambda c: c.model_backgrounds_floors([model]),
    "market_config":   lambda c: c.market_config(),
}
for name, call in calls.items():
    res = {}
    for label, c in clients.items():
        try:
            res[label] = sig(call(c))
        except Exception as e:
            res[label] = f"ERR {type(e).__name__}: {str(e)[:80]}"
    same = res["real"] == res["noheader"] == res["garbage"]
    print(f"{name:16} same_content={same}")
    if not same:
        for k, v in res.items(): print("   ", k, str(v)[:220])
