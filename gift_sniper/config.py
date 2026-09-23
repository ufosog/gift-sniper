"""All runtime configuration comes from environment variables. No secrets
or magic numbers live in code outside this file.

Naming rule (applies project-wide, not just here):
  - suffix "_NANO"  = integer, nano-units (x10**9), safe to use in arithmetic
                       directly against price_nano / combo_floor_nano.
  - no suffix        = value in ordinary currency units, as it came from env
                       or from the API. MUST be converted before it touches
                       any arithmetic involving *_nano fields.
  - exception: MARKETPLACE_FEE_RATE is a dimensionless ratio (0.02), never
    scaled by NANO.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation

NANO = 10**9

logger = logging.getLogger("gift_sniper.config")


class ConfigError(RuntimeError):
    pass


# The project directory (the package's parent). Every default path below
# is anchored here, never to the caller's working directory.
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and (val is None or val == ""):
        raise ConfigError(f"Missing required env var: {name}")
    return val


def _decimal_env(name: str, default: str) -> Decimal:
    raw = _env(name, default)
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"Env var {name}={raw!r} is not a valid decimal") from exc


# --- required, but LAZILY -- see get_portals_auth() below ---
# NOT read at import time: report.py (and anything else that only reads
# the DB) has no business touching the network and must not be forced to
# have a token just to import this module. auth.AuthManager calls
# get_portals_auth() at the point it actually needs the value (client
# construction), not here.
# Bot commands are served by ONE process only (Telegram getUpdates allows a
# single consumer). The Portals poller serves them unless the supervisor
# runs it: then the supervisor serves them and sets this to 0 for children.
BOT_COMMANDS_IN_POLLER: bool = (_env("BOT_COMMANDS_IN_POLLER", "1") or "").strip().lower() in ("1", "true", "yes")


def get_portals_auth() -> str | None:
    """OPTIONAL. Measured 2026-09-19 (diag/portals_noauth_probe.py): every
    Portals endpoint this project calls (/nfts/search feed, by ids, pair
    and model floor, /collections/models/backgrounds/floors,
    /market/config) answers without an Authorization header, with the same
    content and the same rate limit (x-ratelimit-limit 2). A 97-hour-old
    token and a garbage token were accepted too, so the token was never
    checked. Without a token, requests go out with no Authorization header.
    """
    return _env("PORTALS_AUTH") or None  # never log this value


# MRKT token refreshed without the owner (mrkt_auth.py): a Telegram user
# session (TG_API_ID / TG_API_HASH from my.telegram.org, session file
# TG_SESSION.session) gets the mini-app initData and writes a new token to
# MRKT_TOKEN_FILE. Never logged.
TG_API_ID: str | None = _env("TG_API_ID")
TG_API_HASH: str | None = _env("TG_API_HASH")
TG_SESSION: str = _env("TG_SESSION") or os.path.join(_PROJECT_DIR, "secrets", "telegram")
# Absolute by default, same reason as DB_DSN: a relative path silently
# depends on the working directory of whoever writes or reads it.
MRKT_TOKEN_FILE: str = _env("MRKT_TOKEN_FILE") or os.path.join(_PROJECT_DIR, "secrets", "mrkt_token.txt")
_mrkt_token_cache: tuple[float, str] | None = None


def get_mrkt_access_token() -> str | None:
    """MRKT_TOKEN_FILE first (written by mrkt_auth.py, re-read when it
    changes, so a refreshed token reaches running processes), then env
    MRKT_ACCESS_TOKEN. None (not an exception) when neither is set -- per
    spec, MRKT must degrade to "not queried" rather than crash either
    poller. Never log the token value itself.
    """
    global _mrkt_token_cache
    try:
        mtime = os.path.getmtime(MRKT_TOKEN_FILE)
        if _mrkt_token_cache is None or _mrkt_token_cache[0] != mtime:
            with open(MRKT_TOKEN_FILE, encoding="utf-8") as fh:
                _mrkt_token_cache = (mtime, fh.read().strip())
        if _mrkt_token_cache[1]:
            return _mrkt_token_cache[1]
    except OSError:
        pass
    return _env("MRKT_ACCESS_TOKEN")

# --- polling ---
# Confirmed via a live 30-minute run: actual request usage was 144 search
# + 95 floor requests over 1802s (~0.13 req/s combined) against limits of
# 2/s (search) and 5/s (floors) -- multiple times over headroom. Default
# lowered from 15 to 5 accordingly (measured median detection delay drops
# from ~3.8s to ~1.3s). The floor of 3 is a residual safety margin, not a
# measured limit.
POLL_INTERVAL_SEC: int = int(_env("POLL_INTERVAL_SEC", "5"))
if POLL_INTERVAL_SEC < 3:
    raise ConfigError("POLL_INTERVAL_SEC must be >= 3 (residual safety margin)")

# --- storage ---
# Anchored to the project directory (the package's parent), NOT to the
# caller's working directory: `python health.py` from another folder used
# to fail with "unable to open database file" while the same code inside
# the supervisor worked (found by the daily review, 2026-09-19). Same
# class of defect as supervisor.Child's clock. An explicit DB_DSN env
# value still wins, relative or not.
DB_DSN: str = _env("DB_DSN") or os.path.join(_PROJECT_DIR, "gift_sniper.db")

# Two independent poller processes (poller.py/Portals, tonnel_poller.py/
# Tonnel) write to the same SQLite file -- SQLite allows many readers but
# only one writer at a time. db.connect() puts every connection in WAL
# mode (readers no longer block the writer) and sets this busy_timeout so
# a writer that finds the DB locked waits instead of raising immediately.
# This is a wait budget, NOT a substitute for keeping write transactions
# short -- see db.py's connect()/touch_listing_lifecycle and poller.py's
# review for long-held transactions.
SQLITE_BUSY_TIMEOUT_MS: int = int(_env("SQLITE_BUSY_TIMEOUT_MS", "15000"))

# --- currency: never hardcode 'TON' or 'GRAM' elsewhere in the codebase ---
# Confirmed live: /market/config prices this marketplace in GRAM, not
# TON. parsing.py always falls back to this default (the /nfts/search
# response has no currency field of its own -- item.get("currency") or
# CURRENCY_DEFAULT), so a wrong default here silently mislabels every
# listing's currency. Was "TON" (wrong) until this delivery -- caught
# because a notification showed "115.00 GRAM" worth of price displayed
# as "115.00 TON". Fixed to the confirmed value.
CURRENCY_DEFAULT: str = _env("CURRENCY_DEFAULT", "GRAM")

# --- rarity: UNCONFIRMED scale factor, see README "Допущения" ---
RARITY_SCALE_TO_PM: Decimal = _decimal_env("RARITY_SCALE_TO_PM", "10")

# --- floor cache TTL: UNCONFIRMED, see README "Допущения" ---
FLOOR_CACHE_TTL_SEC: int = int(_env("FLOOR_CACHE_TTL_SEC", "600"))

# --- fees ---
# Dimensionless ratio, applied directly, never scaled by NANO.
MARKETPLACE_FEE_RATE: Decimal = _decimal_env("MARKETPLACE_FEE_RATE", "0.02")

# Ordinary-unit value as read from env / API. Converted to nano ONCE, here,
# and only WITHDRAWAL_FEE_FLAT_NANO may be used in arithmetic downstream.
WITHDRAWAL_FEE_FLAT: Decimal = _decimal_env("WITHDRAWAL_FEE_FLAT", "0.35")
WITHDRAWAL_FEE_FLAT_NANO: int = int(WITHDRAWAL_FEE_FLAT * NANO)

# --- pagination / pacing ---
# Confirmed via live measurement: the 429 response on /nfts/search carries
# x-ratelimit-limit: 2, x-ratelimit-reset: 0 -- 2 requests per short
# window, no Retry-After header. The floors endpoint has a DIFFERENT,
# more generous limit: x-ratelimit-limit: 5, x-ratelimit-reset: 12.
# Throttling is therefore tracked PER ENDPOINT PATH in portals_client.py,
# not as one shared clock across the whole client -- two requests to
# different paths do not wait on each other. REQUEST_DELAY_MS=400
# (2.5 req/s) was measured to still trigger 429s on /nfts/search; 600ms
# (~1.67 req/s) is the current default, applied per path. Floor is
# 500ms -- going lower reintroduces the confirmed-too-fast regime.
MAX_PAGES_PER_ITERATION: int = int(_env("MAX_PAGES_PER_ITERATION", "6"))
REQUEST_DELAY_MS: int = int(_env("REQUEST_DELAY_MS", "600"))
if REQUEST_DELAY_MS < 500:
    raise ConfigError("REQUEST_DELAY_MS must be >= 500 (400ms measured to still trigger 429s)")

# --- collection-loop price filter ---
# Confirmed live, 50-record pages: no filter -> 50 records/6.5s (~7.7/s);
# min_price=15 -> 50 records/25.3s (~2.0/s); min_price=30 -> 49 records/
# 80.9s (~0.6/s). Separately measured: the poller's actual sustained
# throughput is capped at ~1.3 new listings/sec by Portals' 2 req/s limit
# on /nfts/search combined with ~92% of every unfiltered page being
# already-known records (~0.7 requests spent per net-new listing) -- the
# full stream (~7.7/s) can NEVER be kept up with under that limit. At
# min_price=15 the segment's own rate (~2/s) fits inside that ceiling
# with room to spare. min_price=8 was measured to give NO savings (the
# segment above 8 is nearly the entire stream, 50 records/6.5s -- same as
# unfiltered) -- filtering that low is pointless. Listings priced below
# this value are NOT written to the DB AT ALL -- this is a deliberate
# tradeoff of completeness for keeping up with the target segment, not a
# storage optimization (compare FLOOR_MIN_PRICE below, which is a
# different, independent threshold).
COLLECT_MIN_PRICE: Decimal = _decimal_env("COLLECT_MIN_PRICE", "15")
COLLECT_MIN_PRICE_NANO: int = int(COLLECT_MIN_PRICE * NANO)
if 0 < COLLECT_MIN_PRICE_NANO < 10 * NANO:
    raise ConfigError(
        "COLLECT_MIN_PRICE must be 0 (disabled) or >= 10 -- measured to give no "
        "request savings below 10 (the segment above 8 is nearly the whole stream)"
    )

# --- floor batching ---
# Confirmed live: models=30 -> 200 OK, all 30 keys returned.
# models=60 -> 400 Bad Request (root cause -- count vs. URL length -- not
# isolated, and doesn't matter practically). 25 is chosen with headroom
# under the confirmed-working 30. A larger batch matters a lot at a
# confirmed 2-requests-per-window rate limit: it directly divides the
# number of floor requests needed.
FLOORS_BATCH_SIZE: int = int(_env("FLOORS_BATCH_SIZE", "25"))
if FLOORS_BATCH_SIZE > 30:
    raise ConfigError("FLOORS_BATCH_SIZE must be <= 30 (60 confirmed to return 400 Bad Request)")

# --- price threshold below which a combo-floor lookup is not worth its
# rate-limit cost. Ordinary units, converted below. See README for the
# breakeven math this default is based on.
#
# THIS IS A DIFFERENT THRESHOLD FROM COLLECT_MIN_PRICE ABOVE -- do not
# conflate them:
#   COLLECT_MIN_PRICE -- what is written to the DB at all (collection loop)
#   FLOOR_MIN_PRICE   -- what gets a floor computed, for listings already
#                        in the DB (ANALYTICS PATH)
# Default raised from 8 to 15 to match the new COLLECT_MIN_PRICE default
# -- a FLOOR_MIN_PRICE below COLLECT_MIN_PRICE is meaningless (nothing
# that cheap is ever collected in the first place), which is exactly what
# the check right below guards against. ---
FLOOR_MIN_PRICE: Decimal = _decimal_env("FLOOR_MIN_PRICE", "15")
FLOOR_MIN_PRICE_NANO: int = int(FLOOR_MIN_PRICE * NANO)

if COLLECT_MIN_PRICE > FLOOR_MIN_PRICE:
    logger.warning(
        "COLLECT_MIN_PRICE (%s) is greater than FLOOR_MIN_PRICE (%s) -- "
        "FLOOR_MIN_PRICE has no effect below the collection threshold, "
        "since nothing cheaper than COLLECT_MIN_PRICE is ever written to "
        "the DB in the first place.",
        COLLECT_MIN_PRICE,
        FLOOR_MIN_PRICE,
    )

# --- price-drop significance threshold ---
# Confirmed live, 239 listings observed across repeated page appearances:
# 109 unchanged, 55 raised, 60 lowered. Lowered examples: 24.99->24.95
# (0.16%) and 22.99->22.98 (0.04%) -- machine-step relister-bot noise,
# not a seller repricing. 67.91->65.93 (2.9%) is a real, human-scale cut.
# Drops below this threshold are still recorded in price_history (never
# discarded -- it's a real observed change) but flagged is_noise=True so
# report.py can separate signal from bot noise in the price-drop block.
PRICE_DROP_MIN_PCT: Decimal = _decimal_env("PRICE_DROP_MIN_PCT", "1.0")

# --- price-drop anomaly filters ---
# Confirmed live via manual review of the 20 largest price drops: Jelly
# Bunny #2627 went 999 -> 99 -> 29 within 10 seconds -- a 90% single-step
# cut, no real seller repricing happens at this scale in one step. Single
# steps bigger than this are flagged is_anomaly and excluded from clean
# signals (still recorded, never discarded).
PRICE_DROP_MAX_PCT: Decimal = _decimal_env("PRICE_DROP_MAX_PCT", "60.0")
# A second drop on the same listing within this many seconds of a prior
# one means BOTH are flagged is_anomaly -- confirmed exactly by the Jelly
# Bunny case above (10 seconds apart). A real seller doesn't reprice
# twice inside a minute.
PRICE_DROP_BURST_SEC: int = int(_env("PRICE_DROP_BURST_SEC", "60"))

# --- relister-bot ladder detector ---
# Confirmed live: Low Rider #23134 stepped down 179.9 -> 146.51 -> 139.18
# -> 132.22 -> 125.6 -> 119.32 -> 113.35, a exact 5% step each time,
# ~30 minutes apart, floor equal to its own starting price (see the
# self-comparison bug this whole delivery is about). Each step looked
# like an isolated signal; grouped, it's obviously one bot mechanically
# walking its own price down, not a market opportunity -- buying now just
# means it's cheaper in half an hour anyway.
LADDER_WINDOW_HOURS: int = int(_env("LADDER_WINDOW_HOURS", "24"))
LADDER_MIN_DROPS: int = int(_env("LADDER_MIN_DROPS", "3"))

# --- market config refresh: fees are NOT constants, see README ---
CONFIG_REFRESH_SEC: int = int(_env("CONFIG_REFRESH_SEC", "3600"))

# --- API combo-floor sanity check ---
# The /collections/models/backgrounds/floors endpoint answers by model
# name GLOBALLY, with no collection scoping (collection_id/short_name are
# ignored -- confirmed live). Model names collide across collections
# constantly (32 names in 2-4 collections each, in a 30-minute sample),
# so the "floor" it returns is frequently the floor of a WRONG, unrelated
# collection. Confirmed concretely: Berry Box (collection floor 9.35) got
# an API combo-floor of 200.0; Liberty Figure (floor 4.39) got 65-97.
# A combo-floor can be somewhat above the collection floor (a rare
# backdrop within the collection), but not by an order of magnitude --
# 8x is a deliberately generous, NOT measured, threshold. This is a
# safety net, not the fix: the fix is own_floors.py, which computes the
# floor from our own collected listings, correctly scoped by
# collection_name.
FLOOR_SANITY_MAX_RATIO: int = int(_env("FLOOR_SANITY_MAX_RATIO", "8"))

# --- pair floor (source of truth for discount/profit) ---
# Confirmed live: /nfts/search accepts filter_by_models / filter_by_backdrops
# / filter_by_collections (comma-separated string values; the bracketed
# form filter_by_models[]=X gets the connection reset) plus collection_id
# (which, unlike the floors endpoint, actually filters), combined with
# sort=price_asc, to return real active listings for one exact
# collection+model+backdrop triple sorted by price. This replaces BOTH
# earlier broken sources: the API combo-floor endpoint (globally scoped
# by model name, confirmed to return other collections' floors) and
# own_floors.py's self-collected floor (confirmed systematically ~2x too
# high, median api/own... err own/true ratio 0.54, because the poller
# only observes newly-listed items, missing older cheap listings still
# active in the book). See pair_floor.py and README for the concrete
# measured numbers.
PAIR_FLOOR_CACHE_TTL_SEC: int = int(_env("PAIR_FLOOR_CACHE_TTL_SEC", "300"))

# --- FAST PATH / ANALYTICS PATH split ---
# Confirmed live: the collection loop (FAST PATH) doing floor lookups
# in-line was the actual root cause of losing >99% of the listing
# stream -- at a measured ~8 listings/sec, doing a pair-floor network
# call per listing inside the collection loop stretched each iteration
# so badly that the poller fell far behind. FAST PATH now only fetches
# pages and writes listings (FloorSnapshot rows start "pending"); a
# separate ANALYTICS PATH pass fills in floor data afterwards, bounded
# by FLOOR_BATCH_PER_RUN so it can never itself eat the rate-limit
# budget the FAST PATH needs.
#
# Defaults raised again after live measurement showed the worker still
# falling behind even after fixing the collection loop: 792 new listings
# in one run produced only 18 floor_requests and a floor_pending_count of
# 130 and climbing, against a worker ceiling of 40 floors/min
# (FLOOR_BATCH_PER_RUN=20 / FLOOR_WORKER_INTERVAL_SEC=30 = 40/min). Even
# after COLLECT_MIN_PRICE narrows the stream to ~2/s (~120/min), that
# ceiling was still too low. Raised to 30 per run / every 10s = 180/min.
# The pair-floor cache (PAIR_FLOOR_CACHE_TTL_SEC, see above) means actual
# network calls are fewer than listings processed whenever multiple
# pending listings share a (collection, model, backdrop) triple within
# the TTL window -- see floor_cache_hits in the run summary.
FLOOR_WORKER_INTERVAL_SEC: int = int(_env("FLOOR_WORKER_INTERVAL_SEC", "10"))
FLOOR_BATCH_PER_RUN: int = int(_env("FLOOR_BATCH_PER_RUN", "30"))

# --- thin-book / implausible-floor filters (report.py) ---
# Confirmed live: manual review of 20 "clean" price-drop signals found 18
# of 20 were artifacts, all with pair_listed_count_excl_self == 1 -- i.e.
# after excluding the listing itself, exactly ONE other lot remained, and
# ITS price was taken as the floor. Concrete examples: Khabib's Papakha
# (Chokha+Hunter Green) price 23.91 vs. "floor" 230.00; Eternal Rose
# (3D Glow+Black) 139.00 vs. 1000.00; Easter Egg (Scrambull+Black) 88.00
# vs. 600.00 -- a single mispriced/unsold listing, not a market floor.
# Confirmed by the liquidity breakdown itself: cnt=1 -> 85 signals deeper
# than 35%, cnt=2-3 -> 17, cnt=4-9 -> 2, cnt=10+ -> 0 -- the "discount"
# shrinks monotonically as real competition increases, the signature of
# an artifact, not a market pattern. Applies identically to pair AND
# model level (same failure mode either way). Floor of 2 enforced below:
# a floor of the listing_count minimum itself (1) would defeat the
# entire point of this filter.
FLOOR_MIN_LISTED_COUNT: int = int(_env("FLOOR_MIN_LISTED_COUNT", "3"))
if FLOOR_MIN_LISTED_COUNT < 2:
    raise ConfigError("FLOOR_MIN_LISTED_COUNT must be >= 2 (a value of 1 defeats this filter's purpose)")

# --- floor/price ratio sanity check (report.py) ---
# A second, independent net on top of FLOOR_MIN_LISTED_COUNT: even a
# floor computed from several other listings can still be implausible if
# it is many times the signal's own price. Confirmed live (same 20-signal
# review as above): ratios of 7-10x between price and "floor" were single
# mispriced listings, not real discounts. UNMEASURED default (unlike
# FLOOR_SANITY_MAX_RATIO above, which has a live-measured basis) --
# 4.0 is a deliberately conservative starting point, to be tightened or
# loosened once report.py has accumulated enough clean signals to measure
# a real distribution. See README.
FLOOR_MAX_RATIO_TO_PRICE: Decimal = _decimal_env("FLOOR_MAX_RATIO_TO_PRICE", "4.0")

# --- absolute-profit threshold (signals.py's below_min_profit stage) ---
# Measured live, 3 days across all marketplaces: 548/1379 (40%) of
# Portals' "clean" signals had ratio < 1.05 -- including genuinely
# NEGATIVE-profit rows (Joyful Bundle/Pepe Bag: price 32.45, floor
# 32.50, profit_usd = -4.54) that were still being treated as clean
# because profit was None at level="model" and no absolute-profit gate
# existed at level="pair" either. A fixed ratio threshold is the wrong
# shape of check: at ratio 1.05 (2% fee + 0.35 withdrawal), profit is
# ~0.09 TON (~$0.12) on a 15 TON lot but ~8.35 TON (~$11.86) on a 300
# TON one -- the same percentage means wildly different real money
# depending on price. MIN_SIGNAL_PROFIT_TON is therefore an ABSOLUTE
# floor, in TON/GRAM units (converted to nano below), applied via
# signals.compute_profit_nano -- the single shared formula, never
# duplicated per file. UNMEASURED exact value -- 1.0 TON (~$1.4) is a
# deliberately modest starting point, tightened once real signal volume
# at this threshold is observed.
MIN_SIGNAL_PROFIT_TON: Decimal = _decimal_env("MIN_SIGNAL_PROFIT_TON", "1.0")
MIN_SIGNAL_PROFIT_NANO: int = int(MIN_SIGNAL_PROFIT_TON * NANO)

# --- realization rate (signals.compute_profit_nano) ---
# Measured 2026-09-14 on confirmed MRKT sales (751 sales, 67 with a pair
# floor recorded at the moment of sale, 61 after dropping ratio outliers
# outside [0.2, 1.5]): sold_price / floor_at_sale had median 0.783 and max
# 1.000 -- no sale ever cleared ABOVE the floor, so the floor is a
# ceiling, not an expected sale price. By book depth at sale:
#   depth 1:   n=41, median 0.606
#   depth 2-3: n=14, median 0.898
#   depth 4-9: n=6,  median 0.950
#   depth 10+: n=0 (no data -- default copied from 4-9)
# Defaults are the measured medians rounded conservatively. The 2-3 and
# 4-9 samples are SMALL -- re-measure with sale_vs_floor.py as data
# accumulates and override via env, never by editing code.
REALIZATION_RATE_DEPTH_1: Decimal = _decimal_env("REALIZATION_RATE_DEPTH_1", "0.61")
REALIZATION_RATE_DEPTH_2_3: Decimal = _decimal_env("REALIZATION_RATE_DEPTH_2_3", "0.90")
REALIZATION_RATE_DEPTH_4_9: Decimal = _decimal_env("REALIZATION_RATE_DEPTH_4_9", "0.95")
REALIZATION_RATE_DEPTH_10: Decimal = _decimal_env("REALIZATION_RATE_DEPTH_10", "0.95")

# --- floor stability (signals.py's unstable_floor stage) ---
# Measured on Portals, 130 pairs with 3+ floor snapshots: median max/min
# spread 1.06; spread >= x1.5 in 22 pairs (16%), >= x2.0 in 11 (8%),
# >= x3.0 in 5 (3%) (e.g. Snoop Dogg / Woofee / Onyx Black: 13 snapshots,
# 10.83..23.00). 2.0 cuts the clear outliers without touching normal
# fluctuation. Fewer than 3 snapshots in the window -> stage is skipped
# (not enough data is never a reason to drop a row).
FLOOR_STABILITY_WINDOW_HOURS: int = int(_env("FLOOR_STABILITY_WINDOW_HOURS", "24"))
FLOOR_MAX_INSTABILITY: Decimal = _decimal_env("FLOOR_MAX_INSTABILITY", "2.0")

# --- floor snapshot freshness (signals.py's stale_floor stage) ---
# Measured live: Tonnel floor_snapshots rows used as a signal's floor
# have been found 8-12 HOURS stale relative to the drop being evaluated
# -- tens of price changes happen on a marketplace in that span. A
# snapshot older than this is refused outright rather than silently
# compared against, regardless of how normal its listed_count looked
# (the depth-of-book was never the problem -- see tonnel_poller.py's
# _maybe_refresh_floor_snapshot(), fixed the same delivery this setting
# was added in). Only ever applies to floor_source="snapshot" rows --
# an "at_drop" floor is fetched at the exact moment of the drop being
# evaluated and can never be stale by construction.
MAX_SNAPSHOT_AGE_MIN: int = int(_env("MAX_SNAPSHOT_AGE_MIN", "30"))

# --- dual-floor sampling (poller.py) ---
# Confirmed live (model_level_audit.py): "rows with BOTH pair and model
# floor filled: 0" -- not a bug in the audit script, but a direct
# consequence of the mutually exclusive-by-construction fetch discipline
# (model floor is only ever requested when pair gave "alone_in_pair" --
# see pair_floor.py/poller.py). There is therefore no data anywhere to
# measure how much the two levels disagree. This deliberately spends a
# small extra slice of rate-limit budget, for a sample of listings whose
# pair floor already came back "ok", to ALSO fetch the model floor
# purely for comparison (written into the same existing model_floor_*
# columns -- no schema change). Selection is deterministic
# (hash(external_id) % 100 < DUAL_FLOOR_SAMPLE_PCT), not random, so a
# repeated run samples the same listings rather than a fresh random draw
# each time. Default 0 (disabled): ordinary runs must not spend any
# extra budget on this -- it is opt-in, for measurement runs only.
DUAL_FLOOR_SAMPLE_PCT: int = int(_env("DUAL_FLOOR_SAMPLE_PCT", "0"))
if not (0 <= DUAL_FLOOR_SAMPLE_PCT <= 100):
    raise ConfigError("DUAL_FLOOR_SAMPLE_PCT must be between 0 and 100")

# --- Telegram notification (notifier.py) ---
# Personal-use bot: notifies the OWNER (TELEGRAM_OWNER_ID) and any
# VIEWERS (TELEGRAM_VIEWER_IDS, optional, comma-separated) about clean
# signals (signals.clean_signals -- the SAME cascade report.py uses, not
# a re-implementation) in real time. The owner can run every bot
# command; viewers can only run read-only ones (/status, /last) -- see
# notifier.py's CommandHandler. NOT a public bot -- anyone who is
# neither gets rejected with "доступ ограничен".
#
# NOTIFY_ENABLED defaults to False: the bot must be explicitly opted
# into, same spirit as DUAL_FLOOR_SAMPLE_PCT defaulting to 0 -- ordinary
# runs must not require Telegram credentials just to collect data.
NOTIFY_ENABLED: bool = _env("NOTIFY_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def get_telegram_bot_token() -> str:
    """Lazily read, like config.get_portals_auth() -- NOT a module-level
    constant, so importing config never requires TELEGRAM_BOT_TOKEN to be
    set unless the bot is actually being constructed. Required ONLY when
    NOTIFY_ENABLED is true; enforced at the call site (notifier.py /
    poller.py's build_default_poller), not here. NEVER logged, exactly
    like PORTALS_AUTH.
    """
    return _env("TELEGRAM_BOT_TOKEN", required=True)


def get_telegram_owner_id() -> str:
    """Lazily read, same reasoning as get_telegram_bot_token() above --
    required only when NOTIFY_ENABLED is true. The OWNER receives every
    notification and can run every bot command (see notifier.py's
    CommandHandler). TELEGRAM_USER_ID (the old, single-recipient name)
    is accepted as a synonym for backward compatibility ONLY when
    TELEGRAM_OWNER_ID itself is unset -- a warning is logged so an
    existing deployment notices the rename without breaking.
    """
    owner_id = _env("TELEGRAM_OWNER_ID")
    if owner_id:
        return owner_id
    legacy = _env("TELEGRAM_USER_ID")
    if legacy:
        logger.warning(
            "TELEGRAM_OWNER_ID is not set -- falling back to the legacy TELEGRAM_USER_ID=%s. "
            "Set TELEGRAM_OWNER_ID instead; TELEGRAM_USER_ID is kept only for backward compatibility.",
            legacy,
        )
        return legacy
    raise ConfigError("Missing required env var: TELEGRAM_OWNER_ID (or the legacy TELEGRAM_USER_ID)")


def get_telegram_user_id() -> str:
    """DEPRECATED alias for get_telegram_owner_id() -- kept only so any
    external code/notes referencing the old name keep working. New code
    must call get_telegram_owner_id().
    """
    return get_telegram_owner_id()


def get_telegram_viewer_ids() -> list[str]:
    """VIEWERS also receive every notification but can only run
    read-only bot commands (see notifier.py's CommandHandler). Optional
    -- TELEGRAM_VIEWER_IDS unset or empty means no viewers, i.e. the
    original single-recipient behavior. Comma-separated, whitespace
    around each id is stripped, empty entries are dropped (so a trailing
    comma or accidental double comma doesn't produce a bogus empty id).
    """
    raw = _env("TELEGRAM_VIEWER_IDS", "") or ""
    return [v.strip() for v in raw.split(",") if v.strip()]


# Confirmed live (manual review of 20 clean signals -- same review this
# entire filter chain is built on): profit_usd below a few dollars is not
# worth an interactive alert. 5 is a deliberately round, UNMEASURED
# starting point (like FLOOR_MAX_RATIO_TO_PRICE) -- to be tightened or
# loosened once real notification volume is observed.
NOTIFY_MIN_PROFIT_USD: Decimal = _decimal_env("NOTIFY_MIN_PROFIT_USD", "5")

# Comma-separated list of signals.Signal.floor_level values to notify on
# (Portals and MRKT; Tonnel has TONNEL_NOTIFY_LEVELS below).
# Default "pair,model" (2026-09-15). Measured: in 12h all 10 clean Portals
# signals were level=model (Loot Bag +46.05 TON, Scared Cat +11.13 TON), so
# "pair" alone sent nothing; over 24h model gave 25 signals (21 above $5),
# pair 5 (3 above $5). The model level was switched off earlier because a
# model floor belongs to another backdrop and inflated profit. Since then
# the cascade applies the depth-based realization rate and
# MIN_SIGNAL_PROFIT_TON, which cut inflated estimates.
# OPEN QUESTION: the model level is NOT verified by facts. No sale has a
# model floor computed (n=0; pair level: n=58, median realization 0.67),
# so model-level signals use the pair-level realization rate as an
# assumption. The paper journal records floor_level and splits by it --
# decide after 2-4 weeks from its data.
NOTIFY_LEVELS: set[str] = {
    lvl.strip() for lvl in _env("NOTIFY_LEVELS", "pair,model").split(",") if lvl.strip()
}

# ДЕФЕКТ 2 (systemic-check delivery): Tonnel has no pair floor at ALL
# (see _maybe_refresh_floor_snapshot) -- every Tonnel signal is
# level="model" by construction, which NOTIFY_LEVELS's default "pair"
# was never meant to gate. Previously this was handled by tonnel_poller.
# py's `_maybe_notify` simply NOT applying the NOTIFY_LEVELS check at
# all for Tonnel candidates -- correct in effect, but implicit: nothing
# in the code actually asserted "Tonnel signals are level=model, that's
# fine" the way an explicit gate would. Made explicit per spec ("для
# Tonnel... своя настройка... а не молча проходить"): TONNEL_NOTIFY_
# LEVELS, applied the SAME way NOTIFY_LEVELS is for Portals/MRKT, just
# defaulting to the one level Tonnel actually has.
TONNEL_NOTIFY_LEVELS: set[str] = {
    lvl.strip() for lvl in _env("TONNEL_NOTIFY_LEVELS", "model").split(",") if lvl.strip()
}

# Confirmed (Telegram Bot API docs): ~30 messages/sec globally, ~20/min
# per individual chat. This cap is deliberately counted PER CHAT (i.e.
# per SIGNAL, not per raw HTTP request) -- notifier.py's
# select_signals_to_send() picks at most this many SIGNALS per check,
# and every signal becomes exactly ONE message in each recipient's own
# chat, regardless of how many recipients (owner + viewers) there are.
# So this number already IS the per-chat rate every individual
# recipient sees, unaffected by N -- what DOES grow with N is the total
# number of Telegram API requests per check (N per signal, one per
# recipient's chat), which matters for the ~30/sec GLOBAL limit, not
# this one. A default of 10 leaves comfortable headroom under the
# per-chat limit even if a burst of clean signals arrives in the same
# poll cycle; the rest are summarized in one line ("...and N more, see
# /last") rather than silently dropped.
NOTIFY_MAX_PER_MINUTE: int = int(_env("NOTIFY_MAX_PER_MINUTE", "10"))

# --- per-listing cooldown (notifier.py / poller.py) ---
# Confirmed live: Cupid Charm #8599 was notified TWICE within a minute
# (22.00 then 21.60) -- two genuinely different price_history rows
# (different observed_at), so the (listing_external_id, observed_at)
# anti-duplicate check in alerts_sent never catches this; it's a
# distinct failure mode (repeat notifications about the SAME listing in
# quick succession), not a duplicate-signal one. SIGNAL_COOLDOWN_MIN
# (default 60): if a notification for this listing_external_id was sent
# less than this many minutes ago, a new one is suppressed --
# UNMEASURED default (like FLOOR_MAX_RATIO_TO_PRICE), a round hour.
SIGNAL_COOLDOWN_MIN: int = int(_env("SIGNAL_COOLDOWN_MIN", "60"))
# Exception: if the new price is LOWER than the last-notified price by
# more than this percent, the cooldown is bypassed and the resend is
# sent anyway (marked "ЛИСТИНГ · ЦЕНА СНИЖЕНА" in the message, see
# notifier.format_caption) -- a real further price cut during the
# cooldown window is worth knowing about even if the listing was
# already notified recently. UNMEASURED default.
SIGNAL_RESEND_DROP_PCT: Decimal = _decimal_env("SIGNAL_RESEND_DROP_PCT", "10")

# NOTIFY_MIN_PROFIT_USD (above) only applies to level="pair" signals --
# signals.Signal.profit_usd IS computed for level="model" too (as of the
# below_min_profit cascade delivery), but it's flagged profit_is_estimate
# (see signals.py/compute_profit_nano's docstring: the model floor is a
# different item's price, an absolute profit computed against it is an
# estimate -- confirmed live, Durov's Glasses #3685 showed ~$29
# "profit" against a model floor that was another backdrop's price; the
# listing was actually the cheapest in its own pair, real flip potential
# ~2.5 TON before fees). Model-level signals are instead gated on
# discount% alone. 15 is a deliberately round, UNMEASURED starting point
# (like FLOOR_MAX_RATIO_TO_PRICE) -- to be
# tightened or loosened once real notification volume is observed.
NOTIFY_MIN_DISCOUNT_PCT: Decimal = _decimal_env("NOTIFY_MIN_DISCOUNT_PCT", "15")

# --- listing lifecycle tracking (poller.py / db.py, schema v10) ---
# A listing disappearing is the only observable signal we have that it
# MIGHT have sold -- it is NOT proof of a sale (the owner could simply
# have delisted it; see README). Distinguished from a genuine sale only
# by whether it reappears afterwards.
#
# FIXED, confirmed live: the original mechanism (mark disappeared_at if
# not SEEN IN THE FEED for LIFECYCLE_GONE_AFTER_SEC) measured feed
# surfacing, not sales. Confirmed: 39325 "newly gone" overnight against
# only 11705 listings ever in the DB and 993 new that same night --
# disappearing more times than rows exist is impossible for a real
# disappearance signal. Root cause: the poller only reads the first
# MAX_PAGES_PER_ITERATION pages of freshly-surfaced listings; a listing
# sitting deep in the book for a while (not gone, just not on the first
# few pages) drops out of that view, gets marked "disappeared" ~10
# minutes later, and then "reappears" the moment its seller touches its
# price and it resurfaces on page 1 -- one specific listing
# (d851447b-afc5-4f97-b082-1cbfff590413) hit reappeared_count=41 in a
# single night. This is a resurfacing-frequency measurement, not a
# lifecycle one. LIFECYCLE_GONE_AFTER_SEC is RETIRED -- no time-based
# rule of any kind decides disappearance any more.
#
# FIX: disappearance is now decided ONLY by an explicit GET
# /nfts/search?ids=<id1>,<id2>,... status check (confirmed live: this
# endpoint returns a queried listing's CURRENT status; a withdrawn lot
# returned status="withdrawn", price=null). A background pass, on its
# own LIFECYCLE_CHECK_INTERVAL_SEC timer, batches LIFECYCLE_BATCH_SIZE
# not-yet-disappeared listings (oldest last_checked_at first) through
# this check. status="listed" just bumps last_checked_at;
# status in ("withdrawn", "unlisted") sets disappeared_at + final_status;
# any other status value is logged and treated as "unknown" (bumps
# last_checked_at only, never marked disappeared); a listing missing
# from the response entirely is logged and left alone (disappeared_at
# is NEVER set from an absence -- an absence proves nothing, per spec).
LIFECYCLE_CHECK_INTERVAL_SEC: int = int(_env("LIFECYCLE_CHECK_INTERVAL_SEC", "300"))
# Confirmed live: /nfts/search?ids=<id1>,<id2> (multiple comma-separated
# values in one call, not just the single-id case) returned 200 with
# both results, correct status/price for each. LIFECYCLE_BATCH_SIZE
# defaults to 50 -- the confirmed max effective page size on this
# endpoint (see search()); PortalsClient.search_by_ids() always passes
# an explicit limit= equal to the number of ids requested, since without
# it the server's default page size can silently truncate the result
# set below what was asked for.
#
# Budget estimate (record this so future threshold changes are informed,
# not guessed): at ~3500 tracked listings, batch 50, interval 300s, a
# full sweep of every tracked listing takes ~3500/50 batches * 300s =
# 70 batches * 300s = ~5.8 hours. Comfortably inside
# LIQUIDITY_WINDOW_HOURS=168h (a week), and fast enough that same-day
# disappearance data is realistic for most listings (previously ~29.2h
# at batch 20 / interval 600s).
LIFECYCLE_BATCH_SIZE: int = int(_env("LIFECYCLE_BATCH_SIZE", "50"))
if LIFECYCLE_BATCH_SIZE > 50:
    raise ConfigError("LIFECYCLE_BATCH_SIZE must be <= 50 (confirmed max effective page size on /nfts/search)")

# --- pair liquidity, shown in notifications (signals.py / notifier.py) ---
# Confirmed live: we did not previously track whether gifts ever actually
# sell. Counting "gone and not since reappeared" listings for a
# (collection_id, model_name, backdrop_name) pair over this window is a
# proxy for liquidity -- shown alongside a signal so a discount against
# an illiquid pair doesn't look like a real opportunity. 168h = 1 week,
# UNMEASURED starting point.
LIQUIDITY_WINDOW_HOURS: int = int(_env("LIQUIDITY_WINDOW_HOURS", "168"))

# --- bulk-reprice detector (report.py) ---
# Confirmed live: three DIFFERENT Khabib's Papakha listings all recorded
# the identical drop 24.60 -> 23.91 (same delta_pct) at the identical
# timestamp 19:14:32 -- one seller adjusting several of their own lots at
# once, not three independent market signals. Grouped by
# (collection_name, delta_pct) within this many seconds of each other.
SAME_SECOND_WINDOW: int = int(_env("SAME_SECOND_WINDOW", "5"))

# --- Tonnel cross-market verification (tonnel_client.py / poller.py) ---
# Confirmed live (see tonnel_client.py's module docstring for the full
# set of confirmed API facts): Tonnel Market (gifts2.tonnel.network) is a
# SEPARATE marketplace from Portals, no auth needed. For each clean
# signal, after all existing filters and BEFORE sending, the poller
# queries Tonnel's own pair floor (same collection+model+backdrop,
# self-excluded by gift_num) as a second, independent price source.
#
# Confirmed fee models differ and must never be compared without
# conversion: Tonnel adds a 10% BUYER fee (price*1.1 is what a buyer
# pays); Portals charges a 2% SELLER fee (MARKETPLACE_FEE_RATE, applied
# at sale). GRAM (Portals' asset) and TON (Tonnel's asset) are the same
# network/token, 1:1 -- confirmed, not assumed -- but stored as separate
# fields, never silently merged.
# Правка 4 (Tonnel full-collector delivery): default flipped false.
# Measured live: with only on-demand pair queries (no accumulated Tonnel
# history), this cross-check lands on "no_data" for the overwhelming
# majority of signals -- Tonnel almost never happens to be selling the
# EXACT same (collection, model, backdrop) combination at query time
# (confirmed separately: a random pair-overlap measurement found only
# ~40% of pairs exist on Tonnel at all, and that's before requiring a
# forsale, non-excluded listing at the moment of the check). This code
# path is NOT removed -- it will be reused once этап 2 (Tonnel's own
# accumulated listing history, from this delivery's collector) gives it
# a real floor to compare against instead of a single live query.
# Правка 1 (two-way cross-check delivery): replaces the earlier, one-way
# TONNEL_CROSS_CHECK_ENABLED -- a single flag now gates BOTH directions
# (a Portals signal checked against Tonnel's pair floor, AND a Tonnel
# signal checked against Portals' pair floor), via the shared
# cross_check() mechanism (see cross_check.py). Measured live on 19 real
# signals from both marketplaces: pair-level (model+backdrop) comparison
# found a comparable neighbour listing for 6/12 Portals signals and 5/7
# Tonnel signals (~60% coverage) -- workable, unlike per-listing
# comparison (0/19, rejected -- the exact same gift can never be listed
# on two marketplaces at once, see README). Default flipped to true now
# that there's a real, workable comparison on both sides.
CROSS_CHECK_ENABLED: bool = _env("CROSS_CHECK_ENABLED", "true").strip().lower() in ("1", "true", "yes")

# Правка 3 (unified-notification delivery): REPLACES BOTH
# CROSS_MARKET_MIN_GAP_PCT and CROSS_MAX_RATIO (removed -- see below) --
# cross-check is no longer a separate "confirmed"/"worse"/"no_data"
# verdict feeding a checkmark AND a separate arbitrage notification type;
# it is now a single, pure pre-send filter (cross_check.py). Let P be the
# signal's own price and N the neighbour's floor price (fee-adjusted
# where applicable): N absent -> send; N <= P*(1+CROSS_MIN_GAP_PCT/100)
# -> do not send (the neighbour is the same price or only slightly more
# -- nothing to gain, a buyer would just go there); N > that threshold
# -> send. UNMEASURED, like the settings it replaces -- 10% is a
# deliberately moderate starting point (between the removed
# CROSS_MARKET_MIN_GAP_PCT's 15% and no gap requirement at all), to be
# tightened/loosened once more real gaps are observed.
CROSS_MIN_GAP_PCT: Decimal = _decimal_env("CROSS_MIN_GAP_PCT", "10")

# Правка 4: the mirror-image protection CROSS_MAX_RATIO used to provide
# (a lonely, arbitrarily-priced neighbour listing skewing the verdict),
# now done by COUNTING listings instead of bounding a ratio -- the SAME
# class of fix already applied to Portals' own pair floor
# (FLOOR_MIN_LISTED_COUNT) and Tonnel's own model floor
# (TONNEL_MODEL_MIN_LISTED_COUNT). Confirmed live: a "confirmed"-
# equivalent verdict was forming at neighbour_listed_count=1 (Lush
# Bouquet, Moonlight + Seal Brown -- neighbour 21.50 at 1 listing;
# Voodoo Doll, Electrician + Ivory White -- 39.20 at 1 listing; Mood
# Pack, Moon Power + Onyx Black -- 82.50 at 1 listing) -- one seller's
# asking price is not a market price. Below this threshold, the
# neighbour's floor is NOT used in the decision at all -- treated
# exactly like "no comparable neighbour" (send), recorded as its own
# verdict "neighbour_thin" for the report. UNMEASURED, like
# CROSS_MIN_GAP_PCT -- 3 is a deliberately moderate starting point.
CROSS_MIN_NEIGHBOUR_COUNT: int = int(_env("CROSS_MIN_NEIGHBOUR_COUNT", "3"))

# ДЕФЕКТ 5 (systemic-check delivery): CROSS_MIN_NEIGHBOUR_COUNT=3 turned
# out to be UNREACHABLE in practice -- measured live, 102 cross-check
# snapshots, not ONE neighbour ever had 3+ listings (11 had 1, 5 had 2),
# so 25/102 (25%) of checks landed on neighbour_thin and were discarded
# even when real, usable data existed (Victory Medal/Dunk Master: Tonnel
# neighbour 11.44 at 2 lots; Instant Ramen/Broccoli: 71.50 at 2 lots;
# Snoop Dogg/Super Bowl: 60.50 at 2 lots). Depth-of-book is not the only
# way to trust a price -- two INDEPENDENT sellers on two DIFFERENT
# marketplaces agreeing closely is itself evidence of a real market
# price, regardless of how many listings sit behind either one. When 2+
# neighbours' floors (thin or not) diverge by less than this percent
# (computed as abs(a-b)/min(a,b)*100), their agreement is used as a
# vote on its own, bypassing CROSS_MIN_NEIGHBOUR_COUNT for THAT vote --
# see cross_check.py's _combine/cross_check. A single thin neighbour
# alone is UNCHANGED (still neighbour_thin, never blocks). UNMEASURED
# default, like CROSS_MIN_GAP_PCT -- 25 is a deliberately generous
# starting point (looser than CROSS_MIN_GAP_PCT's 10, since two
# INDEPENDENT sources agreeing is already strong evidence even with some
# spread between them).
CROSS_AGREEMENT_PCT: Decimal = _decimal_env("CROSS_AGREEMENT_PCT", "25")

# --- MRKT (tgmrkt.io) -- THIRD cross-check neighbour, never a signal ---
# source (see mrkt_client.py's module docstring for why: no reliable
# freshness ordering exists on this API). The token itself
# (MRKT_ACCESS_TOKEN) is read LAZILY via get_mrkt_access_token() below,
# same discipline as get_portals_auth() -- nothing at import time
# depends on it being set, and its absence returns None (never raises)
# so the caller can degrade to "MRKT not queried" instead of crashing.

# Confirmed live: 10 back-to-back requests, 10/10 succeeded, ~0.84s each,
# no rate limit observed -- same "apply the pause anyway" discipline as
# TONNEL_REQUEST_DELAY_MS.
MRKT_REQUEST_DELAY_MS: int = int(_env("MRKT_REQUEST_DELAY_MS", "600"))

# Правка 2 (MRKT third-neighbour delivery): gates whether MRKT is
# queried as a cross-check neighbour at all -- independent of
# CROSS_CHECK_ENABLED (which gates Tonnel<->Portals), so MRKT can be
# turned off on its own without touching the other two. Default true --
# unlike CROSS_MARKET_MIN_GAP_PCT's original off-by-default caution, MRKT
# adds a THIRD independent data point to an already-working mechanism,
# not a brand-new unproven one.
MRKT_CROSS_CHECK_ENABLED: bool = _env("MRKT_CROSS_CHECK_ENABLED", "true").strip().lower() in ("1", "true", "yes")

# --- Tonnel full collector (tonnel_poller.py, tonnel_parsing.py) ---
# Этап 1: accumulate Tonnel's own listing history (feed, price changes,
# lifecycle) the same way this project already does for Portals --
# signals/cross-check on top of that history are этап 2, deliberately
# out of scope here (see README).
#
# Settings kept SEPARATE from Portals' equivalents (POLL_INTERVAL_SEC,
# MAX_PAGES_PER_ITERATION, COLLECT_MIN_PRICE, REQUEST_DELAY_MS) -- the
# two pollers are independent processes against unrelated APIs with
# unrelated measured behavior; there is no reason their tuning should be
# coupled.
#
# UNMEASURED (like the Portals defaults were originally) -- reasonable
# starting points, not confirmed-optimal live numbers:
TONNEL_POLL_INTERVAL_SEC: int = int(_env("TONNEL_POLL_INTERVAL_SEC", "15"))
TONNEL_MAX_PAGES_PER_ITERATION: int = int(_env("TONNEL_MAX_PAGES_PER_ITERATION", "6"))
TONNEL_COLLECT_MIN_PRICE: Decimal = _decimal_env("TONNEL_COLLECT_MIN_PRICE", "15")
TONNEL_COLLECT_MIN_PRICE_NANO: int = int(TONNEL_COLLECT_MIN_PRICE * NANO)

# Confirmed live (see tonnel_client.py): no rate limit observed across 20
# back-to-back requests, but per spec ("это не значит, что его нет") the
# pause is applied unconditionally regardless. tonnel_client.TonnelClient
# defaults to 600ms internally when constructed with no argument --
# tonnel_poller.py passes this explicitly instead, so it's independently
# tunable from Portals' own REQUEST_DELAY_MS without touching code.
TONNEL_REQUEST_DELAY_MS: int = int(_env("TONNEL_REQUEST_DELAY_MS", "600"))

# Confirmed live (ДОПОЛНЕНИЕ, Правка 3): {"gift_id": {"$in": [...]}}
# batches lifecycle status checks -- 30 (MAX_LIMIT in tonnel_client.py,
# also confirmed live) is the largest single request this can be. Must
# be passed as `limit` explicitly on every search_by_gift_ids() call, or
# part of the result silently truncates (same failure mode confirmed on
# Portals' floors-by-model-name endpoint).
TONNEL_LIFECYCLE_BATCH_SIZE: int = int(_env("TONNEL_LIFECYCLE_BATCH_SIZE", "30"))

# --- Tonnel signals (signals.py / tonnel_poller.py / notifier.py) ---
# Правка 1: signals.py's filter cascade (is_anomaly/is_noise/is_ladder/
# floor no_data/thin-book/is_implausible/is_bulk_update) is REUSED as-is
# for Tonnel, parameterized by marketplace -- not duplicated. These four
# thresholds are its marketplace-specific knobs. Values below are taken
# from Portals' own defaults as a STARTING POINT -- UNMEASURED for
# Tonnel (Tonnel's feed is ~120 new listings/hour total, about half of
# Portals' measured rate, so the same numbers are a reasonable guess,
# not a confirmed-correct one) -- see README, to be tightened once real
# Tonnel signal volume is observed.
TONNEL_PRICE_DROP_MIN_PCT: Decimal = _decimal_env("TONNEL_PRICE_DROP_MIN_PCT", "1.0")
TONNEL_FLOOR_MIN_LISTED_COUNT: int = int(_env("TONNEL_FLOOR_MIN_LISTED_COUNT", "3"))
TONNEL_FLOOR_MAX_RATIO_TO_PRICE: Decimal = _decimal_env("TONNEL_FLOOR_MAX_RATIO_TO_PRICE", "4.0")
TONNEL_SIGNAL_COOLDOWN_MIN: int = int(_env("TONNEL_SIGNAL_COOLDOWN_MIN", "60"))

# Правка 4: separate from the shared NOTIFY_ENABLED -- lets the Tonnel
# collector run (accumulating listings/price history) without sending
# any notifications, independent of whether the Portals bot is on.
# Recipients (TELEGRAM_OWNER_ID/TELEGRAM_VIEWER_IDS) and the bot token
# are shared with Portals -- only whether Tonnel signals are SENT is
# separately gated.
TONNEL_NOTIFY_ENABLED: bool = _env("TONNEL_NOTIFY_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# --- MRKT full collector + signaller (mrkt_poller.py, mrkt_parsing.py) ---
# MRKT's own event feed (/api/v1/feed) was found and confirmed live to
# be strictly chronological (unlike the /gifts/saling showcase used for
# cross-check pair_floor() queries, whose order was confirmed RANDOM --
# see mrkt_client.py -- and is NEVER used for collection). Confirmed
# live: at a 30s poll interval, the first page's 20 events fully turned
# over within 30s -- events are GUARANTEED lost at that interval. At
# ~1.2 events/sec measured, 8s keeps a comfortable margin under a full
# page (20 events) between polls.
MRKT_POLL_INTERVAL_SEC: int = int(_env("MRKT_POLL_INTERVAL_SEC", "8"))
MRKT_MAX_PAGES_PER_ITERATION: int = int(_env("MRKT_MAX_PAGES_PER_ITERATION", "8"))

# In normal TON units (like TONNEL_COLLECT_MIN_PRICE/COLLECT_MIN_PRICE),
# converted to nano below. The feed has NO server-side price filter
# (confirmed: the /api/v1/feed endpoint accepts no price params at all,
# unlike /gifts/saling's minPrice/maxPrice) -- filtering happens
# entirely on this side, counted in collect_filtered_count.
MRKT_COLLECT_MIN_PRICE: Decimal = _decimal_env("MRKT_COLLECT_MIN_PRICE", "15")
MRKT_COLLECT_MIN_PRICE_NANO: int = int(MRKT_COLLECT_MIN_PRICE * NANO)

# --- MRKT signals (signals.py / mrkt_poller.py / notifier.py) ---
# Правка 3: signals.py's filter cascade is REUSED as-is for MRKT,
# parameterized by marketplace -- not duplicated (see _thresholds_for).
# Values below start as Portals' own defaults, UNMEASURED for MRKT --
# same starting-point discipline as Tonnel's equivalents, to be
# tightened once real MRKT signal volume is observed.
MRKT_PRICE_DROP_MIN_PCT: Decimal = _decimal_env("MRKT_PRICE_DROP_MIN_PCT", "1.0")
# Правка 3, verbatim from spec: MRKT's OWN pair-level floor (collection+
# model+backdrop) is usable directly -- unlike Tonnel, where the pair
# level is confirmed always empty and model-level is the only workable
# basis. MRKT's /gifts/saling pair_floor() query returns a real `total`
# depth-of-book figure without pagination (see mrkt_client.py), so
# "pair" is the floor LEVEL for every MRKT signal, never "model".
MRKT_FLOOR_MIN_LISTED_COUNT: int = int(_env("MRKT_FLOOR_MIN_LISTED_COUNT", "3"))
MRKT_FLOOR_MAX_RATIO_TO_PRICE: Decimal = _decimal_env("MRKT_FLOOR_MAX_RATIO_TO_PRICE", "4.0")
MRKT_SIGNAL_COOLDOWN_MIN: int = int(_env("MRKT_SIGNAL_COOLDOWN_MIN", "60"))

# Правка 4: separate from the shared NOTIFY_ENABLED/TONNEL_NOTIFY_ENABLED
# -- lets the MRKT collector run (accumulating listings/price history/
# sales) without sending notifications, independent of the other two
# pollers. Recipients and bot token are shared -- only whether MRKT
# signals are SENT is separately gated. Default false, same caution as
# TONNEL_NOTIFY_ENABLED had at its own introduction.
MRKT_NOTIFY_ENABLED: bool = _env("MRKT_NOTIFY_ENABLED", "false").strip().lower() in ("1", "true", "yes")

# --- Tonnel model-floor criterion (this delivery) ---
# MEASURED, not a guess (20 significant Tonnel price drops spot-checked):
# a Tonnel pair floor (same collection+model+backdrop, self-excluded) is
# ABSENT 20/20 times -- Tonnel's feed is roughly half Portals' rate, so a
# second listing of the exact same pair essentially never coexists in
# the book. Tonnel's pair_floor query is therefore NOT used for signal
# formation anymore (tonnel_poller.py) -- only the MODEL floor (same
# collection+model, no backdrop, self-excluded) has real depth: 3-11
# listings measured per model vs. 0-1 at pair level.
#
# This model-level floor is confirmed NOT reliable below a minimum book
# depth -- measured ratio of the second-cheapest price to the floor,
# by listed_count: at 2 listings, 4.64/1.87/2.86/1.63 (the floor is
# often just one arbitrarily-priced lot); by 5-6 listings the ratios
# converge to 1.05-1.19; by 8+ they're at or near 1.00. Extreme case:
# Durov's Glasses, Vampire Gaze -- 2 listings, 110 and 510 -- calling
# 110 "the floor" would be wrong. TONNEL_MODEL_MIN_LISTED_COUNT is that
# threshold, DERIVED from this measurement (not copied from Portals'
# FLOOR_MIN_LISTED_COUNT=3, which answers a different question at pair
# level) -- below it, tonnel_poller.py records
# model_floor_status="thin_model_book" instead of "ok", and no signal
# forms (see signals.py's _floor_and_level: only status=="ok" floors are
# ever used).
#
# IMPORTANT (see README): a Tonnel signal's floor_level is ALWAYS
# "model" -- this is NOT the same situation as Portals' model level
# (rejected there precisely BECAUSE a real pair floor exists and the
# model level is a strictly worse fallback that only misleads). Tonnel
# has no pair floor to fall back FROM -- the model level here, gated by
# this measured depth threshold, is the only basis this project actually
# has data for. Do not conflate the two "model level" concepts.
TONNEL_MODEL_MIN_LISTED_COUNT: int = int(_env("TONNEL_MODEL_MIN_LISTED_COUNT", "5"))

# --- API ---
# Domain confirmed via apiUrl in the frontend JS bundle + live authorized
# 200 responses with correctly-shaped bodies. NOT confirmed via direct
# observation of live mini-app network traffic. portals-market.com (with an
# "s") has no A record and must never be used, regardless of what
# third-party open-source libraries reference.
BASE_URL: str = "https://portal-market.com/api"

HEADERS_STATIC: dict[str, str] = {
    "Origin": "https://portal-market.com",
    "Referer": "https://portal-market.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}
