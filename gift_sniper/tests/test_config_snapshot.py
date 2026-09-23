from gift_sniper import config, db
from gift_sniper.auth import AuthManager
from gift_sniper.floors import FloorCache
from gift_sniper.poller import Poller
from .fakes import FakePortalsClient


class ConfigVaryingClient(FakePortalsClient):
    def __init__(self, configs: list[dict], **kwargs):
        super().__init__(**kwargs)
        self._configs = list(configs)

    def market_config(self):
        return self._configs.pop(0)


def _poller_with_configs(configs):
    conn = db.connect(":memory:")
    client = ConfigVaryingClient(configs, pages=[[], [], [], []])
    auth = AuthManager()
    floor_cache = FloorCache(client, ttl_sec=600)
    poller = Poller(conn, client, auth, floor_cache)
    return conn, poller


def test_changed_config_writes_two_rows():
    configs = [
        {"commission": "0.02", "user_cashback": "0.05", "usd_course": "1.42",
         "offer_fee": "0.01", "withdrawal_fee": "0.35"},
        {"commission": "0.02", "user_cashback": "0",  "usd_course": "1.42",
         "offer_fee": "0.01", "withdrawal_fee": "0.35"},
    ]
    conn, poller = _poller_with_configs(configs)

    # force refresh both times regardless of CONFIG_REFRESH_SEC
    poller._last_config_fetch_mono = None
    poller.maybe_refresh_market_config()
    poller._last_config_fetch_mono = None
    poller.maybe_refresh_market_config()

    rows = conn.execute("SELECT COUNT(*) FROM market_config_snapshots").fetchone()[0]
    assert rows == 2


def test_unchanged_config_writes_one_row():
    same = {"commission": "0.02", "user_cashback": "0.05", "usd_course": "1.42",
            "offer_fee": "0.01", "withdrawal_fee": "0.35"}
    configs = [dict(same), dict(same)]
    conn, poller = _poller_with_configs(configs)

    poller._last_config_fetch_mono = None
    poller.maybe_refresh_market_config()
    poller._last_config_fetch_mono = None
    poller.maybe_refresh_market_config()

    rows = conn.execute("SELECT COUNT(*) FROM market_config_snapshots").fetchone()[0]
    assert rows == 1
