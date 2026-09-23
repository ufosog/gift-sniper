"""Standalone probe script. Run once with a real token, by the human who
holds it, to fill in the "Step 0" facts table in README.md. This script
makes no attempt to guess at any answer -- it only reports what the live
API actually returned.

Usage:
    PORTALS_AUTH=<token> python -m gift_sniper.step0

The token is read ONLY from the PORTALS_AUTH env var. There is no
command-line flag for it, so it can never end up in shell history via an
argument, and it is never printed here, not even partially.

Nothing in this script writes to the database.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime

MAX_PAGES_FOR_MODEL_COLLECTION = 10
TARGET_UNIQUE_MODELS = 60


def _print_header(title: str) -> None:
    print()
    print(f"=== {title} ===")


def _safe_call(label: str, fn):
    """Runs fn(), prints a one-line status, returns the result or None.
    Never raises -- any exception is reported and swallowed so the rest of
    the probe still runs.
    """
    try:
        result = fn()
        print(f"[{label}] OK (200)")
        return result
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, this is a probe
        print(f"[{label}] FAILED: {type(exc).__name__}: {exc}")
        return None


def _model_name_of(item: dict) -> str | None:
    for attr in item.get("attributes", []) or []:
        if isinstance(attr, dict) and attr.get("type") == "model" and attr.get("value"):
            return attr["value"]
    return None


def _parse_dt(raw: str | None):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _sorted_desc_verdict(results: list[dict]) -> str:
    """Yes/no based on actually-parseable listed_at values. Reports
    "insufficient data" instead of a fabricated answer when fewer than 2
    timestamps parse -- never print "unknown" without saying why.
    """
    dts = [d for d in (_parse_dt(r.get("listed_at")) for r in results) if d is not None]
    if len(dts) < 2:
        return "insufficient data (fewer than 2 parseable listed_at values)"
    is_desc = all(dts[i] >= dts[i + 1] for i in range(len(dts) - 1))
    return "yes" if is_desc else "no"


def probe_sort(client) -> dict:
    _print_header("(a) SORT")
    variants = {
        "no sort": None,
        "sort=latest": "latest",
        "sort=listed_at desc": "listed_at desc",
    }
    outcomes = {}
    for label, sort_value in variants.items():
        def call(sort_value=sort_value):
            return client.search(limit=20, offset=0, sort=sort_value)

        resp = _safe_call(label, call)
        outcomes[label] = resp
        if resp is None:
            continue
        results = resp.get("results", [])
        first_dt = results[0].get("listed_at") if results else None
        last_dt = results[-1].get("listed_at") if results else None
        verdict = _sorted_desc_verdict(results)
        print(f"    {label}: first.listed_at={first_dt} last.listed_at={last_dt} sorted_desc={verdict}")
    return outcomes


def probe_max_limit(client) -> dict:
    _print_header("(b) MAX LIMIT")
    outcomes = {}
    for limit in (20, 50, 100, 200):
        def call(limit=limit):
            return client.search(limit=limit, offset=0)

        resp = _safe_call(f"limit={limit}", call)
        outcomes[limit] = resp
        if resp is not None:
            print(f"    limit={limit}: len(results)={len(resp.get('results', []))}")
    return outcomes


def collect_unique_models(client, target: int = TARGET_UNIQUE_MODELS) -> list[str]:
    """Pages through /nfts/search (limit=50) accumulating DISTINCT model
    names until `target` unique names are collected or the page cap is
    hit. Needed because a single page of 50 listings can contain fewer
    than `target` distinct models (observed: only 19 unique in a 50-item
    sample), which is not enough to measure the real batch-size ceiling.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    offset = 0
    for _ in range(MAX_PAGES_FOR_MODEL_COLLECTION):
        resp = _safe_call(f"collect models offset={offset}", lambda offset=offset: client.search(limit=50, offset=offset))
        if resp is None:
            break
        results = resp.get("results", [])
        if not results:
            break
        for item in results:
            name = _model_name_of(item)
            if name and name not in seen_set:
                seen_set.add(name)
                seen.append(name)
        if len(seen) >= target:
            break
        offset += 50
    return seen


def probe_max_models(client) -> list[str]:
    _print_header("(c) MAX MODELS")
    unique_models = collect_unique_models(client)
    print(f"    collected {len(unique_models)} unique model names across pages")
    if not unique_models:
        print("    no model names available, skipping batch-size probe")
        return unique_models

    for n in (15, 30, 60):
        subset = unique_models[:n]
        if len(subset) < n:
            print(f"    requested={n}: only {len(subset)} unique names available, testing with that many")

        def call(subset=subset):
            return client.model_backgrounds_floors(subset)

        resp = _safe_call(f"models={len(subset)}", call)
        if resp is not None:
            returned_keys = set(resp.get("model_backgrounds", {}).keys())
            missing = [m for m in subset if m not in returned_keys]
            print(
                f"    requested_unique={len(subset)} returned_keys={len(returned_keys)} "
                f"missing={missing}"
            )
    return unique_models


def probe_market_config(client) -> None:
    _print_header("(d) MARKET CONFIG")
    resp = _safe_call("market_config", client.market_config)
    if resp is not None:
        print(f"    full response: {resp}")


def probe_unit_reconciliation(client, first_listing: dict | None) -> None:
    _print_header("(e) UNIT RECONCILIATION")
    if not first_listing:
        print("    no listing available from probe (a), skipping")
        return
    model_name = _model_name_of(first_listing)
    backdrop_name = None
    for attr in first_listing.get("attributes", []) or []:
        if attr.get("type") == "backdrop":
            backdrop_name = attr.get("value")
    if not model_name:
        print("    first listing has no model attribute, skipping")
        return

    def call():
        return client.model_backgrounds_floors([model_name])

    resp = _safe_call(f"floors for model={model_name}", call)
    combo_floor = None
    if resp is not None:
        block = resp.get("model_backgrounds", {}).get(model_name, {})
        combo_floor = block.get(backdrop_name) if backdrop_name else None
    print(f"    listing.price={first_listing.get('price')}")
    print(f"    listing.floor_price={first_listing.get('floor_price')}")
    print(f"    combo_floor (model={model_name}, backdrop={backdrop_name})={combo_floor}")


def probe_name_collisions(max_limit_resp: dict | None) -> None:
    _print_header("(f) NAME COLLISIONS")
    if not max_limit_resp:
        print("    no data from probe (b) max-limit call, skipping")
        return
    results = max_limit_resp.get("results", [])
    collections_by_model: dict[str, set[str]] = defaultdict(set)
    for item in results:
        collection_name = item.get("name")
        name = _model_name_of(item)
        if name:
            collections_by_model[name].add(collection_name)
    colliding = {m: sorted(c) for m, c in collections_by_model.items() if len(c) > 1}
    if colliding:
        for model, cols in colliding.items():
            print(f"    COLLISION: {model} -> {cols}")
    else:
        print("    no collisions observed in this sample (empty list is itself a result)")


def run(client) -> None:
    sort_outcomes = probe_sort(client)
    limit_outcomes = probe_max_limit(client)

    no_sort_resp = sort_outcomes.get("no sort")
    results = (no_sort_resp or {}).get("results", [])

    probe_max_models(client)
    probe_market_config(client)
    probe_unit_reconciliation(client, results[0] if results else None)

    max_limit_resp = limit_outcomes.get(200) or limit_outcomes.get(100) or limit_outcomes.get(50) or limit_outcomes.get(20)
    probe_name_collisions(max_limit_resp)


def main(argv: list[str] | None = None, client_factory=None) -> int:
    token = os.environ.get("PORTALS_AUTH")
    if not token:
        print(
            "PORTALS_AUTH is empty or unset. Refusing to make any network "
            "calls. Set it and re-run: PORTALS_AUTH=<token> python -m gift_sniper.step0",
            file=sys.stderr,
        )
        return 1

    if client_factory is None:
        from .portals_client import PortalsClient

        def client_factory():
            return PortalsClient(auth_provider=lambda: token)

    client = client_factory()
    run(client)
    print()
    print("=== Done. Paste this entire output (no token appears above) into the chat. ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
