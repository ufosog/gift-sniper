"""Feed without auth: is it the same newest-first stream? Fetch real and
no-header pages back to back 5 times; compare overlap of ids and listed_at."""
import os, time
from gift_sniper import config
from gift_sniper.portals_client import PortalsClient
class NoAuth(PortalsClient):
    def _headers(self): return dict(config.HEADERS_STATIC)
real = PortalsClient(auth_provider=lambda: os.environ.get("PORTALS_AUTH", ""))
noa = NoAuth(auth_provider=lambda: "")
for i in range(5):
    a = real.search(limit=50, offset=0)["results"]; b = noa.search(limit=50, offset=0)["results"]
    ia, ib = {x["id"] for x in a}, {x["id"] for x in b}
    print(f"try{i}: real n={len(a)} newest={a[0].get('listed_at')} | noauth n={len(b)} newest={b[0].get('listed_at')} | overlap={len(ia & ib)}")
    time.sleep(2)
