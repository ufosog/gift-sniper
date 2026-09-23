# Telegram Gifts Marketplace Dataset (Portals, Tonnel, MRKT)

Two and a half weeks of continuous price collection from the three
marketplaces where Telegram gifts are traded: **Portals**, **Tonnel** and
**MRKT**. Collected 5–23 September 2026.

The rare part is `confirmed_sales.csv`: **4,420 sales with the price actually
paid**. The marketplaces do not publish trade history — a lot simply disappears
from the listing and the reason is unknown (sold? withdrawn? returned to its
owner?). MRKT's event feed is the one place where a sale arrives explicitly,
with a price. Everything else in this dataset is asking prices; these are
transaction prices.

## Files

| File | Rows | What it is |
|---|---|---|
| `listings.csv` | 54,447 | Every lot seen, with its attributes and asking price |
| `price_changes.csv` | 1,811,271 | Every price change, with the pair floor at that moment |
| `confirmed_sales.csv` | 4,420 | Sales with a confirmed price (MRKT) |
| `paper_trades.csv` | 3,352 | A paper-trading journal: signals and their simulated outcome |

Prices are in TON. Timestamps are ISO 8601, UTC.

### listings.csv
`marketplace`, `lot_id`, `collection`, `model`, `backdrop`, `symbol`,
`gift_number`, `price_ton`, `currency`, `status`, `first_seen_at`, `listed_at`

A gift is identified by `collection` + `model` + `backdrop` — that triple is
what makes two lots comparable. There are 131 collections and 39,707 distinct
triples here, which is the first hint about liquidity.

### price_changes.csv
`marketplace`, `lot_id`, `observed_at`, `old_price_ton`, `new_price_ton`,
`change_pct`, `is_noise`, `floor_at_change_ton`, `floor_depth`, `floor_level`

`is_noise = 1` marks changes too small to mean anything (the threshold was
measured, not guessed). `floor_at_change_ton` is the cheapest other lot of the
same pair at that exact moment, queried live when the change was recorded;
`floor_depth` is how many other lots backed that floor, and `floor_level` says
whether the floor is for the exact pair or for the model alone.

### confirmed_sales.csv
`marketplace`, `lot_id`, `collection`, `model`, `backdrop`, `gift_number`,
`sold_price_ton`, `sold_at`, `listed_at`, `floor_at_sale_ton`,
`floor_depth_at_sale`

Median sale price 27.5 TON (quartiles 18.7 and 48.0, max 1,285).

### paper_trades.csv
`scenario`, `signal_at`, `marketplace`, `collection`, `model`, `backdrop`,
`gift_number`, `buy_price_ton`, `floor_ton`, `floor_level`, `floor_depth`,
`cross_check`, `floor_to_price_ratio`, `status`, `reject_reason`,
`execution_check`, `opened_at`, `closed_at`, `sell_price_ton`, `pnl_ton`,
`pnl_pct`, `sold`

No real money was traded. Each signal is recorded under several scenarios
(different holding times and sale probabilities) with the real fee structure of
each marketplace.

## What the data says

Flipping gifts on these marketplaces does not work, and the dataset shows why:

- 3,134 confirmed sales are spread over 2,495 distinct pairs — **a median of one
  sale per pair in nine days**;
- buy at the price of a real trade and sell into the next real trade of the same
  pair, with no competition and no time limit: 639 such round trips, 10%
  profitable, **median −2.0%** — exactly the seller fee. Prices do not move
  between consecutive trades;
- the expensive segment behaves the same as the cheap one.

The full write-up and the scripts that reproduce every number are in the
repository linked below.

## Notes and limits

- Collection starts at 15 TON: cheaper lots were deliberately not tracked.
- Portals and Tonnel never confirm a sale, so `confirmed_sales.csv` is MRKT only.
- `listings.status` and `price_ton` are the values as of the end of collection,
  not as of `first_seen_at`; use `price_changes.csv` to reconstruct a price at a
  past moment.
- No personal data: only lot identifiers and public marketplace attributes.

## Licence

CC BY 4.0 — use freely, including commercially, with attribution.
