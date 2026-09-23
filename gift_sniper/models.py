from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass
class Listing:
    marketplace: str  # "portals"
    external_id: str  # response field "id" (uuid)
    tg_id: str  # e.g. "IceCream-45374"
    collection_id: str
    collection_name: str  # response field "name"
    gift_number: int  # response field "external_collection_number"
    price_nano: int | None  # Decimal(price) * 10**9
    currency: str  # ALWAYS CURRENCY_DEFAULT: the response has no currency field
    collection_floor_nano: int | None  # response field "floor_price"
    model_name: str | None
    symbol_name: str | None
    backdrop_name: str | None
    model_rarity_raw: Decimal | None  # rarity_per_mille AS RECEIVED, not converted
    symbol_rarity_raw: Decimal | None
    backdrop_rarity_raw: Decimal | None
    image_url: str | None  # response field "photo_url"
    animation_url: str | None
    listed_at: datetime | None
    unlocks_at: datetime | None
    status: str
    first_seen_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FloorSnapshot:
    listing_external_id: str
    model_name: str
    backdrop_name: str
    # Floor from the /collections/models/backgrounds/floors API, scoped
    # ONLY by model name (the endpoint ignores collection_id/short_name).
    # Kept for diagnostics/comparison ONLY -- confirmed live to be
    # frequently wrong across model-name collisions between unrelated
    # collections (e.g. Berry Box got 200.0 against a collection floor of
    # 9.35). NEVER used for discount or profit calculations -- see
    # own_combo_floor_nano.
    api_combo_floor_nano: int | None
    model_min_floor_nano: int | None
    floor_fetched_at: datetime
    floor_age_sec: int
    raw_model_block: dict[str, Any] = field(default_factory=dict)
    # ALWAYS False when written by the poller. Set only by report.py's
    # backfill pass -- never at write time (see db.py / report.py).
    name_collision: bool = False
    # None = normal (floor looked up and found, or backdrop genuinely
    # absent from the model's block -- both are valid, unremarkable
    # outcomes). Otherwise one of: "below_price_threshold" (listing price
    # under FLOOR_MIN_PRICE, floor lookup skipped entirely to save
    # rate-limit budget) or "model_not_returned" (server silently dropped
    # this model name from the batch response, twice).
    floor_skip_reason: str | None = None
    # Floor computed from OUR OWN collected listings, grouped by
    # (collection_name, model_name, backdrop_name) -- see own_floors.py.
    # This is the source of truth for discount/profit calculations.
    own_combo_floor_nano: int | None = None
    own_sample_size: int = 0
    own_confidence: str = "none"  # none|low|medium|high, see own_floors.py
    # "ok" | "suspect" | "no_data" -- see FLOOR_SANITY_MAX_RATIO in
    # config.py. Diagnostic only (about api_combo_floor_nano); does not
    # gate own_combo_floor_nano, but report.py still excludes non-"ok"
    # rows from the main distribution as a safety net.
    floor_sanity: str = "no_data"
    # Floor from a direct, filtered /nfts/search query for this exact
    # (collection_id, model_name, backdrop_name) triple, sorted by price
    # ascending -- see pair_floor.py. INCLUDES the listing's own price if
    # it's active in that pair -- kept for diagnostics/comparison ONLY
    # (see the _excl_self fields below, which are the real source of
    # truth). api_combo_floor_nano and own_combo_floor_nano are also
    # retained only for diagnostics/comparison and as a fallback should
    # this query ever stop working.
    pair_floor_nano: int | None = None
    pair_listed_count: int = 0
    # "ok" | "alone_in_pair" | "no_data" | "error" -- computed strictly
    # from the POST-exclusion result (see pair_floor.py). "ok" is a hard
    # guarantee that pair_floor_excl_self_nano is not None.
    pair_floor_status: str = "no_data"
    pair_floor_age_sec: int = 0
    # Confirmed live (manual review of 20 top price drops, 17/20 were
    # artifacts): a listing alone in its pair -- or currently the
    # cheapest -- IS pair_floor_nano above, so it gets compared against
    # its own price. THESE fields exclude the listing itself and are the
    # actual source of truth for discount/profit -- see pair_floor.py.
    pair_floor_excl_self_nano: int | None = None
    pair_listed_count_excl_self: int = 0
    pair_self_was_floor: bool = False
    # Model-level fallback (no backdrop filter) -- confirmed live that
    # most (model, backdrop) pairs have exactly one active listing (336
    # measured with one, zero with ten), so "alone_in_pair" is the common
    # case, not rare. Only requested when pair_floor_status ==
    # "alone_in_pair" (see poller.py) -- never when the pair level is
    # already usable, to avoid spending rate-limit budget on a comparison
    # that won't be used. Coarser than pair level (different backdrops
    # price differently within one model) -- report.py never mixes pair-
    # and model-level signals into one distribution.
    model_floor_excl_self_nano: int | None = None
    model_listed_count_excl_self: int = 0
    model_floor_status: str = "no_data"  # ok | alone_in_pair | no_data | error


@dataclass
class MarketConfigSnapshot:
    fetched_at: datetime
    raw: dict[str, Any]
    commission: Decimal | None
    offer_fee: Decimal | None
    withdrawal_fee: Decimal | None
    user_cashback: Decimal | None
    usd_course: Decimal | None
