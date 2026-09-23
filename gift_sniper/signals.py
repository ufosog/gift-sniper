"""Single source of truth for "what counts as a clean, actionable
price-drop signal". Moved out of report.py so this logic exists in
exactly ONE place -- both the offline report and the Telegram notifier
(notifier.py, poller.py) call into this module rather than each
maintaining their own copy of the filter cascade.

The cascade (unchanged from the report.py delivery this was extracted
from): is_anomaly -> noise -> is_ladder -> floor no_data -> thin-book ->
is_implausible -> is_bulk_update -> CLEAN. See each stage's docstring/
comments below for the measured reasoning behind it.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from . import config, db

# Below this many "gone" (disappeared, not-since-reappeared) listings for
# a (collection_id, model_name, backdrop_name) pair within
# LIQUIDITY_WINDOW_HOURS, the liquidity numbers are NOT shown -- see
# Правка 3: "Если данных меньше трёх — писать 'недостаточно данных',
# число не выдумывать."
MIN_LIQUIDITY_SAMPLE = 3


@dataclass
class Signal:
    """One clean, actionable price-drop signal -- everything a caller
    (report.py's line-item table, notifier.py's Telegram message) needs,
    already computed. `discount`/`ratio` are against `floor_nano` at
    `floor_level` ("pair" or "model" -- never mixed, see _floor_and_level)
    from `floor_source` ("at_drop": fresh, moment-of-drop query, or
    "snapshot": backfilled from the current floor_snapshots row).

    profit_before_withdrawal_nano / profit_nano / profit_usd are computed
    at BOTH floor levels now (ДОПОЛНЕНИЕ, min-profit-threshold delivery
    -- see compute_profit_nano/signals.run_cascade's below_min_profit
    stage). At level="model" they carry `profit_is_estimate=True`:
    confirmed live (Durov's Glasses #3685, Underwater + Chocolate: price
    91.37, "floor" 115.00 at level=model -- bot showed ~20.98 TON
    profit, but that lot was ITSELF the cheapest in its (model,
    backdrop) pair; the next real offer was 94.00, real flip potential
    ~2.5 TON before fees). The model floor is a DIFFERENT item's price
    (different backdrop) -- not a sale price this listing is GUARANTEED
    to achieve, so this number is an ESTIMATE, not a certainty -- every
    consumer (report.py, notifier.py) that displays it should flag it as
    such via profit_is_estimate, never present it with the same
    confidence as a level="pair" number. It is still a real,
    non-fabricated arithmetic result (unlike the old None), and still
    worth filtering garbage signals by -- see below_min_profit.
    `discount`/`ratio` remain valid at both levels regardless (they're
    pure price comparisons, not sale-price claims).
    """
    listing_external_id: str
    tg_id: str | None
    collection_id: str | None
    collection_name: str | None
    model_name: str | None
    backdrop_name: str | None
    symbol_name: str | None
    gift_number: int | None
    photo_url: str | None
    animation_url: str | None
    currency: str
    old_price_nano: int
    new_price_nano: int
    delta_pct: Decimal
    observed_at: datetime
    floor_nano: int
    floor_source: str  # "at_drop" | "snapshot"
    floor_level: str  # "pair" | "model"
    listed_count: int
    ratio: Decimal  # floor_nano / new_price_nano
    discount: Decimal  # 1 - new_price/floor
    profit_before_withdrawal_nano: int | None
    profit_nano: int | None
    profit_usd: Decimal | None
    # ДОПОЛНЕНИЕ (min-profit-threshold delivery): profit is now computed
    # at BOTH floor levels (never None -- see compute_profit_nano) so
    # the below_min_profit cascade stage has a real number to filter on
    # for every row, including level="model" (previously None there,
    # which meant 548/1379 measured Portals model-level "clean" signals
    # -- many with negative or two-cent profit -- could never be
    # filtered by profit at all). True at level="model" -- per spec,
    # this profit is an ESTIMATE: the model floor is a DIFFERENT
    # backdrop's price, not a sale price this exact listing could
    # achieve (see compute_profit_nano's docstring) -- callers (report.py/
    # notifier.py) should visually flag this, never present it with the
    # same confidence as a level="pair" number.
    profit_is_estimate: bool = False
    # "portals" | "tonnel" | "mrkt" -- which source this signal came
    # from. Drives notifier.py's deep-link choice and the profit formula
    # in _signal_from_row (Tonnel's 10% BUYER fee vs. Portals' 2% SELLER
    # fee vs. MRKT's fee-already-included salePrice -- never the same
    # formula twice).
    marketplace: str = "portals"
    # Liquidity, from listing_lifecycle (schema v8) -- see
    # db.pair_liquidity_stats(). None when fewer than 3 gone listings
    # have been observed for this (collection_id, model_name,
    # backdrop_name) pair in the last LIQUIDITY_WINDOW_HOURS -- the
    # number is deliberately NOT fabricated below that sample size.
    pair_gone_count: int | None = None
    pair_median_time_to_gone_hours: Decimal | None = None
    # Правка 3 (unified-notification delivery): cross-check is a PURE
    # PRE-SEND FILTER now (cross_check.py) -- it decides only whether to
    # send, never anything about display (notifier.py's header is always
    # "ЛИСТИНГ ✓" regardless of this value, see its docstring for why the
    # earlier checkmark-tied-to-verdict design was removed: it produced a
    # real contradiction live, Victory Medal #86056 rejected as a Portals
    # signal with a "worse"-equivalent verdict while simultaneously sent
    # as a separate cross-market notification). NOT populated by
    # clean_signals() itself (this module stays network-free, per its own
    # design) -- poller.py/tonnel_poller.py call cross_check.cross_check()
    # to fill these in before deciding whether to send at all.
    # "not_checked" (the default) is NOT a blocking verdict -- see
    # cross_check.BLOCKING_VERDICTS, which contains only
    # "skipped_neighbour_cheaper".
    cross_verdict: str = "not_checked"
    # neighbour_marketplace names whichever marketplace was actually
    # queried ("tonnel" for a Portals signal, "portals" for a Tonnel
    # signal); neighbour_floor_nano is already fee-adjusted where
    # applicable (Tonnel's buyer fee when Tonnel is the neighbour,
    # unadjusted when Portals is the neighbour -- Portals has no buyer
    # fee, see cross_check.py). neighbour_listed_count backs
    # CROSS_MIN_NEIGHBOUR_COUNT's "neighbour_thin" verdict.
    neighbour_marketplace: str | None = None
    neighbour_floor_nano: int | None = None
    neighbour_listed_count: int = 0
    neighbour_status: str = "not_checked"  # "not_checked" | "ok" | "no_data" | "error"


def _thresholds_for(marketplace: str) -> dict:
    """Правка 1: the cascade's marketplace-specific knobs -- same filter
    logic (run_cascade below), different thresholds per source. Tonnel's
    values are Portals' own defaults copied over as a STARTING POINT,
    UNMEASURED for Tonnel -- see config.py / README.
    """
    if marketplace == "tonnel":
        return {
            "price_drop_min_pct": config.TONNEL_PRICE_DROP_MIN_PCT,
            "floor_min_listed_count": config.TONNEL_FLOOR_MIN_LISTED_COUNT,
            "floor_max_ratio_to_price": config.TONNEL_FLOOR_MAX_RATIO_TO_PRICE,
        }
    if marketplace == "mrkt":
        return {
            "price_drop_min_pct": config.MRKT_PRICE_DROP_MIN_PCT,
            "floor_min_listed_count": config.MRKT_FLOOR_MIN_LISTED_COUNT,
            "floor_max_ratio_to_price": config.MRKT_FLOOR_MAX_RATIO_TO_PRICE,
        }
    return {
        "price_drop_min_pct": config.PRICE_DROP_MIN_PCT,
        "floor_min_listed_count": config.FLOOR_MIN_LISTED_COUNT,
        "floor_max_ratio_to_price": config.FLOOR_MAX_RATIO_TO_PRICE,
    }


def _floor_and_level(r, min_listed_count: int = 1) -> tuple[Decimal | None, str | None]:
    """Hierarchy of comparison: pair level first (the precise,
    model+backdrop comparison), model level as a fallback (a deliberately
    coarser comparison -- different backdrops price differently within
    one model, e.g. Black 22.49 vs. Emperor's median 4.10 measured live).
    Never both -- callers must never mix pair- and model-level signals
    into one distribution, so callers must always branch on the returned
    level, not just the floor value.

    `min_listed_count` (FLOOR_MIN_LISTED_COUNT) is a floor of its own
    applied uniformly to BOTH levels: confirmed live, a floor computed
    from a single OTHER listing (listed_count_excl_self == 1) is
    frequently just one seller's mispriced lot, not a market floor (18/20
    manually-reviewed "clean" signals were exactly this). Default 1 keeps
    callers that need to see every row regardless of count (e.g. the
    thin-book breakdown itself) unaffected.
    """
    if (
        r["pair_floor_status"] == "ok"
        and r["pair_floor_excl_self_nano"] not in (None, 0)
        and r["pair_listed_count_excl_self"] >= min_listed_count
    ):
        return Decimal(r["pair_floor_excl_self_nano"]), "pair"
    if (
        r["model_floor_status"] == "ok"
        and r["model_floor_excl_self_nano"] not in (None, 0)
        and r["model_listed_count_excl_self"] >= min_listed_count
    ):
        return Decimal(r["model_floor_excl_self_nano"]), "model"
    return None, None


def backfill_ladder(
    conn: sqlite3.Connection, now: datetime | None = None, marketplace: str = "portals"
) -> set[str]:
    """Idempotent, same pattern as report.py's backfill_name_collisions: a
    listing qualifies as a "ladder" (relister-bot walking its own price
    down) if it had >= LADDER_MIN_DROPS SIGNIFICANT drops (abs(delta_pct)
    >= PRICE_DROP_MIN_PCT, marketplace-specific -- see _thresholds_for)
    within the last LADDER_WINDOW_HOURS (as of `now`). Confirmed live:
    Low Rider #23134, 6 steps of exactly 5%, ~30 minutes apart -- a real
    ladder. Counting ALL drops regardless of size was measured to be
    wrong: it caught 11090/11850 (94%) of all drops as "ladders", because
    bot relisting ticks of 0.0-0.1% (already below the noise threshold
    and destined to be filtered as noise anyway) dominate the raw drop
    count. Restricting to significant drops before counting steps is
    what actually isolates real walk-down behavior -- see also the
    filter cascade order in run_cascade, which must filter noise BEFORE
    checking is_ladder for the same reason. Once a listing qualifies,
    ALL of its price_history rows (both directions, noise included) are
    flagged is_ladder=1 -- this is a property of the LOT's behavior, not
    of one row.

    `marketplace` (Tonnel signals delivery) scopes BOTH the query and
    the two UPDATE statements below -- without this, resetting
    is_ladder=0 for one marketplace's backfill pass would wipe out the
    OTHER marketplace's already-computed ladder flags every time either
    one runs (both pollers are separate processes against the same DB
    file).

    `now` defaults to the real current time (production behavior,
    unchanged) but is overridable -- tests must pass a FIXED `now` rather
    than relying on wall-clock time to stay within LADDER_WINDOW_HOURS of
    hardcoded fixture dates.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=config.LADDER_WINDOW_HOURS)
    rows = conn.execute(
        """
        SELECT listing_external_id, COUNT(*) FROM price_history
        WHERE marketplace = ?
          AND new_price_nano < old_price_nano
          AND is_noise = 0
          AND observed_at >= ?
        GROUP BY listing_external_id
        """,
        (marketplace, window_start.isoformat()),
    ).fetchall()
    ladder_listings = {ext_id for ext_id, drop_count in rows if drop_count >= config.LADDER_MIN_DROPS}

    with conn:
        conn.execute("UPDATE price_history SET is_ladder = 0 WHERE marketplace = ?", (marketplace,))
        if ladder_listings:
            placeholders = ",".join("?" for _ in ladder_listings)
            conn.execute(
                f"UPDATE price_history SET is_ladder = 1 "
                f"WHERE marketplace = ? AND listing_external_id IN ({placeholders})",
                (marketplace, *ladder_listings),
            )
    return ladder_listings


def _ladder_listings_live(conn: sqlite3.Connection, marketplace: str, candidate_rows) -> set[tuple[str, str]]:
    """ДЕФЕКТ (min-1.7-signals-per-lot delivery, then a SECOND, more
    fundamental bug found on live data after the first fix): run_cascade
    used to read price_history.is_ladder, a column only ever kept
    current by a SEPARATE call to backfill_ladder() -- which report.py
    made, but NEITHER poller ever did. Confirmed live: Bonded Ring/Cat
    Bed/Pine Green, 19 consecutive ~5% drops, ALL 19 rows is_ladder=0,
    9 reached "clean".

    FIRST fix (wrong): computed the window ending at `now` (the call's
    evaluation time), same as backfill_ladder. This works ONLY when
    `now` is close to the drops being evaluated (true for a live poller
    cycle) -- confirmed BROKEN on real data for anything evaluated well
    after the fact (a report run, or `since` widened past
    LADDER_WINDOW_HOURS): Clover Pin/Maple Leaf
    (01a08b5d-27e2-7a62-ae78-3095d9ddf091), 20 real ~5% drops 30 min
    apart, all 4 days before the evaluating `now` -- window_start =
    now - 24h fell AFTER every one of the 20 drops, so NONE of them
    were ever in range and the whole lot silently stopped being
    recognized as a ladder. Proven live: widening `since` from -1d to
    -7d changed the OUTPUT count (0 -> 1 -> 12 -> 15 -> 15) with the
    exact same underlying rows -- `since` only post-filters
    clean_signals()'s output, so a change in it can only ever change
    which of an ALREADY-decided set of clean rows gets returned; the
    fact that it changed the count proves 15 of the 20 were reaching
    cascade.clean in the first place, i.e. the is_ladder decision
    itself (not the since filter) was wrong for old data.

    CORRECT fix: the window is anchored at EACH CANDIDATE ROW's OWN
    observed_at, not at `now` -- and it looks BOTH directions (the
    candidate's LOT is a ladder if there are >= LADDER_MIN_DROPS
    significant drops for that listing within LADDER_WINDOW_HOURS of
    THIS row, before or after it). Looking both directions (not just
    "before") is deliberate, not a rounding choice: it's what makes the
    FIRST 1-2 drops of an already-fully-recorded ladder get recognized
    too, once later drops make the pattern visible -- required by the
    "20 drops in one lot -> 0 signals, not 18" test. It also makes the
    result depend only on the lot's own drop history, never on `now` or
    `since`, so a delayed report run gives the SAME answer as a
    same-day one. One query per candidate row (per spec: "кандидатов
    после предыдущих стадий каскада немного").

    Returns a set of (listing_external_id, observed_at) ROW keys, not
    listing ids -- deliberately a PER-ROW decision, not "flag every row
    of a listing once any one of its rows qualifies". A single lot can
    have an isolated, unrelated significant drop far outside any burst's
    window (months apart, say); flagging the whole listing on the first
    match wrongly swept that isolated drop into an unrelated ladder too
    -- caught by test_item4_drops_outside_window_are_not_counted.

    backfill_ladder() itself is UNCHANGED and kept for report.py's
    historical is_ladder column (audit trail) -- just not the mechanism
    cascade filtering depends on.
    """
    half_window = timedelta(hours=config.LADDER_WINDOW_HOURS)
    ladder_rows: set[tuple[str, str]] = set()
    seen: set[tuple[str, str]] = set()
    for r in candidate_rows:
        ext_id = r["listing_external_id"]
        observed_at = r["observed_at"]
        key = (ext_id, observed_at)
        # PER-ROW decision, deliberately NOT "once this listing has one
        # ladder row, treat the whole listing as a ladder" -- a single
        # lot can have an isolated significant drop MONTHS after (or
        # before) an unrelated 3+-drop burst; lumping them together on
        # first match wrongly swept the isolated drop into the burst.
        if key in seen:
            continue
        seen.add(key)
        t = datetime.fromisoformat(observed_at)
        window_start = (t - half_window).isoformat()
        window_end = (t + half_window).isoformat()
        count = conn.execute(
            """
            SELECT COUNT(*) FROM price_history
            WHERE marketplace = ?
              AND listing_external_id = ?
              AND new_price_nano < old_price_nano
              AND is_noise = 0
              AND observed_at >= ?
              AND observed_at <= ?
            """,
            (marketplace, ext_id, window_start, window_end),
        ).fetchone()[0]
        if count >= config.LADDER_MIN_DROPS:
            ladder_rows.add(key)
    return ladder_rows


def realization_rate(depth) -> Decimal:
    """Share of the pair floor a sale is actually expected to realize, by
    book depth (listed_count excluding self). See config.REALIZATION_RATE_*
    for the measured basis. depth 0/None falls into the depth-1 bucket
    (the most conservative) -- such rows never reach sending anyway
    (no_floor_at_send).
    """
    depth = depth or 0
    if depth <= 1:
        return config.REALIZATION_RATE_DEPTH_1
    if depth <= 3:
        return config.REALIZATION_RATE_DEPTH_2_3
    if depth <= 9:
        return config.REALIZATION_RATE_DEPTH_4_9
    return config.REALIZATION_RATE_DEPTH_10


def compute_profit_nano(marketplace: str, floor_nano, price_nano, depth) -> tuple[int, int]:
    """SINGLE SOURCE OF TRUTH for the profit formula, per spec ("не
    дублировать формулы по файлам") -- every caller (the
    below_min_profit cascade stage, _signal_from_row, report.py) goes
    through this, never reimplements it. Returns
    (profit_before_withdrawal_nano, profit_nano) -- identical for
    Tonnel/MRKT (neither has a separate withdrawal-fee concept, see
    README), distinct for Portals.

    REALIZATION RATE (realization-rate delivery): the floor is a CEILING
    on the sale price, not the expected one -- measured on confirmed MRKT
    sales, no sale ever cleared above the pair floor (see config's
    REALIZATION_RATE_* comment). So the expected sale price is
    floor * realization_rate(depth), and every formula below uses that
    instead of the raw floor. `depth` is required so no caller can
    silently skip it.

    Formulas (fixed here once, per spec), RATE = realization_rate(depth):
      Portals: floor*RATE*(1 - MARKETPLACE_FEE_RATE) - price - WITHDRAWAL_FEE_FLAT_NANO
      Tonnel:  floor*RATE - price*1.1   (buyer pays price+10%, no seller-side deduction found)
      MRKT:    floor*RATE*(1 - MARKETPLACE_FEE_RATE) - price
               (MRKT's confirmed 2% BUYER fee is already baked into
               salePrice on BOTH sides of this subtraction -- so unlike
               Portals, no separate fee multiplier belongs on `price`;
               MRKT's WITHDRAWAL fee is an OPEN QUESTION, not measured
               -- see README -- so none is subtracted, same "no
               confirmed number, no fabricated one" discipline as
               Tonnel's missing seller fee).

    Computed for ANY floor level (pair or model) -- the caller decides
    what level="model" profit MEANS (an estimate, see Signal's
    profit_is_estimate) but the arithmetic itself never depends on
    level, so this function doesn't take one.
    """
    floor = Decimal(floor_nano) * realization_rate(depth)
    price = Decimal(price_nano)
    if marketplace == "tonnel":
        profit_nano = int(floor - price * Decimal("1.1"))
        return profit_nano, profit_nano
    if marketplace == "mrkt":
        profit_nano = int(floor * (1 - config.MARKETPLACE_FEE_RATE) - price)
        return profit_nano, profit_nano
    profit_before_withdrawal = floor * (1 - config.MARKETPLACE_FEE_RATE) - price
    profit_nano = int(profit_before_withdrawal - config.WITHDRAWAL_FEE_FLAT_NANO)
    return int(profit_before_withdrawal), profit_nano


def _floor_instability(conn: sqlite3.Connection, marketplace: str, r) -> Decimal | None:
    """max/min of the pair floor (pair_floor_excl_self_nano) across
    floor_snapshots rows of the SAME (collection, model, backdrop) pair,
    fetched within [observed_at - FLOOR_STABILITY_WINDOW_HOURS, observed_at].
    floor_snapshots holds one row per listing (upserted), so the
    "snapshots of a pair" are rows of different listings in that pair.
    Returns None when fewer than 3 snapshots exist -- not enough data to
    judge, and never a reason to drop a row.
    """
    observed_at = datetime.fromisoformat(r["observed_at"])
    window_start = (observed_at - timedelta(hours=config.FLOOR_STABILITY_WINDOW_HOURS)).isoformat()
    row = conn.execute(
        """
        SELECT MIN(f.pair_floor_excl_self_nano), MAX(f.pair_floor_excl_self_nano), COUNT(*)
        FROM floor_snapshots f
        JOIN listings l ON l.external_id = f.listing_external_id AND l.marketplace = f.marketplace
        WHERE f.marketplace = ?
          AND l.collection_name IS ?
          AND f.model_name = ?
          AND f.backdrop_name IS ?
          AND f.pair_floor_excl_self_nano > 0
          AND f.floor_fetched_at >= ?
          AND f.floor_fetched_at <= ?
        """,
        (marketplace, r["collection_name"], r["model_name"], r["backdrop_name"], window_start, r["observed_at"]),
    ).fetchone()
    min_floor, max_floor, count = row[0], row[1], row[2]
    if count < 3 or not min_floor:
        return None
    return Decimal(max_floor) / Decimal(min_floor)


def _floor_and_source(r) -> tuple[Decimal | None, str | None, str | None, int, str | None]:
    """Returns (floor, source, level, listed_count_excl_self,
    fetched_at_iso). source distinguishes at_drop (fresh, moment-of-drop
    query) from snapshot (backfilled from floor_snapshots); level
    distinguishes pair vs model comparison -- an orthogonal axis. Level
    "pair"/"model" for at_drop comes from floor_level_at_drop (set by
    poller.py); for the snapshot fallback it comes from the same
    pair->model hierarchy used everywhere else (_floor_and_level).
    listed_count_excl_self is used by the thin-book filter below -- NOT
    gated here, so the "floor no_data" cascade stage keeps its original
    meaning (no floor at all, any liquidity).

    `fetched_at_iso` (ДОПОЛНЕНИЕ, floor-freshness delivery): when the
    floor came from at_drop, this is price_history's OWN
    floor_fetched_at (the moment of THIS drop's re-query -- always
    fresh by construction, never stale). When from a snapshot, this is
    floor_snapshots.floor_fetched_at (aliased `snapshot_fetched_at` in
    run_cascade's SELECT to avoid colliding with price_history's own
    same-named column) -- THIS is the value the stale_floor cascade
    stage actually checks the age of; see run_cascade.
    """
    if r["floor_at_drop_nano"] not in (None, 0):
        level = r["floor_level_at_drop"] or "pair"
        listed_count = r["floor_listed_count_at_drop"] or 0
        return Decimal(r["floor_at_drop_nano"]), "at_drop", level, listed_count, r["floor_fetched_at"]
    floor, level = _floor_and_level(r, 1)
    if floor is not None:
        listed_count = r["pair_listed_count_excl_self"] if level == "pair" else r["model_listed_count_excl_self"]
        return floor, "snapshot", level, listed_count, r["snapshot_fetched_at"]
    return None, None, None, 0, None


def _find_bulk_update_ids(rows_subset) -> set[tuple[str, str]]:
    """Several DIFFERENT listings in the same collection recording the
    identical delta_pct within SAME_SECOND_WINDOW seconds of each other
    is one seller adjusting multiple lots at once, not independent market
    signals -- confirmed live (three Khabib's Papakha lots, same
    19:14:32 timestamp, same 24.60->23.91 drop).
    """
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows_subset:
        groups[(r["collection_name"], str(r["delta_pct"]))].append(r)

    bulk_keys: set[tuple[str, str]] = set()
    for group_rows in groups.values():
        group_rows.sort(key=lambda r: r["observed_at"])
        times = [datetime.fromisoformat(r["observed_at"]) for r in group_rows]
        cluster = [group_rows[0]]
        for i in range(1, len(group_rows)):
            if (times[i] - times[i - 1]).total_seconds() <= config.SAME_SECOND_WINDOW:
                cluster.append(group_rows[i])
            else:
                if len({r["listing_external_id"] for r in cluster}) >= 2:
                    bulk_keys.update((r["listing_external_id"], r["observed_at"]) for r in cluster)
                cluster = [group_rows[i]]
        if len({r["listing_external_id"] for r in cluster}) >= 2:
            bulk_keys.update((r["listing_external_id"], r["observed_at"]) for r in cluster)
    return bulk_keys


@dataclass
class CascadeResult:
    """Every stage of the filter cascade, kept separate so report.py can
    print exact per-stage removed/remaining counts (each stage's count is
    drawn from the REMAINDER of the prior stage) without re-deriving the
    same filtering logic a second time.
    """
    rows: list = field(default_factory=list)  # ALL price_history rows joined, both drops and raises
    drops: list = field(default_factory=list)
    raises: list = field(default_factory=list)
    anomalies: list = field(default_factory=list)
    noise: list = field(default_factory=list)
    ladders: list = field(default_factory=list)
    ladder_listings: set = field(default_factory=set)
    no_floor: list = field(default_factory=list)
    thin_book: list = field(default_factory=list)
    thin_book_cnt1: int = 0
    thin_book_cnt2: int = 0
    implausible: list = field(default_factory=list)
    # ДОПОЛНЕНИЕ (price-above-own-floor delivery): mandatory for ALL
    # marketplaces and ALL floor levels -- price must be strictly BELOW
    # its own floor for a real discount to exist. Measured on 25 Portals
    # model-level signals: 18/25 (72%) were priced ABOVE their own model
    # floor (e.g. Bling Binky/Regent: price 210.00, floor 34.00, x6.2) --
    # a lot priced above the cheapest known price for its own model can
    # never be a bargain, regardless of how big its OWN price drop looked.
    # This SUPERSEDES the earlier Tonnel-only "no_discount" stage (same
    # check, previously duplicated for one marketplace only) -- one
    # shared stage now, not one per marketplace. See run_cascade.
    price_above_own_floor: list = field(default_factory=list)
    # ДОПОЛНЕНИЕ (floor-freshness delivery): a snapshot-sourced floor
    # (source="snapshot") older than MAX_SNAPSHOT_AGE_MIN is refused
    # outright -- measured live, Tonnel snapshots have been found 8-12h
    # stale, during which the market can reprice many times over.
    # at_drop floors are NEVER stale by construction (fetched at the
    # exact moment of the drop being evaluated) so this stage can only
    # ever remove snapshot-sourced rows. Placed right after "no floor"
    # -- every LATER stage (price_above_own_floor, below_min_profit,
    # thin-book, is_implausible) compares against the floor value, so a
    # stale one must be thrown out before any of them trust it.
    stale_floor: list = field(default_factory=list)
    # ДОПОЛНЕНИЕ (min-profit-threshold delivery): profit below
    # MIN_SIGNAL_PROFIT_TON is not a real opportunity regardless of how
    # good the discount PERCENTAGE looks -- measured live, 548/1379
    # (40%) of Portals' "clean" signals had ratio < 1.05, including
    # actually-negative-profit rows (Joyful Bundle/Pepe Bag: price
    # 32.45, floor 32.50, profit_usd = -4.54) that were nonetheless
    # being treated as clean. See compute_profit_nano -- the absolute
    # TON amount is what actually determines whether a flip is worth
    # doing, not a fixed ratio (a 5% "discount" is $0.12 on a 15 TON lot
    # but $11.86 on a 300 TON one).
    below_min_profit: list = field(default_factory=list)
    # ПРАВКА 2 (realization-rate delivery): pair floor swung >=
    # FLOOR_MAX_INSTABILITY within FLOOR_STABILITY_WINDOW_HOURS -- see
    # run_cascade and config.FLOOR_MAX_INSTABILITY.
    unstable_floor: list = field(default_factory=list)
    bulk_updates: list = field(default_factory=list)
    # ДЕФЕКТ 1 (systemic-check delivery): final unconfigurable backstop --
    # see the "no_floor_at_send" comment inside run_cascade.
    no_floor_at_send: list = field(default_factory=list)
    clean: list = field(default_factory=list)  # final survivors, raw sqlite3.Row


# backfill_ladder rewrites is_ladder over the whole table: 4.4 s on the
# live DB (1.04 M Portals rows, 2026-09-19), every notify pass. Selection
# never reads that column (it uses _ladder_listings_live), so the column
# only needs to stay roughly current: at most once per interval per
# (database file, marketplace). report.py still calls backfill_ladder itself.
BACKFILL_LADDER_MIN_INTERVAL_SEC = 600
_last_backfill: dict[tuple[str, str], float] = {}


def _backfill_ladder_throttled(conn, now, marketplace: str) -> None:
    import time as _time
    # Keyed by the database FILE, not id(conn): a new connection can reuse
    # a closed one's id. An in-memory database (tests) is never throttled.
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not path:
        backfill_ladder(conn, now, marketplace=marketplace)
        return
    key = (path, marketplace)
    mono = _time.monotonic()
    last = _last_backfill.get(key)
    if last is not None and mono - last < BACKFILL_LADDER_MIN_INTERVAL_SEC:
        return
    _last_backfill[key] = mono
    backfill_ladder(conn, now, marketplace=marketplace)


# clean_signals(since=...) loads price_history only from since minus this
# margin. Only the bulk-update stage compares rows WITHIN the loaded set,
# over SAME_SECOND_WINDOW seconds; the ladder and unstable-floor stages run
# their own SQL. One hour is far above that window.
CASCADE_CONTEXT_MARGIN = timedelta(hours=1)


def run_cascade(
    conn: sqlite3.Connection, now: datetime | None = None, marketplace: str = "portals",
    min_observed_at: datetime | None = None,
) -> CascadeResult:
    """Runs the full filter cascade ONCE against price_history, joined
    with listings and floor_snapshots. This is the single source of truth
    both report.py's diagnostics and clean_signals() (and therefore the
    Telegram notifier) are built on -- if the filter changes, it changes
    here, nowhere else. Правка 1 (Tonnel signals delivery): SAME cascade
    for both marketplaces, never duplicated -- `marketplace` scopes the
    query and selects the right thresholds (see _thresholds_for).

    ORDER MATTERS, confirmed live: noise must be filtered BEFORE
    is_ladder, not after -- see backfill_ladder's docstring (the same
    reasoning applies to _ladder_listings_live below). Filtering noise
    first makes each stage's removed-count mean what it says.

    ДЕФЕКТ (min-1.7-signals-per-lot delivery): is_ladder is computed
    LIVE here (see _ladder_listings_live), not read from a column a
    separate backfill_ladder() pass was supposed to have populated
    beforehand -- confirmed live, neither poller ever called
    backfill_ladder, so is_ladder was always 0 at the moment a signal
    was selected for sending (Bonded Ring/Cat Bed/Pine Green: 19
    consecutive ~5% drops, all 19 rows is_ladder=0, 9 reached "clean").
    backfill_ladder() itself is unchanged, still called separately by
    report.py, purely for the persisted historical is_ladder column
    (audit trail) -- selection no longer depends on it.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    thresholds = _thresholds_for(marketplace)

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ph.*, l.collection_name, l.model_name, l.backdrop_name, l.gift_number,
               l.tg_id, l.image_url AS photo_url, l.animation_url, l.currency, l.symbol_name, l.collection_id,
               f.pair_floor_excl_self_nano, f.pair_floor_status, f.pair_listed_count_excl_self,
               f.model_floor_excl_self_nano, f.model_floor_status, f.model_listed_count_excl_self,
               f.floor_fetched_at AS snapshot_fetched_at
        FROM price_history ph
        JOIN listings l ON l.external_id = ph.listing_external_id AND l.marketplace = ph.marketplace
        LEFT JOIN floor_snapshots f ON f.listing_external_id = ph.listing_external_id AND f.marketplace = ph.marketplace
        WHERE ph.marketplace = ? AND ph.observed_at >= ?
        """,
        # Measured 2026-09-19: without a lower bound every live notify pass
        # loaded all 1,043,670 Portals rows; the Portals poller used 1.1 GB
        # RAM + 1 GB swap and froze a 2 GB server. min_observed_at=None
        # (report.py) keeps the whole history.
        (marketplace, min_observed_at.isoformat() if min_observed_at is not None else ""),
    ).fetchall()

    result = CascadeResult(rows=list(rows))
    result.drops = [r for r in rows if Decimal(r["delta_pct"]) < 0]
    result.raises = [r for r in rows if Decimal(r["delta_pct"]) >= 0]

    remaining = result.drops
    result.anomalies = [r for r in remaining if r["is_anomaly"]]
    remaining = [r for r in remaining if not r["is_anomaly"]]

    result.noise = [r for r in remaining if r["is_noise"]]
    remaining = [r for r in remaining if not r["is_noise"]]

    ladder_rows = _ladder_listings_live(conn, marketplace, remaining)
    result.ladders = [r for r in remaining if (r["listing_external_id"], r["observed_at"]) in ladder_rows]
    remaining = [r for r in remaining if (r["listing_external_id"], r["observed_at"]) not in ladder_rows]
    result.ladder_listings = {ext_id for ext_id, _observed_at in ladder_rows}
    # Also keep the persisted is_ladder column in sync, purely so ANY
    # OTHER code that reads price_history.is_ladder directly (not through
    # this cascade) never sees a stale value -- restored after removing
    # it broke that compatibility (see README's newest entry). Filtering
    # above never reads this column back; it only ever uses
    # `ladder_listings`, computed fresh a few lines up.
    _backfill_ladder_throttled(conn, now, marketplace)

    result.no_floor = [r for r in remaining if _floor_and_source(r)[0] is None]
    remaining = [r for r in remaining if _floor_and_source(r)[0] is not None]

    # --- stale_floor (ДОПОЛНЕНИЕ, floor-freshness delivery): a ---
    # --- snapshot-sourced floor older than MAX_SNAPSHOT_AGE_MIN, ---
    # --- measured AT THE MOMENT OF THE DROP (observed_at) -- NOT ---
    # --- against whatever wall-clock time run_cascade happens to be ---
    # --- called at, which could be irrelevant hours/days later for a ---
    # --- historical report run. The question this stage answers is ---
    # --- "was this floor still trustworthy WHEN THE DROP HAPPENED", ---
    # --- same question poller.py's own at-drop re-query already ---
    # --- answers for the "at_drop" source -- see CascadeResult's ---
    # --- docstring for the measured reasoning. at_drop floors are ---
    # --- exempt by construction (fetched at the exact moment of the ---
    # --- drop being evaluated), so this can only remove ---
    # --- snapshot-sourced rows. ---
    max_age = timedelta(minutes=config.MAX_SNAPSHOT_AGE_MIN)

    def _is_stale(r) -> bool:
        _floor, source, _level, _cnt, fetched_at_iso = _floor_and_source(r)
        if source != "snapshot" or fetched_at_iso is None:
            return False
        fetched_at = datetime.fromisoformat(fetched_at_iso)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        observed_at = datetime.fromisoformat(r["observed_at"])
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        return (observed_at - fetched_at) > max_age

    result.stale_floor = [r for r in remaining if _is_stale(r)]
    remaining = [r for r in remaining if not _is_stale(r)]

    # --- price_above_own_floor (ДОПОЛНЕНИЕ): mandatory, ALL marketplaces ---
    # --- and ALL floor levels -- new_price_nano must be strictly BELOW ---
    # --- floor_nano, or there is no discount at all. Placed EARLY, right ---
    # --- after floor no_data, on purpose: a lot priced at/above its own ---
    # --- floor must never reach the expensive later stages (in ---
    # --- particular cross-check's network call to the other marketplace ---
    # --- -- see cross_check.py -- which only ever runs against ---
    # --- clean_signals() output, so filtering here means it's never ---
    # --- spent on a signal that was never going anywhere). ---
    def _price_below_floor(r) -> bool:
        floor, *_ = _floor_and_source(r)
        return floor is not None and Decimal(r["new_price_nano"]) < floor

    result.price_above_own_floor = [r for r in remaining if not _price_below_floor(r)]
    remaining = [r for r in remaining if _price_below_floor(r)]

    # --- below_min_profit (ПРАВКА 1, min-profit-threshold delivery): ---
    # --- placed immediately after price_above_own_floor, per spec -- ---
    # --- computed via the SAME compute_profit_nano() every other ---
    # --- caller uses (report.py, _signal_from_row), never a second ---
    # --- copy of the formula. Applies at BOTH floor levels -- profit ---
    # --- is no longer None at level="model" (see Signal's docstring), ---
    # --- so this is the stage that actually catches the 548 measured ---
    # --- Portals model-level rows a None profit could never filter. ---
    def _profit_nano(r) -> int:
        floor, _source, _level, listed_count, _fetched_at = _floor_and_source(r)
        _before, profit = compute_profit_nano(marketplace, floor, r["new_price_nano"], listed_count)
        return profit

    min_profit_nano = config.MIN_SIGNAL_PROFIT_NANO
    result.below_min_profit = [r for r in remaining if _profit_nano(r) < min_profit_nano]
    remaining = [r for r in remaining if _profit_nano(r) >= min_profit_nano]

    # --- unstable_floor (realization-rate delivery, ПРАВКА 2): a pair ---
    # --- whose floor swung >= FLOOR_MAX_INSTABILITY (max/min) over the ---
    # --- FLOOR_STABILITY_WINDOW_HOURS before the drop has no reliable ---
    # --- profit estimate -- the floor at signal time may mean nothing ---
    # --- an hour later. Fewer than 3 snapshots -> skipped, never dropped. ---
    # --- Window anchored to the row's own observed_at, never to `now` ---
    # --- (same lesson as the ladder window). ---
    instability_cache: dict = {}

    def _is_unstable(r) -> bool:
        key = (r["collection_name"], r["model_name"], r["backdrop_name"], r["observed_at"])
        if key not in instability_cache:
            instability_cache[key] = _floor_instability(conn, marketplace, r)
        spread = instability_cache[key]
        return spread is not None and spread >= config.FLOOR_MAX_INSTABILITY

    result.unstable_floor = [r for r in remaining if _is_unstable(r)]
    remaining = [r for r in remaining if not _is_unstable(r)]

    # --- thin-book filter (FLOOR_MIN_LISTED_COUNT): confirmed live, ---
    # --- 18/20 manually-reviewed "clean" signals had ---
    # --- listed_count_excl_self == 1 -- one other listing's price ---
    # --- taken as "the floor", usually a mispriced/unsold lot. ---
    min_listed_count = thresholds["floor_min_listed_count"]
    result.thin_book = [r for r in remaining if _floor_and_source(r)[3] < min_listed_count]
    remaining = [r for r in remaining if _floor_and_source(r)[3] >= min_listed_count]
    result.thin_book_cnt1 = sum(1 for r in result.thin_book if _floor_and_source(r)[3] == 1)
    result.thin_book_cnt2 = sum(1 for r in result.thin_book if _floor_and_source(r)[3] == 2)

    # --- implausible floor/price ratio (FLOOR_MAX_RATIO_TO_PRICE): a ---
    # --- floor computed from >= FLOOR_MIN_LISTED_COUNT listings can ---
    # --- still be many multiples of the signal's own price -- ---
    # --- confirmed live, ratios of 7-10x were single mispriced ---
    # --- listings, not real discounts. ---
    def _ratio(r) -> Decimal | None:
        floor, *_ = _floor_and_source(r)
        if floor is None:
            return None
        new_price = Decimal(r["new_price_nano"])
        if new_price == 0:
            return None
        return floor / new_price

    max_ratio = thresholds["floor_max_ratio_to_price"]
    result.implausible = [r for r in remaining if (_ratio(r) or Decimal(0)) > max_ratio]
    remaining = [r for r in remaining if not ((_ratio(r) or Decimal(0)) > max_ratio)]

    bulk_ids = _find_bulk_update_ids(remaining)
    result.bulk_updates = [r for r in remaining if (r["listing_external_id"], r["observed_at"]) in bulk_ids]
    remaining = [r for r in remaining if (r["listing_external_id"], r["observed_at"]) not in bulk_ids]

    # --- no_floor_at_send (ДЕФЕКТ 1, systemic-check delivery): a final, ---
    # --- HARDCODED backstop, deliberately NOT driven by
    # --- FLOOR_MIN_LISTED_COUNT/TONNEL_/MRKT_ variants (those ARE
    # --- user-configurable and, unlike the Portals one, carry no >= 2
    # --- floor of their own in config.py -- a misconfigured 0 or 1
    # --- there would let thin_book pass a signal through with a
    # --- listed_count of 0). Confirmed live: 6 sent Tonnel signals had
    # --- floor_level_at_drop=NULL and floor_listed_count_at_drop=0 --
    # --- the user saw a FLOOR number in the notification with no real
    # --- listings behind it. "ни при каких настройках": this stage
    # --- ignores every threshold and just requires a real floor with
    # --- listed_count >= 1, full stop.
    def _floor_missing_at_send(r) -> bool:
        floor, _source, _level, listed_count, _fetched_at = _floor_and_source(r)
        return floor is None or not listed_count or listed_count < 1

    result.no_floor_at_send = [r for r in remaining if _floor_missing_at_send(r)]
    remaining = [r for r in remaining if not _floor_missing_at_send(r)]

    result.clean = remaining
    return result


def _signal_from_row(r, usd_rate: Decimal, marketplace: str = "portals") -> Signal:
    floor, source, level, listed_count, _fetched_at = _floor_and_source(r)
    new_price = Decimal(r["new_price_nano"])
    discount = Decimal(1) - new_price / floor
    ratio = floor / new_price

    # ДОПОЛНЕНИЕ (min-profit-threshold delivery): profit is now computed
    # at EVERY floor level, via the single shared compute_profit_nano()
    # -- per spec, "profit=None for level=model" was itself the bug: it
    # made 548 measured Portals model-level "clean" rows (many with
    # negative or two-cent profit) impossible to filter by profit at
    # all. level="model" profit is an ESTIMATE (profit_is_estimate=True)
    # -- the model floor is a DIFFERENT backdrop's price (confirmed live,
    # Durov's Glasses #3685: "floor" 115.00 was another backdrop's price,
    # the listing itself was the cheapest in its own pair, real next
    # offer was 94.00) -- not a sale price THIS listing could achieve
    # with certainty, but still a real number worth filtering garbage by,
    # per spec ("порог применять и к ним").
    profit_before_withdrawal_nano, profit_nano = compute_profit_nano(marketplace, floor, new_price, listed_count)
    profit_usd = (Decimal(profit_nano) / config.NANO) * usd_rate
    profit_is_estimate = level == "model"

    return Signal(
        listing_external_id=r["listing_external_id"],
        tg_id=r["tg_id"],
        collection_id=r["collection_id"],
        collection_name=r["collection_name"],
        model_name=r["model_name"],
        backdrop_name=r["backdrop_name"],
        symbol_name=r["symbol_name"],
        gift_number=r["gift_number"],
        photo_url=r["photo_url"],
        animation_url=r["animation_url"],
        currency=r["currency"] or config.CURRENCY_DEFAULT,
        old_price_nano=r["old_price_nano"],
        new_price_nano=r["new_price_nano"],
        delta_pct=Decimal(r["delta_pct"]),
        observed_at=datetime.fromisoformat(r["observed_at"]),
        floor_nano=int(floor),
        floor_source=source,
        floor_level=level,
        listed_count=listed_count,
        ratio=ratio,
        discount=discount,
        profit_before_withdrawal_nano=profit_before_withdrawal_nano,
        profit_nano=profit_nano,
        profit_usd=profit_usd,
        profit_is_estimate=profit_is_estimate,
        marketplace=marketplace,
    )


def clean_signals(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    *,
    usd_rate: Decimal = Decimal(1),
    now: datetime | None = None,
    marketplace: str = "portals",
) -> list[Signal]:
    """The public contract: every listing/report/bot caller that needs
    "what are the current clean signals" calls this, and only this --
    never re-derives the cascade. `since` restricts to signals observed
    at or after that timestamp (None = no restriction, matching
    report.py's whole-history behavior). `usd_rate` feeds profit_usd;
    default 1 is a placeholder for callers (like tests) that don't care
    about the USD conversion. `now` is the ladder-window reference point,
    computed live on every call (see run_cascade/_ladder_listings_live --
    NOT dependent on a prior backfill_ladder() pass) -- None defaults to
    real current time.
    `marketplace` (Tonnel signals delivery, Правка 1) selects which
    source's rows/thresholds the cascade runs against -- clean_signals(
    marketplace='tonnel') never returns a Portals row and vice versa
    (see run_cascade's explicit WHERE ph.marketplace = ?).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    lower = since - CASCADE_CONTEXT_MARGIN if since is not None else None
    cascade = run_cascade(conn, now=now, marketplace=marketplace, min_observed_at=lower)
    result = []
    for r in cascade.clean:
        observed_at = datetime.fromisoformat(r["observed_at"])
        if since is not None and observed_at < since:
            continue
        signal = _signal_from_row(r, usd_rate, marketplace=marketplace)
        gone_count, median_hours = db.pair_liquidity_stats(
            conn,
            signal.collection_id,
            signal.model_name,
            signal.backdrop_name,
            since=now - timedelta(hours=config.LIQUIDITY_WINDOW_HOURS),
            marketplace=marketplace,
        )
        # Never fabricate a number below the minimum sample size -- see
        # Правка 3 / config.py. Below this, both fields stay None and
        # notifier.py prints "недостаточно данных о ликвидности".
        if gone_count >= MIN_LIQUIDITY_SAMPLE:
            signal.pair_gone_count = gone_count
            signal.pair_median_time_to_gone_hours = median_hours
        result.append(signal)
    return result


# Правка 1 (removed, this delivery): the separate cross-marketplace
# notification type and its build/format/send machinery are GONE
# entirely -- a cross-marketplace price gap is no longer its own
# notification type, only an input to cross_check.py's send/no-send
# decision on an ordinary signal. See cross_check.py for the verdict
# logic that replaces the earlier confirmed/worse/no_data functions
# (also removed).
