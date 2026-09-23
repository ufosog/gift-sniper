# Gift Sniper — combo-floor data collector

## Backtest on collected history: the exit, not the entry, is the problem (2026-09-21)

The paper journal needs weeks to answer "do the signals make money" (10
closed trades a day). The history already in the database answers part of
it today. `diag/backtest_mrkt.py` replays the SAME cascade over all MRKT
history and prices the exit from CONFIRMED sales only — MRKT is the one
marketplace with a sale event carrying a price.

**Measured on 154 historical MRKT clean signals (before cross-check):**
- **Only 11 (7%) had any confirmed sale of the SAME pair, by another lot,
  within 48 h of the signal.** 48% of the signalled lots were still listed
  at the end of history, 19-26% were returned to their owner. The binding
  problem is that nobody buys that exact pair, not that the price is wrong.
- Of those 11 measurable exits: 3 profitable, median **-7.27%**.
- A first version of the script priced the exit from the lot's OWN later
  sale and got a median of exactly **-2.00%** on 19 sales — the sell fee.
  That is the honest reading: those lots sold at their own listed price,
  i.e. at the price we would have paid. The "discount against the floor"
  was the market price, and the floor was not a price anyone paid.
- A liquidity pre-filter (does this pair have a confirmed sale in the past
  7 days?) matched only 3 signals: nothing can be concluded yet.

**`journal_config.SCENARIOS` gained "all"**: the same rules as `base` but
with no portfolio limits (its own start balance, slots and position cap,
via the new `journal_config.limits(scenario)`), so every clean signal
becomes a trade. With 10 slots and a 300 TON bank the portfolio scenarios
rejected 124 of 242 signals for `no_slot` alone; the per-signal question
needed a scenario that never runs out of room. The portfolio scenarios are
unchanged and still answer "what would my 300 TON have made".

## Poll cycle loaded the whole history; daily review on GitHub (2026-09-19)

**Server crash, measured.** On the new 2 GB VPS the box froze twice (RAM
1908/1968 MB, CPU 98%). The Portals poller held 1.1 GB RSS plus 1 GB swap.
Cause: `run_cascade` loaded ALL of `price_history` (1,043,670 Portals rows,
joined with `listings` and `floor_snapshots`) on EVERY notify pass, then
filtered by `since` afterwards. Fix: `clean_signals(since=...)` passes
`min_observed_at = since - CASCADE_CONTEXT_MARGIN` (1 h, above the
bulk-update stage's `SAME_SECOND_WINDOW`; the ladder and unstable-floor
stages run their own SQL). Verified on a copy of the live DB: identical
clean-signal sets at since = 1/6/24/72 h, peak memory 1747 MB -> 1-4 MB,
pass 34 s -> 4.5 s. `backfill_ladder` (4.4 s, rewrites the whole table,
never read by selection) now runs at most once per 10 min per database
file. After deploy: Portals 47 MB, whole server 362 MB, cycle ~28 s.
`report.py` still loads everything (`since=None`) — do not run it on the
server.

**Daily review.** The server pushes `reports/` (digest, health snapshot,
service status, log tails) to a private GitHub repo every day; a scheduled
cloud agent reads them and opens one issue with its findings. Its first
run found two real defects in that very pipeline: `health.py` run from
another directory died on relative DB paths (`config.DB_DSN` and
`journal_config.JOURNAL_DB_DSN` are now absolute by default, env still
wins — the "depends on how it was called" class again), and `.gitignore`'s
`logs/` rule silently hid `reports/logs/`.

## Cross-check ignored Tonnel's buyer fee; journal exit capped by other marketplaces (2026-09-19)

**Defect 1, measured.** `cross_check` compared the RAW Tonnel signal
price with neighbour floors taken at buyer cost (Tonnel neighbours with
the 10% fee, MRKT salePrice with its fee). Live case: Bonded Ring
Leopard #7588 on Tonnel at 40.25 (buyer cost 44.28); MRKT model floor
42.13 (12 lots), Portals 42.55 (25 lots); 12 of 13 MRKT sales of the
collection went at 40-42. It passed as `model_bound_inconclusive` and
went to the owner. Recount on history: 10 of 28 cross-checked Tonnel
signals (36%) should have been blocked. Fix: `_buy_cost_nano(signal)`,
used by all three decisions.

**Defect 2, journal model.** The close used only the entry marketplace's
own floor. The ring's Tonnel model floor was 150 (one lot, the rest from
227), so the journal would have booked a sale near 142. Fix:
`_cross_market_cap` caps the exit at the cheapest same-level offer on the
other two marketplaces, at buyer cost (÷1.1 for a Tonnel exit). Live: the
two rings are capped at 38.30 and 38.68, a loss after the fee. A failed
competitor query postpones the close. The cap is stored in
`exit_cap_nano` (journal schema v4).

**Open:** a pair with no listing on the other marketplaces gets no cap.
Live: MRKT Liberty Figure Rebel Royal / Roman Silver, bought at 13.99,
own pair floor 48.96 (depth 3), while the same model's recent listings
were 12-25 and the same pair was listed on Portals at 18.70 on 09-13.
Candidate: a cap from recent sales/listings of the pair. Not built yet,
because there are too few sales per pair to set a threshold.

## Portals poll cycle took 306 s: missing index (schema v20, 2026-09-19)

**Measured** on the live run: one Portals poll cycle took 306 s (journal
heartbeat 10:37:19 -> 10:42:25) with `POLL_INTERVAL_SEC=10`. py-spy on the
live process: 6 of 6 samples in `signals._floor_instability`. The query
scanned `floor_snapshots` (0.105 s) and the cascade ran it about 2400
times per notify pass, because the cascade re-evaluates the whole history
every cycle. New index `idx_floor_snapshots_pair_time` (migration v20 and
the fresh-DB DDL): 0.0001 s, identical results on 30 random pairs.
**Still open:** the cascade cost grows with history; measure the cycle
time after the restart.

Also fixed: false alerts after a start or a journal reset. The closer
now writes its own heartbeat every loop. "Zero signals in 24 h" alerts
only after 24 h of observation. Owner-only `/mrkt_token <token>` stores a
new MRKT token from the phone. Running processes use it without a restart.

## Journal execution check, Portals without a token, supervisor, health (2026-09-19)

### "sniped" was a late check, not the market
**Measured** on the live journal (diag/sniped_audit.py): the closer did
not run from 2026-09-15 10:15 to 2026-09-17 08:54. The execution check ran
inside the 30-minute closer pass, so the 09-15 signals were checked about
46 hours late. Of the 7 lots marked `sniped` and then traced in
`gift_sniper.db`: 1 was returned by its owner at 0.1 min (really gone),
4 were repriced later (one of them DOWN, from 22.35 to 22.15 at 12.7 min,
so still buyable), and 1 was repriced up at 19.5 min and then sold. A
check hours later measures whether a lot survived hours, not 45 s.

The same missing closer caused the clogged journal: the 5 pending rows
held all 5 slots for 46 hours (`no_slot` 311 rows).

**Fix (`paper_journal.py`, journal schema v3):**
- A pending row does not hold a slot. The slot is taken only on OPEN.
- The execution check runs on its own loop every `EXEC_POLL_SEC` (15 s),
  so a row is checked at 45–60 s of age. A row older than
  `EXEC_MAX_AGE_SEC` (120 s) is never checked: it becomes
  `REJECTED exec_check_missed`. Basis: lots taken within 45 s 5%, within
  120 s 6% (MRKT sales), so a check up to 120 s biases the snipe rate by
  about 1 point; at 300 s the bias is 7 points.
- A lot repriced DOWN is bought, entered at the signal price (the result
  is understated, as the journal requires). Gone, not for sale or
  repriced UP -> `sniped`.
- New columns `exec_checked_at`, `exec_state`
  (`listed | repriced_down | repriced_up | gone | not_for_sale`),
  `exec_price_nano`: every snipe can now be checked afterwards.
- One physical lot is held once: a second signal on a lot already OPEN
  is `lot_already_held`. Live example: MRKT Chill Flame 9d63b46a was OPEN
  twice from two signals.
- Reason order: `thin_book`, `spread_too_low`, then `no_slot`,
  `insufficient_balance`. The statistics now name the signal's own
  defect before the portfolio state.

**Live check** (supervisor run 2026-09-19 12:06, 4 min): the 2 stuck
pending rows became `exec_check_missed`; 2 new signals were checked at
46 s and 49 s of age, both `listed`, 0 sniped. **n=2: no conclusion on the
real snipe rate yet.** The rows before this fix (`no_slot`, old `sniped`)
stay as they are; start a clean period with `/magazine_reset`.

### Portals needs no token
**Measured** 2026-09-19 (diag/portals_noauth_probe.py,
diag/portals_noauth_feed.py): every Portals endpoint this project calls
answers with no `Authorization` header at all, with the same content:
`search_by_ids`, pair floor, model floor,
`/collections/models/backgrounds/floors`, `/market/config` identical; the
`/nfts/search` feed newest-first with 37–42 of 50 ids shared between two
calls about a second apart (the stream is ~8 listings/s). Same rate limit
(`x-ratelimit-limit: 2`). A 97-hour-old token and a garbage token were
accepted too: the token was never checked. A 4-minute live run without
`PORTALS_AUTH` collected 399 items with 0 errors.

`PORTALS_AUTH` is now optional. Without it no `Authorization` header is
sent. A 401/403 on an anonymous request logs that Portals started to
require auth.

### MRKT token: automatic refresh (`mrkt_auth.py`), waits for a Telegram session
MRKT's frontend (cdn.tgmrkt.io, js/auth-preflight) gets its token with
`POST https://api.tgmrkt.io/api/v1/auth {"data": initData, "appId": null}`.
The server checks the Telegram signature: invalid initData gives HTTP 403
(measured). `mrkt_auth.py` gets initData with a Telethon user session
(`messages.requestAppWebView`, bot @mrkt, app `app`) and writes the token
to `secrets/mrkt_token.txt`. `config.get_mrkt_access_token()` reads that
file first and re-reads it when it changes, so running processes use a
new token without a restart. **Not verified live**: needs `TG_API_ID`,
`TG_API_HASH` and a one-time `--login` by the owner. Unconfirmed: where
the token is in the auth response (cookie first, then JSON fields).
Token lifetime: unknown, refresh hourly is an assumption.

### Supervisor, health, alerts
- `python health.py [--online]`: one report, OK/WARN/FAIL per check:
  pollers (heartbeat), closer, collection per marketplace, signals in
  24 h, stuck pending rows, `no_slot` share, `sniped` share, tokens.
  Collection thresholds from measured max gaps between new listings
  inside continuous runs (portals 1289 s, tonnel 2851 s, mrkt 344 s) ×
  1.5. Heartbeat 5 min is an assumption (only the last beat is stored).
- `python -m gift_sniper.supervisor` (or `.\start.ps1`): runs all four
  processes, restarts a crashed one (10 s doubling to 5 min), writes
  `logs/<name>.log` (10 MB × 5), checks health every 5 min and tells the
  owner when a check goes bad and when it recovers, refreshes the MRKT
  token when a session is set up.
- Bot commands now live in the supervisor (the pollers get
  `BOT_COMMANDS_IN_POLLER=0`): new `/health`, `/procs`, and owner-only
  `/run`, `/stop`, `/restart <portals|tonnel|mrkt|closer>`.

## Notifications back, whole-lot positions, /magazine, closer fix (2026-09-15)

### Model-level notifications are on by default (open question)
`NOTIFY_LEVELS` now defaults to `pair,model` for Portals and MRKT.
**Measured:** in 12 hours Portals produced 10 clean signals, all at the
model level. With `NOTIFY_LEVELS={'pair'}` none of them was sent, among
them Loot Bag (+46.05 TON) and Scared Cat (+11.13 TON). Over 24 hours:
model gave 25 signals, 21 above $5; pair gave 5 signals, 3 above $5.

The model level was switched off earlier because a model floor belongs to
a different backdrop and inflated the profit. Since then the cascade
applies the depth-based realization rate and `MIN_SIGNAL_PROFIT_TON`,
which cut inflated estimates before a signal is sent. The profit check
(`NOTIFY_MIN_PROFIT_USD`) is unchanged and still applies.

**OPEN QUESTION — the model level is not verified by facts.** No sale has
a computed model floor (n=0). The pair level has 58 observations with a
median realization of 0.67. So the realization rate for the model level
is unknown, and model-level signals use the pair-level rate. **This is an
assumption.** The paper journal records `floor_level`, and both the
journal report and `/magazine_full` split results by it. After 2–4 weeks,
compare real model vs pair profitability and decide from that data, not
from this assumption.

### Paper journal: no percentage position limit
`POSITION_PCT` is removed. It contradicted `MAX_POSITIONS=5` (5 × 30% =
150% of the balance). At a 100 TON bank it also cut 68% of signals: the
signal price median is 38.20 TON, the maximum 259.00. Measured share of
signals that fit a fixed limit: 30 TON 32%, 40 TON 51%, 50 TON 67%,
70 TON 77%, 100 TON 87%.

New rule: the lot is bought whole when `balance >= price`, and
`position_size = price`. Otherwise the signal is rejected with reason
`insufficient_balance`, which replaces `too_big`. The same check runs
again when a pending position opens. `MAX_POSITIONS` (5) stays.

**Rows already in `journal.db` still carry `too_big`.** Start a clean
period with `/magazine_reset`.

### Bot: /magazine, /magazine_full, /magazine_reset
- `/magazine` (owner and viewers, read-only): the base scenario. Shows
  bank, current value, free balance, money in positions, closed trades,
  open positions (up to 5) and the last trades (up to 5).
  - **Realized profit** ("Прибыль по закрытым") and the **estimate of
    open positions** ("Оценка открытых") are separate lines. They are
    never added into one profit number.
  - "Сейчас" is the whole bank now = equity + withdrawn profit.
  - With no closed trades the message says "сделок пока нет".
  - When the message exceeds Telegram's 4096 characters, the trade list is
    cut first, never the summary.
- `/magazine_full`: the same, plus a split by marketplace and by floor
  level (pair vs model).
- `/magazine_reset`: **owner only**, two steps. The first command asks.
  Repeating it within 60 seconds deletes every journal row, uptime row
  and equity point, and resets the balances. A viewer gets a refusal.
- The other scenarios stay in the console report
  (`python -m gift_sniper.journal_report`). The journal sends no trade
  notifications.

The Portals poller's `CommandHandler` reads `journal.db` when
`PAPER_JOURNAL_ENABLED=1`. Otherwise the commands answer
"журнал выключен".

`journal.db` schema v2 adds `mark_nano` and `mark_at`: the current value
of each open position, written by the closer. A v1 database is migrated
in place, and existing rows are kept.

### Closer: positions stuck in PENDING_EXEC
**Measured:** positions waited in `PENDING_EXEC` up to 541 minutes
(example `c3de48215f6aafaa`, MRKT) while `EXEC_MIN_AGE_SEC=45`.

**What the live `journal.db` shows:** the closer writes
`journal_equity_log` on every pass. The last entries are from
2026-09-14 09:13–10:14 (4 passes), and nothing after that. The pollers
kept writing uptime until 2026-09-15 08:29. So the closer process stopped
around 10:15 and never ran again. The stuck signal arrived at 10:37,
after it stopped. No closer log was kept, so the exact exception that
stopped it cannot be recovered.

**What in the code allowed it:** the loop caught only
`sqlite3.OperationalError`. Any other exception from any marketplace
client ended the whole process.

**Fix:**
- A failed pass is logged and the loop continues.
- Each signal is checked on its own. Any error from one marketplace (no
  token, network, API, anything unexpected) leaves only that
  marketplace's rows in their current status. All other rows are still
  processed.
- Each marketplace client is built on its own. A missing
  `PORTALS_AUTH` or `MRKT_ACCESS_TOKEN` disables only that marketplace.
- On start, the closer logs the absolute path of the journal file. The
  pollers and the closer must open the same file, and a relative
  `JOURNAL_DB_DSN` resolves against each process's working directory.
- **One log line per pass:**
  `paper journal closer pass: pending processed=… opened=… sniped=… rejected_on_open=… skipped_unavailable=N {marketplace: n} | closed=… unsold=… close_postponed=N {…}`.

**Not verified live.** Restart the closer and watch that line.

## Paper journal: do the signals make money? (observation period, 2026-09-14)

**Goal**: over 2–4 weeks, answer honestly whether the signals are
profitable after all fees under realistic assumptions. No real trading:
the journal only records what would have happened if every clean signal
had been bought and sold. **The model is built to UNDERSTATE the result.**

### How to run
1. Set `PAPER_JOURNAL_ENABLED=1` (and optionally `JOURNAL_DB_DSN`,
   default `journal.db`) for all three pollers. Each poller then writes
   every clean signal to `journal.db` after its notify check, whether or
   not a notification was sent, and records its uptime.
2. Start the closer as its own process:
   `python -m gift_sniper.paper_journal --closer` (a pass every
   `JOURNAL_CLOSER_INTERVAL_MIN`, default 30). `--once` runs one pass.
3. Report: `python -m gift_sniper.journal_report --db journal.db`.

`journal.db` is separate from `gift_sniper.db`. To reset the journal,
delete that one file. The journal never affects notifications or the
working database. Any journal error is logged and swallowed.

### Parameters (fixed for the whole observation period)
**Do not change these during the period.** Tuning them towards a nicer
result defeats the purpose. The env overrides (`JOURNAL_*`) exist only to
start a NEW period.

All parameters below were measured on confirmed MRKT sales, the only
marketplace with a `sale` event that carries a price: 775 sales, 554 with
a known lifetime, 61 with a pair floor at the moment of sale.

| parameter | value | basis |
|---|---|---|
| HOLD_HOURS | pess 48 / base 24 / opt 12 | sold within 1h 39%, 6h 79%, 12h 93%, 24h 99%, 48h 100%, median 1.7h. **Revisit in a week**: the sample maximum is 24.5h, suspiciously close to how long MRKT has been collected, so long-lived lots are not in the data yet. |
| EXEC_SLIPPAGE | 1.00 | sold/listed median is 1.000 in every lifetime bucket (<1h n=219, 1-6h n=221, 6-24h n=109, >24h n=3: 0.999). 265/420 were exact matches. |
| EXEC_MIN_AGE_SEC | 45 | lots taken within 45s: 5% (120s 6%, 300s 12%, 600s 18%). |
| SELL_PROBABILITY | pess 0.40 / base 0.50 / opt 0.60 | lots below the pair floor: 39/75 sold (52%). Above the floor: 3/9. All lots: 775/1746 (44%). **n=75 is small.** |
| UNSOLD_LIQUIDATION_RATE | 0.70 | **ASSUMPTION, not a measurement.** No trade of an unsold lot has ever been observed. Withdrawn lots (n=10) stood at a median 0.90 of the floor, returned lots (n=32) at 0.77. |
| MIN_NET_SPREAD_PCT | pess 10 / base 7 / opt 5 | expected pnl as % of the entry. |
| START_BALANCE / POSITION_PCT / MAX_POSITIONS / REINVEST_PCT | 100 TON / 30 / 5 / 50 | |

Realization rates (`REALIZATION_RATE_DEPTH_*`) and fees
(`MARKETPLACE_FEE_RATE`, `WITHDRAWAL_FEE_FLAT`) come from `config.py` and
are not duplicated. The journal's pnl formula is pinned to
`signals.compute_profit_nano` by a test. Fees: Portals 2% seller fee plus
0.35 TON withdrawal; Tonnel 10% buyer fee; MRKT 2% already inside the
buyer price.

No collection whitelist is used. The book-depth threshold already does
that job and measurably predicts realization. Inside one collection,
prices vary up to 7x (Ion Gem: 69..520), so a collection average means
nothing.

### Flow
- **Entry** (`record_signal`): uses only data carried by the signal (price,
  floor at drop, level, depth, cross verdict). No fresh floor is fetched,
  because that would be lookahead. One row per scenario. Rejected signals
  are written like accepted ones, with `no_slot`, `too_big`,
  `spread_too_low` or `thin_book` (depth < 2). Otherwise the row is
  `PENDING_EXEC`.
- **Execution check** (closer, `PENDING_EXEC` older than 45s): a real lookup
  of the lot on its marketplace. If the lot is gone, not for sale or
  repriced, the row becomes `REJECTED sniped` and the balance is not
  touched. Otherwise the row becomes `OPEN` and the position is reserved
  from the balance. On a network error the row stays pending and is
  retried.
- **Close** (closer, `OPEN` past HOLD_HOURS): a fresh floor **of the same
  level the signal was computed on** (pair signal: pair floor; model
  signal: model floor) on the entry marketplace. This was a deliberate
  decision: 9 of 10 signals are model-level. Closing them on a pair floor
  they usually lack would measure floor availability, not profitability.
  - no floor: `UNSOLD`, exit = price × 0.70
  - deterministic draw `sha256(signal_id + scenario)` < SELL_PROBABILITY:
    `CLOSED`, exit = floor_now × realization_rate(depth_now)
  - otherwise: `UNSOLD`, exit = floor_now × 0.70
  - `pnl = exit×(1−fee_sell) − entry×(1+fee_buy) − network_fee`. Half of
    a positive pnl goes to `withdrawn`.
  - equity = balance + open positions at floor_now × realization_rate. It
    is logged after each close for the max-drawdown figure.
- **Coverage**: each poller run writes one `journal_uptime` row, and its
  `ended_at` moves on every cycle. A crash therefore never counts as
  coverage.

### Report
For each scenario: totals, then the same metrics split by marketplace,
floor level and cross-check verdict. Metrics: accepted and rejected
counts with top reasons; OPEN, CLOSED and UNSOLD counts; win rate with
and without UNSOLD; mean and median pnl in TON and %; balance, equity,
withdrawn; ROI; max drawdown; hold time; top-10 pairs by profit and by
loss; poller uptime.

The reconciliation line checks
`balance + withdrawn + OPEN positions at entry = START_BALANCE + realized pnl`.
The spec's version of this check left out `withdrawn`.

Schema additions beyond the spec: `journal_signals.collection_id` (needed
for Portals floor queries) and `journal_equity_log` (needed for drawdown).

**Not verified live.** To check, run the three pollers and the closer for
2 hours. Expect every status in `journal_signals`, at least one
`PENDING_EXEC→OPEN` or `sniped`, three scenarios in the report with every
split, and `reconciliation: ok`.

## Realization rate, unstable pairs, neighbour model floor as a lower bound (2026-09-14)

Three changes, each threshold taken from a measurement on real data. None
was picked by hand.

### 1. Realization rate in the profit formula

**Measured** (2026-09-14) on confirmed MRKT sales, the only real trades in
the project: 751 sales, 67 with a pair floor recorded at the moment of
sale, 61 left after dropping ratio outliers outside [0.2, 1.5].
`sold_price / floor_at_sale`: median 0.783, p25 0.482, p75 0.937, max
1.000. **No sale ever went through above the floor.** The floor is a
ceiling, not the expected price.

| depth at sale | n  | median |
|---------------|----|--------|
| 1             | 41 | 0.606  |
| 2-3           | 14 | 0.898  |
| 4-9           | 6  | 0.950  |
| 10+           | 0  | —      |

At depth 1, one overpriced lot sets the floor and the real trade is far
below it (Wrestler / Black: sold 21.42, floor 102.00; Stargazer /
Lemongrass: sold 16.25, floor 132.60).

**Change**: `signals.compute_profit_nano(marketplace, floor, price, depth)`
now uses `floor * realization_rate(depth)` in place of the raw floor.
`depth` is a required argument, so no caller can skip it:
- Portals: `floor*RATE*(1-0.02) - price - 0.35`
- Tonnel: `floor*RATE - price*1.1`
- MRKT: `floor*RATE*(1-0.02) - price`

Fees did not change. Defaults are set by env, with no code edit needed:
`REALIZATION_RATE_DEPTH_1=0.61`, `REALIZATION_RATE_DEPTH_2_3=0.90`,
`REALIZATION_RATE_DEPTH_4_9=0.95`, `REALIZATION_RATE_DEPTH_10=0.95`.
Each default is the measured median, rounded to the conservative side.
**Depth 10+ has no data: its value is copied from 4-9.**
**Small samples:** the 2-3 and 4-9 groups rest on 14 and 6 sales. Update
these values with `sale_vs_floor.py` as more data comes in.
`report.py`'s own copy of the Portals formula now calls the shared
function, so the formula exists in one place only.

**Effect**: break-even moves to ratio > 1.13 at depth 4-9 and to
ratio > 1.19 at depth 2-3. With the earlier median ratio of 1.08, a
typical signal now shows a loss. Expect far fewer clean signals: the old
ones were computed from an inflated sale price. Real depth of sent
signals was portals 2-3:40 / 4-9:58 / 10+:17, tonnel 2-3:6 / 4-9:16,
mrkt 2-3:15 / 4-9:6. Depth 1 never reached sending, because the
thin-book thresholds already remove it.

### 2. `unstable_floor` cascade stage

**Measured** on Portals, 130 pairs with 3+ floor snapshots: the median
max/min spread is 1.06. The spread is >= x1.5 in 22 pairs (16%), >= x2.0
in 11 pairs (8%) and >= x3.0 in 5 pairs (3%). Example: Snoop Dogg /
Woofee / Onyx Black, 13 snapshots, floor between 10.83 and 23.00.

**Change**: a new stage runs right after `below_min_profit`. For the
candidate's pair, it takes `max/min` of `pair_floor_excl_self_nano` from
`floor_snapshots` fetched in
`[observed_at - FLOOR_STABILITY_WINDOW_HOURS, observed_at]` (default
24h). The window is tied to the drop's own time, never to `now`: the
ladder bug came from a window tied to `now`. A row is dropped when the
ratio is `>= FLOOR_MAX_INSTABILITY` (default 2.0, which removes the 8% of
clear outliers). **With fewer than 3 snapshots, the stage is skipped:
too little data is never a reason to drop a row.** `floor_snapshots`
keeps one row per listing, so the pair's snapshots are the rows of
different listings in that pair. Tonnel and MRKT do not have 3 snapshots
per pair yet, so the stage skips them for now. This is expected.

### 3. Neighbour model floor as a lower bound (asymmetric rule)

**Measured** on 821 rows where both floors of one pair are known: the
model floor was above the pair floor **0 times**. pair/model: median
2.64, p25 1.53, p75 6.25. The model floor is a minimum over all
backdrops. The pair floor is a minimum over one. So the model floor is
a **lower bound** on the pair price. Earlier measurements: a neighbour
has the model 88% of the time, but has the pair only 40% of the time.

**Change** (`cross_check.py`): the neighbour is asked for its model floor
only when it has no listing of the pair. A neighbour that has the pair,
or that returned an error, is never asked. This costs about 60 extra
requests per 100 cross-checks.
- our price `>=` neighbour model floor: `skipped_neighbour_cheaper`, the
  signal is **blocked**.
- our price `<` neighbour model floor: `model_bound_inconclusive`. The
  signal is **not blocked**, but it is **not confirmed** either (no ✓).
- the model-floor query fails: `error`, the signal is not blocked.

New clients: `MrktClient.model_floor()`. Tonnel uses the existing
`model_floor()`, Portals uses `search_model_floor()`. A model-bound vote
never takes part in the neighbour-agreement vote (ДЕФЕКТ 5).

**The rule is deliberately asymmetric. The model floor can only DISPROVE
a profit. It must never be used to CONFIRM one.** A lower bound says
nothing about how much higher the neighbour's pair price is. Do not
extend `model_bound_inconclusive` into a confirmation, and do not show ✓
for it.

**Not verified live** in this environment. To check: run `report.py` on
the current DB. The `unstable_floor` stage should appear, and
`below_min_profit` should change. Then run the three pollers for 40
minutes. The `cross_check_snapshots` table should get the verdicts
`model_bound_inconclusive` and `skipped_neighbour_cheaper`.

## Open question: does a sale actually happen at the pair floor? (data collection started, no conclusion yet)

**The profit formula assumes a sale happens AT the pair floor**
(`signals.compute_profit_nano`: `floor*(1-fee) - price [- withdrawal
fee]`). That assumption has never been measured against a real,
confirmed sale price -- until this delivery, nothing in this project
recorded what a listing actually sold FOR next to what the floor was
AT THAT MOMENT. MRKT's `sale` feed event is the only confirmed-sale
signal anywhere in the project (Portals/Tonnel only ever give a bare,
cause-unknown disappearance -- see `db.record_sale`'s docstring).

**What's measured so far, and why no conclusion has been drawn**:
- 420 MRKT sales collected, 277 with a timestamp.
- Sale price vs. the AVERAGE floor over all time (a loose proxy, not
  the floor at the actual moment of sale): median ratio 0.70, n=28
  pairs.
- Sale price vs. the floor AT THE MOMENT OF SALE (the real question):
  median ratio 0.61, but n=6 pairs only -- including one clear outlier
  (Stellar Rocket/Neon Fuel: sold 5.41 at floor 97.92, ratio 0.06).
  **Six points is not a sample size a real decision can be made on.**
- Separately, and NOT in question: lots sell almost exactly at their
  OWN listed price (median sold/listed ratio 1.000, 265/420 an exact
  match) -- there is no haggling. The apparent below-floor pattern
  is entirely about WHICH lots sell (cheap ones) vs. which lots set
  the floor (the cheapest currently listed, which may just sit there).
- **Why the sample was so small**: the pair floor was only ever
  computed for listings that became signal CANDIDATES (a significant
  price drop) -- but most SALES are of listings that never dropped in
  price at all, so no floor was ever recorded for them.

**Fix (this delivery, data collection only -- no formula change)**:
`mrkt_poller.py`'s `_handle_sale_event` now queries the pair floor
(self-excluded by `gift_num`, same `mrkt_client.pair_floor()` used
everywhere else) at the moment of every sale `>= MRKT_COLLECT_MIN_
PRICE` (no point spending a request on a sale this project doesn't
even collect -- 85% of all trades, measured) and records it into three
new `listing_lifecycle` columns (schema v19): `floor_at_sale_nano`,
`floor_listed_count_at_sale`, `floor_fetched_at_sale`. A failed/thin/
no-data floor query NEVER loses the sale record itself -- the sale is
always written first, unconditionally; the floor fields simply stay
NULL. `gift_sniper/sale_vs_floor.py` is a new, read-only, no-network
analysis script: sale count with/without a recorded floor, the ratio
distribution (median/p10/p25/p75/p90/min/max, outliers outside
[0.2, 1.5] shown separately with examples), broken down by book depth
(1 / 2-3 / 4-9 / 10+) and by price segment (<30 / 30-100 / >100 TON),
plus up to 15 example rows.

**What this delivery deliberately does NOT do**: change `compute_
profit_nano` or any threshold based on the current 6-point sample --
per spec, that decision waits for a real sample size, which this
delivery exists to grow. Next step: let `mrkt_poller.py` run and
accumulate `floor_at_sale_nano` rows, then re-run `sale_vs_floor.py`
periodically until the "floor at moment of sale" sample is large
enough (tens of points, not 6) to actually decide whether -- and how
much -- the profit formula's floor-based assumption needs correcting.

**New tests**: `test_mrkt_poller.py` (КАК ТЕСТИРОВАТЬ items 1-4: floor
queried/skipped/erroring/no-data, sale always recorded regardless),
`test_sale_vs_floor.py` (item 5: empty DB; item 6: v18->v19 migration
preserves existing sale rows with the new columns NULL). 516 tests
total, zero regressions.

**Not verified live**: this environment has no network access; the
sanity-check (running `mrkt_poller.py` 30 minutes, then `sale_vs_
floor.py` showing a distribution on "at least ten points") has not
been performed.

## Systemic 6-day check: 5 defects, 4 fixed, 1 not reproduced despite real effort

**ДЕФЕКТ 1 -- signals sent with a missing/degenerate floor.** Measured:
6 sent Tonnel signals had `floor_level_at_drop = NULL` and
`floor_listed_count_at_drop = 0`. Traced the surrounding code (`poller.
py`/`tonnel_poller.py` always set `floor_at_drop_nano` and
`floor_level_at_drop` together, never one without the other) and found
that `thin_book` -- the stage that's SUPPOSED to catch `listed_count=0`
-- is fully driven by `FLOOR_MIN_LISTED_COUNT`/`TONNEL_FLOOR_MIN_
LISTED_COUNT`/`MRKT_FLOOR_MIN_LISTED_COUNT`, and only the Portals one
has a `>= 2` guard in `config.py` -- the Tonnel/MRKT variants can be
set to 0 or 1 by env override with no error at all, silently disabling
the filter. Fix: a new, LAST cascade stage `no_floor_at_send`
(`signals.run_cascade`) that is a **hardcoded** backstop -- floor is
`None` or `listed_count < 1` -- reading no config threshold at all, so
it holds "ни при каких настройках". New `CascadeResult.no_floor_at_send`
field, its own `report.py` counter line.

**ДЕФЕКТ 2 -- NOTIFY_LEVELS not applied.** Measured: with
`NOTIFY_LEVELS={'pair'}`, 54 Portals `level='model'` signals were still
sent. Direct end-to-end reproduction (`Poller._maybe_notify()`, a real
seeded DB, not the old tautological `signal.floor_level not in config.
NOTIFY_LEVELS` unit check) showed the CURRENT code actually filters
this case correctly -- `test_notify_levels_excludes_model_level_by_
default` was too weak to have ever caught a real regression here, since
it never exercised `_maybe_notify` at all; replaced with
`test_notify_levels_end_to_end_blocks_model_level_portals_send`, which
does. Root cause of the measured 54 not pinned down (same as ДЕФЕКТ 3
below -- see "not reproduced"). Still hardened per spec: Tonnel's
"NOTIFY_LEVELS doesn't apply to us, we're always level=model" was
previously a COMMENT, not enforced code -- `tonnel_poller.py` now
checks a real `TONNEL_NOTIFY_LEVELS` (default `{"model"}`), same
mechanism as Portals/MRKT's `NOTIFY_LEVELS`, proven enforced by
`test_tonnel_notify_levels_gate_is_real_not_just_documented` (set it to
`set()`, confirm a real Tonnel signal is suppressed).

**ДЕФЕКТ 3 -- cooldown bypass -- NOT REPRODUCED, despite serious effort.**
Measured: 28 repeat sends for one lot, 4 faster than
`SIGNAL_COOLDOWN_MIN=60`min, minimum interval 8.1min, two concrete
examples (21min/1.6% and ~72min/2.1%, both under `SIGNAL_RESEND_DROP_
PCT=10`). Read `poller.py`/`tonnel_poller.py`/`mrkt_poller.py`'s
`_check_cooldown` line by line -- all three correctly compare the NEW
signal's price against the LAST **SENT** alert's price (via `db.get_
last_sent_alert_for_listing`, which explicitly joins back to `price_
history` and filters `status='sent'`), not against the last DROP's
price, which was the user's own leading suspicion. Built and ran
several reproductions: (1) two drops arriving in the SAME `_maybe_
notify()` batch -- turned out to be a test-fixture artifact (a stale
mocked "current price" made the earlier of the two look artificially
stale, unrelated to cooldown); (2) two SEPARATE `_maybe_notify()`
cycles, properly spaced, small drops within the cooldown window -- this
is EXACTLY `test_poller_maybe_notify_suppresses_second_signal_within_
cooldown` (already existing, already passing) plus three NEW tests
(`test_item4_resend_21min_gap_small_drop_blocked_by_cooldown`,
`..._item5_resend_70min_gap_2pct_drop_blocked_by_resend_threshold`,
`..._item6_resend_70min_gap_15pct_drop_bypasses_cooldown`, all in
`test_notifier.py`) built directly from the reported numbers -- all
pass on the current code. No code change was made for this defect;
none of the fixes above touch cooldown logic. If it's still
reproducible on the real DB, the next step is a raw dump of the
`alerts_sent` rows for one of the 28 repeat-send lots (all statuses,
not just `status='sent'`) plus the exact marketplace, the same kind of
evidence that resolved the ladder investigation earlier in this
project's history.

**ДЕФЕКТ 4** -- already fixed in the immediately preceding delivery
(ladder window anchored to `now` instead of each candidate row's own
`observed_at`) -- included here for completeness only, no further
change.

**ДЕФЕКТ 5 -- cross-check inert in 87/102 (85%) of cases.** Measured:
`CROSS_MIN_NEIGHBOUR_COUNT=3` was UNREACHABLE across 102 real snapshots
-- no neighbour ever had 3+ listings (11 had 1, 5 had 2) -- so 25/102
were discarded as `neighbour_thin` even with real, usable prices
(Victory Medal/Dunk Master: Tonnel 11.44 at 2 lots; Instant Ramen/
Broccoli: 71.50 at 2 lots; Snoop Dogg/Super Bowl: 60.50 at 2 lots), and
62/102 had no comparable neighbour at all (`sent_no_neighbour`).
**Fix 1**: new `CROSS_AGREEMENT_PCT` (default 25%, `config.py`) -- when
2+ neighbours' floors agree within this percent of each other
(`abs(a-b)/min(a,b)*100`), their agreement becomes its OWN vote via
`_decide()` with the depth check forced to pass, added ALONGSIDE the
normal per-neighbour votes before combining (`cross_check.cross_
check()`) -- never a full override, so a higher-priority individual
vote (e.g. a real `skipped_neighbour_cheaper`) still wins unchanged. A
single thin neighbour alone is unaffected (still `neighbour_thin`).
**Fix 2**: the notification header's checkmark is conditional again --
`"ЛИСТИНГ ✓"` ONLY for `cross_verdict == sent_neighbour_higher` (a real
independent confirmation, including the new agreement vote), plain
`"ЛИСТИНГ"` otherwise (`sent_no_neighbour`, `neighbour_thin`, `error`,
`not_checked`). This is NOT a revival of the old, deliberately-removed
"confirmed"-gated checkmark from the Правка 1/2 delivery (see notifier.
py's docstring) -- that one broke because cross-check used to be a
SEPARATE signal type that could contradict a normal signal's own
verdict; today cross-check is still a pure pre-send filter (nothing
reaching `format_caption` was ever blocked), the checkmark now only
distinguishes "did an independent neighbour genuinely confirm a higher
price" from "we have no idea" -- a claim the old design never made and
this one is careful not to conflate with blocking.

**New tests**: `test_systemic_check_defects.py` (ДЕФЕКТ 1/2 cascade-
level items), `test_notifier.py`/`test_tonnel_poller.py` (ДЕФЕКТ 2/3
end-to-end, replacing/augmenting weak or now-outdated assertions),
`test_cross_check.py` (ДЕФЕКТ 5 items 7/8 plus 2 extra regression
guards). 506 tests total, zero regressions.

**Not verified live**: this environment has no network/DB access; the
sanity-check (running all three pollers 40 minutes on the real DB) has
not been performed. ДЕФЕКТ 2's exact root cause (the measured 54 leaked
Portals model-level sends) and ДЕФЕКТ 3 (the cooldown bypass) are
NEITHER confirmed as fixed NOR explained -- both were checked as
thoroughly as this environment allows and found not reproducible; the
hardening done for ДЕФЕКТ 2 (explicit `TONNEL_NOTIFY_LEVELS`) closes
one plausible gap but is not proven to be the actual cause of the
measured numbers.

## Root cause confirmed: ladder window was anchored to `now`, not to the candidate row's own observed_at

**User found the actual bug with a clean proof**: same DB, same 20-row
Clover Pin/Maple Leaf ladder, only `since` varied --
`since=now-1d -> 0`, `-2d -> 1`, `-3d -> 12`, `-4d -> 15`, `-7d -> 15`.
Since `clean_signals`'s `since` only post-filters an ALREADY-decided
`cascade.clean` set (it's never passed into `run_cascade`, only applied
afterward to what survived it), a changing count as `since` widens can
only mean more of an already-leaking set becomes visible -- proving 15
of the 20 rows were reaching `cascade.clean` in the first place. The
previous delivery's "couldn't reproduce" conclusion was wrong because
every reconstruction used a `now` close to the drops (same-day); this
listing's drops were several days old relative to the `now` used when
measuring on the real DB.

**Actual bug**: `_ladder_listings_live`'s window was `[now -
LADDER_WINDOW_HOURS, now]` -- correct for a live poller cycle (`now`
≈ the drop just recorded) but wrong for ANY evaluation that happens
well after the fact (a report run, or effectively any `since` wider
than `LADDER_WINDOW_HOURS`): with `now` 4 days after the drops,
`window_start = now - 24h` lands AFTER every one of the 20 drops, so
none are ever in range and the whole lot silently stops being
recognized as a ladder -- regardless of how obviously clustered the
drops are in their own right.

**Fix**: the window is now anchored at EACH CANDIDATE ROW's own
`observed_at`, never at `now` -- `[row.observed_at -
LADDER_WINDOW_HOURS, row.observed_at + LADDER_WINDOW_HOURS]`, looking
both directions. Both directions (not just "before") is deliberate:
it's what lets the FIRST 1-2 drops of an already-fully-recorded ladder
get recognized too, once later drops make the pattern visible -- a
batch/report evaluation of a completed 20-drop ladder now correctly
flags all 20, not just the ones from the 3rd drop onward. The decision
is now purely a function of the lot's own drop history and never
changes based on `now` or `since`, so a report run today gives the
same answer as one next week.

**A bug introduced while writing this fix, caught before shipping**:
the first version of the per-row check short-circuited to "once any row
of a listing qualifies, treat every row of that listing as a ladder" --
wrong, because a lot can have an isolated, unrelated significant drop
long before or after an unrelated 3+-drop burst; the shortcut swept
that unrelated drop into the burst too. Caught by
`test_item4_drops_outside_window_are_not_counted` (rewritten to test
this directly: 3 close-together drops plus one drop 2×LADDER_WINDOW_
HOURS away must NOT be lumped together). Fixed: `_ladder_listings_live`
now returns a set of `(listing_external_id, observed_at)` row keys, a
strictly per-row decision, not a set of listing ids.

**New tests**: `test_real_ladder_4_days_old_since_5_days_produces_
zero_signals` (the exact required regression test), plus
`..._since_1_day_also_zero` and `..._since_sweep_always_zero`
(reproducing the full `since` sweep from the live report, confirmed 0
at every point now, vs. the reported 0/1/12/15/15). Manually confirmed
the OLD `now`-anchored query gives count=0 for this exact data (proving
these tests would have failed pre-fix). 493 tests total, zero
regressions.

**Not verified live**: the fix has not been run against the actual
production DB; the user's own re-measurement is the next real check.

## Follow-up: previous ladder fix reported still broken on live data -- root cause not confirmed, hardened defensively

**User re-measured on the real DB after the previous delivery below and
reported it made things WORSE**: 812->1028 signals, 486->577 unique
lots, new lots with 15/23 signals (previously max 14). Gave an exact
reproduction case: `01a08b5d-27e2-7a62-ae78-3095d9ddf091` (Clover
Pin/Maple Leaf), 20 real consecutive ~5% drops ~30 minutes apart, all
`is_noise=0`, all `is_ladder=0`, 15 of the 20 reported as reaching
`clean_signals()`.

**Could not reproduce, despite trying hard.** Rebuilt this EXACT
listing's data (the literal timestamps/prices from the report) and ran
it through the current `clean_signals()` three ways: (1) a single batch
call over the full 20-row history, (2) an incremental simulation
calling `clean_signals(since=last_check, now=wall_clock_at_each_drop)`
once per drop, matching `poller.py`'s real `_maybe_notify` call
pattern, and (3) the same ladder seeded alongside 1500 unrelated
concurrent candidate listings, to rule out cross-lot contamination or a
SQLite bound-parameter ceiling (this build's is 32766 -- confirmed
empirically -- well above any plausible candidate count here). All
three gave 0 leaked signals for this lot, not 15. (First attempt used
an unrealistic `floor_at_drop_nano=200` for this fixture, which got
caught by the UNRELATED `price_above_own_floor`/implausible stages
regardless of the ladder logic -- corrected to a realistic ~40 TON
floor before drawing any conclusion from it.)

**Two real, defensible hardening changes were still made, since the
report is concrete and I cannot rule out something specific to the
production DB or deployment I don't have visibility into:**
1. `_ladder_listings_live`'s `IN (...)` query is now chunked (500 ids
   per query) instead of one query built from every candidate id at
   once. Harmless at any scale, and removes a theoretical ceiling this
   environment's SQLite build doesn't hit but another build/version
   might, for a marketplace with thousands of distinct candidate
   listings across its full history.
2. `run_cascade` now calls `backfill_ladder()` again on every
   invocation, in ADDITION to (not instead of) the live
   `_ladder_listings_live` computation that actually drives filtering.
   The previous delivery removed this call specifically to avoid a
   write-as-side-effect on a read path; restoring it costs two cheap
   UPDATEs per call and closes off one concrete way the regression
   COULD have happened: any code that reads `price_history.is_ladder`
   directly (not through `clean_signals()`) would have seen it frozen
   stale after the previous delivery, since nothing but `report.py`'s
   own block called `backfill_ladder` any more, and even that path was
   removed as redundant once this restore landed. Filtering itself
   still never reads this column back -- only the freshly computed set
   from `_ladder_listings_live`, per spec's "no dependency on a prior
   pass".

**New tests**: `test_real_clover_pin_maple_leaf_ladder_produces_zero_
signals` (the literal reported data, permanent regression guard) and
`test_ladder_detection_holds_at_production_scale_with_many_candidates`
(the same ladder amid 1500 concurrent candidates). Both pass. 490
tests total, zero regressions.

**Not resolved**: the actual root cause of the reported live regression
is still unknown -- every reconstruction attempt here produced correct
(zero-signal) behavior. Possible explanations not verifiable from this
environment: the running poller process hadn't picked up the previous
delivery's code yet (still running older code that never called
`backfill_ladder` at all -- which would explain "worse" independent of
anything in this delivery); a difference between this offline
reconstruction and the exact runtime call site/args used to measure
"15 of 20"; or something specific to the production DB's stored data
for this listing (exact `is_noise`/`marketplace` value encoding,
row count/scale) that a hand-built fixture doesn't capture. If the
hardening above doesn't resolve it, the next concrete step is a raw
dump (`sqlite3 .dump` or exported rows) of this listing's actual
`price_history` rows plus the exact code path/args used to call
`clean_signals()` in the measurement, so the reproduction can be built
from real bytes instead of a reconstruction.

## Ladder detection depended on a flag nobody but report.py ever refreshed

**Task premise vs. actual code, checked first.** The spec's diagnosis
(clean_signals's docstring saying "callers must run backfill_ladder()
first", run_cascade only ever READING price_history.is_ladder) does
NOT match this repo as found: `run_cascade` already called
`backfill_ladder(conn, now, marketplace)` unconditionally on every
invocation (an earlier delivery in this project's history had already
fixed that specific staleness). Empirically: a single `clean_signals()`
call over a full 19-drop ladder already returned 0 signals here, before
any change in this delivery. So the LITERAL bug as described (a
never-called prerequisite pass) was not reproducible against current
code.

**What WAS still wrong, and worth fixing anyway**: `run_cascade` made
`is_ladder` current by *writing* to `price_history` (two UPDATE
statements) on every single call -- including plain read paths like
`clean_signals()` being polled every cycle. A read path mutating shared
state as a side effect just to answer "is this a ladder" is exactly
the kind of implicit, easy-to-miss-on-a-future-refactor dependency the
task's diagnosis was worried about, even if it wasn't silently stale
today. Simulating incremental polling (call `clean_signals(since=last_
check)` once per new drop, as the real pollers do) on a synthetic
19-drop ladder gave 3 total signals across the whole run (the first 2
drops necessarily pass before LADDER_MIN_DROPS=3 is reached -- inherent
to any threshold-based detector, on-the-fly or precomputed, and NOT
something `MIN_DROPS`-preserving code can reduce to literally zero) --
nowhere near the measured "9 of 19" from production. That gap is not
reproduced or explained by this delivery; it may reflect a since-fixed
prior state of the code, or a difference between this offline
simulation and the real poller's exact call pattern.

**Fix, still made**: `signals._ladder_listings_live(conn, marketplace,
listing_ids, now)` -- a plain SELECT (no UPDATE) that computes, for
only the candidate listing_ids still alive after the noise stage, the
same "`>= LADDER_MIN_DROPS` significant (`is_noise=0`, real decrease)
drops within the trailing `LADDER_WINDOW_HOURS`" condition
`backfill_ladder` computes, but scoped to the current call, with zero
side effects. `run_cascade` now calls this instead of reading
`r["is_ladder"]`, and no longer calls `backfill_ladder` at all --
selection is fully decoupled from any separate pass, exactly per spec
("не полагаться на предварительный проход"). `backfill_ladder` itself
is unchanged and still used -- report.py's `_price_drops_block` now
calls it explicitly, purely to keep the persisted `is_ladder` column
current for the "ladder-down listings" audit block, with no bearing on
selection.

**ПРАВКА 2 audit of other backward-pass flags**: `is_bulk_update` is
NOT a persisted column at all (confirmed via `PRAGMA table_info`) --
`signals._find_bulk_update_ids` already computes it live, in Python,
against the candidate rows at cascade time. No fix needed.
`name_collision` (`floor_snapshots`) is set by `report.py`'s own
`backfill_name_collisions`, but is never read by `run_cascade`/
`clean_signals`/`notifier.py` -- it's a report-only display column, not
a selection gate. No fix needed.

**New tests** (`test_ladder_live_detection.py`): the ПРАВКА 3 test
(5-drop ladder, `backfill_ladder` never called, `clean_signals()`
returns none of it) plus all 6 КАК ТЕСТИРОВАТЬ items. Verified the
ПРАВКА 3 test actually discriminates: manually reading the (never
written) `is_ladder` column the old way on the same fixture gives 5
false signals, confirming the test would fail pre-fix. 488 tests
passing, zero regressions.

**Not verified live**: the "9 of 19" / "1.7 signals per lot average"
production numbers were not reproduced in this environment (no network/
DB access); the sanity-check (lots with 3+ signals near zero after the
fix) has not been run against production data.

## "Clean" signals could have zero/negative profit; 41% of Tonnel's used an hours-stale floor

**Two defects, both measured live over 3 days across Portals/Tonnel/MRKT.**

**ДЕФЕКТ 1 — no absolute-profit filter.** The cascade filtered noise,
anomalies, thin books, and implausible floors, but never profit itself.
Portals: 1379 "clean" signals, 548 (40%) with ratio < 1.05 (median
1.08) — including a genuinely NEGATIVE-profit row (Joyful Bundle/Pepe
Bag: price 32.45, floor 32.50, profit_usd = -4.54) and a two-cent-gap
row (Jingle Bells/Noble Pearl: price 16.65, floor 16.67, profit_usd was
`None` because it's level="model" — unfilterable by construction).
Tonnel: 44 clean, 5 with ratio < 1.05. A profit-at-ratio-1.05 table
(15 TON lot → $0.12, 30 → $0.74, 50 → $1.56, 100 → $3.62, 300 →
$11.86) shows profit scales with lot price — a fixed RATIO threshold is
the wrong shape of filter; the criterion has to be absolute profit.

**ДЕФЕКТ 2 — stale Tonnel floors.** 41% of Tonnel's clean signals used
a `snapshot`-sourced floor (vs. 99.9% fresh `at_drop` for Portals). Not
correlated with thin books (occurred at book depths 5,6,7,8,9,13 alike)
— `tonnel_poller.py`'s `_maybe_refresh_floor_snapshot` was silently
returning `None` from more branches than just "book too thin", making
the 41% unexplainable from the stats alone. Some of those snapshots
were measured 8-12 HOURS stale relative to the drop being evaluated,
during which the real floor moves dozens of times.

**Fix 1 — `signals.compute_profit_nano(marketplace, floor_nano,
price_nano)`, one function, no per-file duplication**: Portals
`floor*(1-0.02) - price - 0.35` (2% seller fee + 0.35 TON/GRAM flat
withdrawal fee); Tonnel `floor - price*1.1` (10% buyer fee, confirmed
live; no seller fee found — open question); MRKT `floor*(1-0.02) -
price` (2% buyer fee already baked into `salePrice`, confirmed
`salePrice/salePriceWithoutFee = 1.0200` exactly on every lot checked;
MRKT's withdrawal fee is unconfirmed and deliberately NOT subtracted —
same "don't fabricate a number" discipline as Tonnel's missing seller
fee). Profit is now computed at BOTH floor levels — previously `None`
at level="model", which is exactly what made those 548 Portals rows
unfilterable. At level="model" the result carries
`Signal.profit_is_estimate = True` (the model floor is a DIFFERENT
backdrop's price, confirmed live: Durov's Glasses #3685 showed ~$29
"profit" against a model floor that was another backdrop's price; the
listing was actually already the cheapest in its own pair, real flip
potential ~2.5 TON before fees) — but the `below_min_profit` threshold
still applies to it. New cascade stage `below_min_profit`, immediately
after `price_above_own_floor`, drops any row with profit <
`MIN_SIGNAL_PROFIT_TON` (new config, default 1.0 TON ≈ $1.4).
`notifier.py`'s `passes_notify_threshold`/`_priority` were updated to
key off `profit_is_estimate` instead of the now-obsolete `profit_usd is
None` check.

**Fix 2 — `tonnel_poller.py`'s `_maybe_refresh_floor_snapshot`** now
distinguishes and separately counts/logs every exit branch instead of
one undifferentiated "not ok, give up" path: `floor_refresh_ok`,
`floor_refresh_missing_name`, `floor_refresh_error` (network),
`floor_refresh_thin_book` (`status == "thin_model_book"`), and the
previously-hidden `floor_refresh_no_data` (`status == "no_data"` — a
genuinely empty query result, unrelated to book thinness, previously
lumped in with thin-book under one branch). Their sum equals
`price_drops_above_threshold`. Additionally, `signals.py` now checks
the AGE of a `snapshot`-sourced floor when no `at_drop` floor exists: a
new `stale_floor` cascade stage (positioned right after `no_floor`,
before `price_above_own_floor`) drops the row if the snapshot's own
`floor_fetched_at` is more than `MAX_SNAPSHOT_AGE_MIN` (new config,
default 30 minutes) older than the DROP's own `observed_at` — not the
caller's `now`, since `now` is used elsewhere (ladder-window detection)
for unrelated purposes and comparing against it would make every old
historical drop in a report run look increasingly "stale" for no
reason.

**Report (`report.py`)**: both new stages print their own cascade line
with removed/remaining counts; the per-line clean-signal table gained a
`profit_ton` column (estimate rows marked with a trailing `~`); the
old "clean signal profit (level=pair only...)" summary — now
factually wrong since profit is computed at both levels — was split
into separate CONFIRMED (level=pair) and ESTIMATED (level=model)
profit distributions.

**КАК ТЕСТИРОВАТЬ item 3 / item 2 arithmetic note**: the spec's worked
examples state Portals price=30/floor=40 → profit 9.05, and Tonnel
price=32.45/floor=32.50 → profit -4.54. Both formulas as specified
(and as implemented, and matching the spec's OWN "Расчёт прибыли при
ratio 1.05" table, e.g. price=100 → 105*0.98-100-0.35=2.55, an exact
match) actually give 8.85 and -3.195 respectively. Treated as slips in
the task text, not the formula — tests assert the mathematically
correct values; the qualitative claims ("passes" / "negative, dropped")
hold either way. See `test_min_profit_and_stale_floor.py`.

**New tests**: `test_min_profit_and_stale_floor.py` covers all 8
КАК ТЕСТИРОВАТЬ items; `test_report.py` gained tests for both new
cascade lines and the `profit_ton` column; `test_notifier.py`'s stale
`_model_signal` helper (which hardcoded `profit_usd=None`) was updated
to `profit_is_estimate=True` to match the new always-computed behavior.
481 tests passing, zero regressions.

**Not verified live**: this environment has no network/DB access. The
sanity-check — running `report.py` against the real DB and confirming
`below_min_profit` cuts ~40% of Portals' rows and every remaining
clean-signal row shows `profit_ton >= 1.0` — has not been run against
production data.

## MRKT poller was ignoring 1 in 5 feed events; two of them (unlisting/return) risked false signals

**Measured on 200 feed events**: `listing` 70, `change_price` 49, `sale`
40 were handled; `unlisting` 19, `return` 14, `lucky_buy` 4,
`plinko_win` 3, `crafting` 1 were NOT -- all five fell into
`unknown_event_type_count`, which reached 260/1230 (1 in 5) over a real
20-minute run.

**Why `unlisting` mattered most**: a seller-unlisted lot that this
poller never marks as gone stays in `listings`/`listing_lifecycle`
looking active. It doesn't corrupt `mrkt_client.pair_floor()`'s live
floor queries directly (that query's own `isOnSale=false` filter already
excludes it in real time -- confirmed, see `test_mrkt_client.py`), but
it DOES corrupt this project's own bookkeeping: report.py's liquidity
stats, and any future feature reading `listings`/`listing_lifecycle`
directly, would see a phantom active listing indefinitely.

**Fix (`mrkt_poller.py` + `db.py`)**:
- `unlisting` -> `db.record_lifecycle_status(..., "unlisted", ...)` --
  reuses the EXISTING `"unlisted"` status (already meant this for
  Portals) rather than inventing a new one. Confirmed live example:
  PrettyPosy-132026, `salePrice=6426000000`, `isOnSale=false`.
  `sold_price_nano` is deliberately NOT touched.
- `return` -> a NEW `"returned"` status, added to `db.DISAPPEARED_STATUSES`.
  Deliberately kept SEPARATE from both `"sold"` and `"unlisted"` -- a
  gift returned to its owner in Telegram is a third, genuinely different
  outcome, and blending any of the three would corrupt liquidity metrics
  (how often gifts actually change hands vs. get pulled vs. get returned
  are three different questions).
- `lucky_buy`/`plinko_win`/`crafting` -> MRKT's own game mechanics,
  unrelated to ordinary trading -- explicitly skipped via a new
  `events_game_skipped` counter, kept OUT of
  `unknown_event_type_count` per spec (these are known types, not
  mystery ones). `crafting`'s gift has `isOnSale=true` (it's a craft
  RESULT, not a listing) -- explicitly never written to `listings`.
- Genuinely unknown types still increment `unknown_event_type_count`,
  now logging the FULL event (not just id/type) so a real new type can
  actually be diagnosed from the log, per spec.

**`mrkt_error_count` breakdown**: was one undifferentiated bucket (12
in one 20-minute run, no way to tell what they were). Now classified by
`MrktError.status_code` exactly like Tonnel's 403/429/5xx split
(`tonnel_poller.py`): `mrkt_403_count`/`mrkt_429_count`/`mrkt_5xx_count`,
with `mrkt_error_count` now meaning only the leftover cases (network
failure, non-JSON body, unexpected shape -- `status_code` is `None` for
all of these). Every classified error is also logged with its call-site
context and the exception text, at all three of this poller's MrktError
call sites (feed fetch, at-drop floor query, pre-send freshness check --
the last of these previously didn't even count toward
`mrkt_error_count` at all, a second small bug caught while fixing this).

**Not a bug, per spec -- recorded here so it isn't "fixed" again by
mistake**: `sales_recorded` (263) vastly exceeding actual DB rows (13)
in a real run is explained entirely by `MRKT_COLLECT_MIN_PRICE` (default
15 TON): measured on a 40-sale sample, 34 (85%) were for lots priced
below 15 TON -- lots this poller never collects into `listings` in the
first place. `sales_recorded` counts every `sale` event seen on the
feed; the DB only ever holds sales for listings we actually track. This
is the intended behavior of the collection threshold, not a discrepancy
to chase.

**Not verified live** (no live network access in this environment): the
real 20-minute run this bug was originally measured on. Covered instead
by `test_mrkt_poller.py` (all 5 КАК ТЕСТИРОВАТЬ items: unlisting sets
`unlisted` not `sold`, return sets `returned`, all three game event
types counted separately from unknown and never written to `listings`,
an unknown type logs the full event, and an unlisted gift is excluded
from a floor query end-to-end) plus three new tests for the
403/429/5xx/generic error classification.

## MRKT is now a full third signaller -- a real event feed was found, mrkt_poller.py

**Found the missing piece**: MRKT's `/gifts/saling` showcase (used since
the cross-check-neighbour delivery) has confirmed RANDOM ordering, so
MRKT could never be a signal source through it. A SEPARATE endpoint,
`POST /api/v1/feed` (`{"count": 20, "cursor": ""}`), was found and
confirmed live to be a real, strictly chronological event stream:
`type` (listing/sale/change_price), `id` (event id), `amount`
(nano-TON, confirmed NOT to need conversion), `date` (ISO8601 UTC,
strictly descending), `gift` (the full lot object). Confirmed:
`count` capped at 20 (same as `/gifts/saling`); cursor pagination has
ZERO overlap between pages; ~1.2 events/sec; at a 30s poll interval the
first page's 20 events fully turned over -- events are GUARANTEED lost
at that interval, hence `MRKT_POLL_INTERVAL_SEC` defaults to 8s.

**`mrkt_client.py`** gained `.feed(count, cursor) -> (items, cursor)`
-- added to the EXISTING module, not a new one (per spec, MRKT already
has a client). Same auth/headers/curl_cffi discipline as `pair_floor()`/
`find_by_number()`. `amount` is confirmed already nano-TON, same fixed
convention as `salePrice` (see the earlier double-conversion bug fix)
-- never multiplied here either.

**New `mrkt_poller.py`** (by-example of `tonnel_poller.py`, explicitly
NOT a subclass of it or `poller.Poller`, per spec): pages the feed each
iteration up to `MRKT_MAX_PAGES_PER_ITERATION` (default 8), stopping the
moment a known (already-processed) event id is hit -- the feed's
confirmed strict chronological order with zero page overlap means
everything after a known event is already-processed too, so this is a
correct and cheap stopping rule, not a heuristic.

Per-type handling:
- **`listing`**: writes `listings` (marketplace='mrkt'), `tg_id` taken
  directly from `gift.name` (confirmed already the tg_id shape, e.g.
  "SnoopCigar-46116" -- never constructed, unlike Tonnel's). Filtered by
  `MRKT_COLLECT_MIN_PRICE` (default 15 TON, converted to nano) --
  there's no server-side price filter on this endpoint at all.
- **`change_price`**: the event carries NO old price (confirmed) -- the
  old price comes from THIS PROJECT'S OWN `listings` row. If the
  listing isn't known yet (a change_price for a gift this poller never
  saw get listed, e.g. right after a cold start), it's written as a
  brand-new listing instead -- there's nothing to compare against, so
  no `price_history` row is fabricated. On a significant drop, MRKT's
  own PAIR floor is re-queried (`mrkt_client.pair_floor()`, already
  written) and `floor_at_drop_nano`/`floor_level_at_drop="pair"` are
  filled in the SAME `price_history` row -- one network call for both
  the `floor_snapshots` write and the at-drop write, same discipline as
  `tonnel_poller.py`'s mirror-image method (reused, not reinvented).
- **`sale`**: the ONLY marketplace event in this entire project that
  EXPLICITLY confirms a sale -- Portals and Tonnel only ever observe a
  bare disappearance with no confirmed cause (see README history on
  "исчезновение без причины"). New `db.record_sale()` sets
  `disappeared_at`, `final_status='sold'`, and a NEW
  `listing_lifecycle.sold_price_nano` column (schema v18) -- the first
  time this project has ever recorded a confirmed sale price at all.

**Event-level dedup**: new `processed_events` table (schema v18,
composite PK `(marketplace, event_id)`) -- protects a restart from
reprocessing an event it already handled (which would double-write
`price_history` rows or re-mark a sale). `db.is_event_processed()` /
`db.mark_event_processed()`.

**Signals (`signals.py`)**: `_thresholds_for()` gained an `'mrkt'`
branch (`MRKT_PRICE_DROP_MIN_PCT`/`MRKT_FLOOR_MIN_LISTED_COUNT`/
`MRKT_FLOOR_MAX_RATIO_TO_PRICE`) -- the SAME shared cascade, not a
duplicated one, per spec. **MRKT's floor level is `"pair"`, unlike
Tonnel's `"model"`** -- the reason is structural, not a stylistic
choice: MRKT's `/gifts/saling` pair-level query returns a genuine
`total` depth-of-book figure for one exact (collection, model,
backdrop) triple with NO pagination needed, so the precise pair
comparison is actually usable here (Tonnel's pair level, by contrast,
is confirmed always empty -- its model level is the only basis that
delivery had data for). `_signal_from_row()`'s profit-formula gate
gained an explicit MRKT branch: `floor - price` with NO multiplier on
either side, since MRKT's `salePrice` is confirmed to already include
its 2% buyer fee on BOTH sides of that subtraction (unlike Tonnel's
`floor - price*1.1`, where only the buy side needs the multiplier) --
this was a real bug caught before it shipped: the existing profit gate
(`marketplace != "tonnel" and level == "pair"`) would otherwise have
silently routed every MRKT signal into PORTALS' fee formula
(`MARKETPLACE_FEE_RATE`/`WITHDRAWAL_FEE_FLAT_NANO`), unconfirmed costs
that don't apply to MRKT at all, and -- more subtly -- `passes_notify_
threshold()` requires a non-None `profit_usd` for any `level="pair"`
signal, so getting this wrong would have silently blocked every single
MRKT signal from ever being sent.

**Cross-check (`cross_check.py`)**: gained a third dispatch branch --
an MRKT signal's neighbours are Portals and Tonnel, **never MRKT
itself** (the earlier two-branch `if portals / else` would have
incorrectly treated an MRKT signal like a Tonnel signal, querying
Portals + MRKT and never Tonnel, and would let MRKT check itself).

**Notifications (`notifier.py`)**: `_mrkt_lot_link()` --
`https://t.me/mrkt/app?startapp=<id-no-dashes>` (confirmed live: id
`4c667e31-e667-40ed-a41d-641791998bb9` -> `startapp=
4c667e31e66740eda41d641791998bb9`) -- dashes stripped, nothing else.
Same unified header/button/card-link format as every other marketplace
(unchanged, already marketplace-agnostic). `MRKT_NOTIFY_ENABLED`
(default false) gates sending independently, same pattern as
`TONNEL_NOTIFY_ENABLED`.

**`report.py`**: `--marketplace mrkt` now works (the existing
Tonnel-shaped generic branch in `generate_report()` was widened from a
binary "tonnel or not" check to a lookup covering all three non-Portals
marketplaces' own threshold-label names); `_cross_direction_block` gained
the two remaining direction pairs, MRKT->Portals and MRKT->Tonnel.

**Not verified live** (no live network access in this environment): the
SANITY-CHECK's 20-minute run (nonzero `new_listings`/
`price_changes_seen`, `sale` events landing with `final_status='sold'`,
`processed_events` growing with no duplicates). Covered instead by
`test_mrkt_client.py` (`feed()`'s body/URL/error handling),
`test_mrkt_parsing.py` (tg_id-as-is, no price multiplication, exclusion
filters), `test_mrkt_poller.py` (all 10 КАК ТЕСТИРОВАТЬ items: dedup,
pagination-stops-at-known-event, the `listing`/`change_price`/`sale`
event handlers including the unknown-listing `change_price` case, the
collect-min-price filter, and an end-to-end cross-check against fake
Portals/Tonnel clients), and `test_migrations.py`'s class-level ON
CONFLICT regression test (extended to cover `processed_events` and
`listing_lifecycle.sold_price_nano` surviving the v17->v18 migration).

## Fixed: Portals poller crashed on MRKT cross-check -- salePrice was already nano-TON, double-converted

**Bug**: `OverflowError: Python int too large to convert to SQLite
INTEGER` in `db.record_cross_check_snapshot`, crashing the whole Portals
poller whenever a signal got cross-checked against MRKT.

**Root cause**: `mrkt_client.py`'s `pair_floor()` did
`floor_nano = int(floor_price * config.NANO)`, but MRKT's `salePrice` is
**already** an integer in nano-TON (confirmed live: `16289400000` on the
wire == 16.29 TON) -- unlike Portals (`price` is a decimal STRING
needing conversion) and Tonnel (`price` is a JSON FLOAT needing
conversion), MRKT needs **no unit conversion at all**. The bug
multiplied an already-nano value by `config.NANO` a second time,
producing ~1.6e19 -- past SQLite's own INTEGER ceiling (~9.2e18) -- and
`sqlite3` raised `OverflowError` instead of accepting or rejecting the
value gracefully.

**Fix (`mrkt_client.py`)**: `floor_nano = int(min(prices))` -- no `*
config.NANO`. Every other place in the module that touches price-shaped
fields (`minPrice`/`maxPrice` in the request body, always `None`, never
computed from a TON amount; `floorPriceNanoTONsByCollection`, never read
at all) was reviewed per spec and found to need no fix -- neither is
actually converted anywhere in this codebase yet.

**Defensive backstop (`db.py`, schema-independent)**: `SANITY_MAX_NANO =
10**16` (10 million TON/GRAM -- no real gift is worth that).
`record_cross_check_snapshot()` now refuses to write a
`neighbour_floor_nano` above this threshold: logs the offending
marketplace and value, returns without writing the row, **never
raises**. This is deliberately a backstop, not a substitute for correct
conversion at the source -- it exists so that if a similar unit bug ever
recurs (this project's own code, or an API's own field ever changes
shape without notice), the failure mode is "one row silently skipped,
logged" instead of "the entire poller process dies". `None` (a
genuinely absent neighbour, verdict `sent_no_neighbour`) is never
mistaken for an absurd value -- the check only applies to actual
numbers.

**Not verified live** (no live network access in this environment): a
real 30-minute run confirming the crash no longer reproduces. Covered
instead by `test_mrkt_client.py` (fixed conversion, using the exact
measured `salePrice=16289400000` example), `test_db_sanity_nano.py`
(new -- the backstop's boundary behavior: refused above threshold,
accepted at/below it, `None` never triggers it, logged not raised), and
`test_cross_check.py` (a real `MrktClient` wired through `cross_check()`
end-to-end without `OverflowError`, plus all three marketplaces'
conversion paths compared side by side in one test so a future
regression in any one of them is caught here, not just in isolation).

## MRKT added as a THIRD cross-check neighbour -- never a signal source, no reliable freshness feed exists

**New module `mrkt_client.py`** -- a standalone client for MRKT
(tgmrkt.io), by design NOT a subclass of tonnel_client.py or
portals_client.py (three independently-evolving protocols). All facts
below are confirmed by live requests, not assumed.

**Auth is genuinely different from every other marketplace here**:
`Cookie: access_token=<uuid>` -- confirmed live that `Authorization`
does NOT authorize at all (with-Authorization-no-Cookie -> 401,
with-Cookie-no-Authorization -> 200). `origin`/`referer` MUST be
`https://cdn.tgmrkt.io`, not `api.tgmrkt.io` (confirmed the API host as
origin is rejected). Token comes from `MRKT_ACCESS_TOKEN`, read lazily
via `config.get_mrkt_access_token()` (same discipline as
`get_portals_auth()` -- never required at import time, returns `None`
instead of raising when unset).

**Request body is a full, fixed-shape object** -- every field from the
measured example is always sent, even when null/empty; only
`collectionNames`/`modelNames`/`backdropNames`/`number`/`count`/`cursor`/
`ordering`/`lowToHigh` vary per call. Confirmed live: `ordering` only
accepts `"None"`/`"Price"`/`"Number"` (`"Date"`/`"Latest"`/`"CreatedAt"`
all return HTTP 400); `count` is silently capped at 20 server-side (50
and 100 truncate, don't error).

**Fee convention is the THIRD distinct one in this project** --
`salePrice` already has MRKT's 2% fee baked in (confirmed
`salePrice`/`salePriceWithoutFee` = 1.0200 on every lot checked), so it
is compared **as-is, never multiplied** -- unlike Tonnel (`price * 1.1`
for the buyer fee) and unlike Portals (no buyer fee at all, the listed
price already IS what a buyer pays). `MrktFloor` deliberately has only
ONE price field (no `floor_with_fee_nano` counterpart) since there is no
separate "before fee" variant to track.

**`listed_count` comes from the response's `total` field, not
`len(gifts)`** -- confirmed the feed page is capped at `count<=20` while
`total` reports the real depth of the book under the filter. Never use
page length as a stand-in for book depth here.

**`floorPriceNanoTONsByBackdropModel` is confirmed ALWAYS null** (100
lots checked) -- never used; the floor is computed the same
self-excluded-minimum way as every other marketplace in this project,
from `pair_floor(collection_name, model_name, backdrop_name,
exclude_number)`'s own live filtered query (drops `isOnSale=false`,
`isOnAuction=true`, `isLocked=true`, `isLockedForSale=true`,
`premarketStatus != "None"`, and the excluded lot).

**Deliberately NOT a signal source** (per spec, explicitly out of
scope): MRKT has no reliable "newest listings" ordering. Confirmed live:
`ordering="None"` gave an UNSTABLE order -- all 20 of the first page's
lots changed within 60 seconds, while the first three `receivedDate`
values were 2026-09-12 08:50, 2026-09-12 10:21, and 2026-09-10 13:45 --
not freshness-ordered at all. `isNew=true` returned a lot from
2026-08-29 -- "new" doesn't mean recently listed. A full catalog walk
(144785 lots / 20 per page at ~19.6 lots/sec) would take ~2 hours --
impractical as a polling strategy, and there is no cursor-stable way to
detect "what's new since last time" without one. This question stays
OPEN, not closed -- MRKT could become a signal source later if a real
freshness signal turns up.

**`cross_check.py` generalized from one neighbour to a LIST of
neighbours per signal.** For a Portals signal, neighbours are now Tonnel
AND MRKT; for a Tonnel signal, neighbours are Portals AND MRKT. Each
neighbour is queried independently and votes its own verdict via the
SAME unchanged `_decide()` function; a NEW `_combine()` function reduces
all votes to one overall `Signal.cross_verdict` by priority: any
neighbour voting `skipped_neighbour_cheaper` wins outright (BLOCKS the
send -- "достаточно ОДНОГО такого соседа"), else any
`sent_neighbour_higher` wins (a real independent confirmation exists),
else `neighbour_thin`, else `error`, else `sent_no_neighbour`. A
neighbour's query failure votes `"error"` for THAT neighbour only and
never stops the others from being queried -- each neighbour is tried in
its own try/except inside the same loop. A snapshot row is written to
`cross_check_snapshots` for EVERY queried neighbour (not just the
deciding one) -- `Signal.neighbour_marketplace`/`neighbour_floor_nano`/
etc. are set to whichever neighbour's vote decided the overall verdict,
for display/debugging only; the snapshots table is the authoritative
per-neighbour record.

**Schema v17 -- `cross_check_snapshots`' PRIMARY KEY widened** from
`(signal_marketplace, listing_external_id, fetched_at)` to
`(signal_marketplace, checked_marketplace, listing_external_id,
fetched_at)`. Root cause this had to be fixed BEFORE MRKT could actually
work: with a third neighbour queried in the same `cross_check()` call
(same `fetched_at` as an existing neighbour's row for the same signal),
the second neighbour's INSERT collided under the old PK and got silently
dropped by `ON CONFLICT DO NOTHING` -- this is the FOURTH time this
exact class of ON CONFLICT/PK bug has been fixed in this project
(price_history, listing_lifecycle, floor_snapshots/alerts_sent, now
this) -- caught here by the same "test on the real writes, not just
column presence" discipline as every prior instance (see
`test_migrations.py`'s class-level regression test, extended with a
same-`fetched_at`-different-`checked_marketplace` case).

**`MRKT_CROSS_CHECK_ENABLED`** (env, default `true`) gates MRKT
independently of `CROSS_CHECK_ENABLED` (which still gates Tonnel<->
Portals) -- MRKT can be turned off on its own. Missing
`MRKT_ACCESS_TOKEN` degrades to "MRKT not queried", logged exactly ONCE
per process via `mrkt_client.build_default_mrkt_client()` (a shared
factory used by both `poller.py` and `tonnel_poller.py`, never crashes
either poller).

**`report.py`**: `_cross_direction_block` now shows FOUR directions
(Portals->Tonnel, Portals->MRKT, Tonnel->Portals, Tonnel->MRKT)
separately, grouped by `(signal_marketplace, checked_marketplace)`
rather than `signal_marketplace` alone (a signal can now have snapshot
rows for more than one neighbour, so the old single-key grouping would
have blended them).

**Not verified live** (no live network access in this environment): the
SANITY-CHECK's 30-minute run showing `checked_marketplace='mrkt'`
verdicts in both summaries and the new report directions. Covered
instead by `test_mrkt_client.py` (headers, body shape, auction/locked/
premarket exclusion, salePrice-as-is, `total`-not-`len` depth,
self-exclusion, error handling, the token-optional factory),
`test_cross_check.py` (multi-neighbour combining: one bad neighbour
blocks, both-higher sends, one neighbour's error doesn't block or stop
the others, the disabled/no-token cases), `test_migrations.py` (the new
PK actually holds under concurrent-neighbour writes), and `test_report.py`
(the two new MRKT directions appear).

## Tonnel signals were comparing against a stale floor -- floor_at_drop_nano now filled at the moment of the drop

**Bug, measured on the real DB**: `price_history` significant drops --
Tonnel 195 rows, `floor_at_drop_nano` filled on 0 of them; Portals
10160 rows, filled on 9631. In the report, Tonnel's "clean signal floor
source" was `at_drop=0, snapshot(backfilled)=17`, vs. Portals'
`at_drop=1714, snapshot=4`. Every Tonnel signal was falling back to
whatever `floor_snapshots` happened to hold -- measured ages on rows
actually used: 3.4h, 8.3h, 8.4h, 12.4h stale relative to the drop. In
that time, roughly 40 price changes happen on Tonnel per half-hour run,
so a signal could easily be judged against a floor from well before the
relevant competitors even existed.

**Root cause**: `poller.py` (Portals) re-queries the floor AT THE MOMENT
of a significant drop and writes it straight into that same
`price_history` row (`floor_at_drop_nano`/`floor_listed_count_at_drop`/
`floor_fetched_at`/`floor_level_at_drop`) -- see
`Poller._process_known_items`. `tonnel_poller.py` had the re-query
(`_maybe_refresh_floor_snapshot`, from the Tonnel model-floor delivery)
but only ever wrote it to `floor_snapshots` -- never back into the
`price_history` row the drop itself created. `signals.py`'s cascade
prefers an `"at_drop"` floor over a `"snapshot"` one when both exist
(see `_floor_and_source`), so this wasn't a missing fallback -- it was a
missing PRIMARY source, silently downgrading every single Tonnel signal
to the (frequently stale) fallback.

**Fix**: `_maybe_refresh_floor_snapshot()` now returns `(floor_nano,
listed_count)` when it found a real, usable (`"ok"`, already past
`TONNEL_MODEL_MIN_LISTED_COUNT`) floor, or `None` otherwise (thin book,
no data, or the query failed) -- ONE network call, reused for both
writes (never two requests for one drop). `_maybe_record_price_change()`
uses that return value to also pass `floor_at_drop_nano`/
`floor_listed_count_at_drop`/`floor_fetched_at`/`floor_level_at_drop=
"model"` into `db.record_price_change()` for the SAME row -- mirroring
`poller.py`'s Portals mechanism exactly, reused rather than
reimplemented. `floor_snapshots` is still written too, unchanged (the
`"snapshot"` fallback source still needs a current value for listings
that didn't just drop). Below `TONNEL_PRICE_DROP_MIN_PCT`, nothing is
queried at all -- same discipline as before this fix, and as Portals.
A query failure leaves `floor_at_drop_nano` NULL and logs a warning, but
the price drop itself is still recorded -- a Tonnel-side hiccup must
never lose a real price change.

**Measured cost**: 72 significant drops over a 7.7h run, ~10
requests/hour -- negligible, no Tonnel rate limit observed at this
volume (consistent with every other at-drop query this project already
makes there).

**Not verified live** (no live network access in this environment): the
SANITY-CHECK's live 30-minute run and its `SELECT COUNT(1),
SUM(floor_at_drop_nano IS NOT NULL) ... WHERE marketplace='tonnel' AND
is_noise=0` check (second number should track close to the first among
fresh rows). Covered instead by unit/integration tests in
`test_tonnel_poller.py` (fills correctly on a real floor, stays NULL on
a thin book or a query error, never queries below the noise threshold,
the price drop is recorded regardless of the floor query's outcome) and
`test_report.py` (the report's `at_drop` count going from 0 to nonzero
for Tonnel).

## price_above_own_floor: mandatory cascade stage, all marketplaces/levels -- a lot priced above its own floor is never a signal

**Measured on 25 Portals model-level signals**: 18/25 (72%) were priced
ABOVE their own model floor -- e.g. Bling Binky/Regent: price 210.00,
floor 34.00 (x6.2 overpriced); Instant Ramen/Turtle: price 46.00, floor
4.78 (x9.6); Swiss Watch/Patriot: price 220.00, floor 128.00 (x1.7);
Mousse Cake/Luxury: price 15.00, floor 7.50 (x2.0). A lot priced above
the cheapest known price for its OWN model can never be a bargain,
regardless of how large its own price drop looked in isolation --
`delta_pct` measures a change from the lot's own history, not a
comparison against the market.

**Root cause**: Portals' cascade never had this check at all, at either
floor level -- `is_implausible` only rejects a ratio that's too HIGH
(`floor/price > FLOOR_MAX_RATIO_TO_PRICE`), never one that's too LOW or
negative (`floor/price <= 1`, i.e. price >= floor). Tonnel had an
equivalent check (`no_discount`, from the two-way cross-check delivery),
but it was written Tonnel-only, duplicating logic Portals needed too.

**Fix (`signals.py`)**: `CascadeResult.no_discount` renamed to
`price_above_own_floor` and GENERALIZED to run for every marketplace and
every floor level (pair AND model), not just Tonnel's model level. The
stage: `new_price_nano < floor_nano` required, or the row is rejected --
strict inequality, so a price exactly equal to the floor is rejected too
(no benefit: buying at the floor price isn't a discount, it's the floor
itself). Placed in `run_cascade()` immediately after the existing "floor
no_data" stage, BEFORE thin-book/is_implausible/is_bulk_update -- per
spec, so a lot that was never a real signal doesn't reach the expensive
later stages, in particular cross-check's network call to the other
marketplace (`cross_check.py`, which only ever runs against
`clean_signals()` output -- filtering this early means a doomed signal
never spends that request at all).

**`report.py`**: prints the new stage's removed/remaining count in the
cascade breakdown, right after the "floor no_data" line, matching the
actual filter order.

**КАК ТЕСТИРОВАТЬ item 5 verified structurally**: since cross-check only
ever sees `clean_signals()`'s output (post-cascade), and this stage now
runs early in that same cascade, a rejected signal is architecturally
incapable of reaching cross-check -- confirmed by a dedicated test with
a Tonnel client mock that raises `AssertionError` if called at all.

**Not verified live** (no live network access in this environment): the
spec's SANITY-CHECK expectation (~70% of Portals model-level signals cut
by this stage on the real, current DB). Covered instead by unit tests
using the exact measured example (Bling Binky/Regent, 210.00/34.00) and
a source-inspection test confirming the stage is NOT duplicated per
marketplace (no `marketplace ==` branch around it, unlike the retired
Tonnel-only `no_discount`).

## Cross-market signal type removed; cross-check is now a pure pre-send filter; notifications unified

**Bug this closes**: the previous delivery's cross-check produced a
real contradiction, confirmed live -- Victory Medal #86056 got verdict
"worse" as a Portals signal (rejected, not sent) AND was simultaneously
sent as a SEPARATE "МЕЖБИРЖЕВОЙ" (cross-market) notification ("buy on
Tonnel, sell on Portals"). One lot, two opposite decisions reaching the
user. Root cause: cross-check was doing two unrelated jobs at once --
deciding whether the ORIGINAL signal was trustworthy, and independently
building a whole SECOND signal type off the same underlying price gap.

**Правка 1 -- the cross-market signal type is gone, completely.**
Removed: `CrossMarketSignal`, `build_cross_market_signal`,
`format_cross_market_caption`, `build_cross_market_keyboard`,
`send_cross_market_signal`, `CROSS_MARKET_MIN_GAP_PCT`, the
`cross_market_signals_sent` counter. A cross-marketplace price gap is no
longer its own notification -- it is now only an input to whether the
underlying signal gets sent at all (see Правка 3). One signal, one
decision, always.

**Правка 2 -- one notification format for every signal, every
marketplace.** `notifier.format_caption()`'s header is now ALWAYS
`"ЛИСТИНГ ✓"` -- no marketplace name (`"ЛИСТИНГ TONNEL"` is gone), and
the checkmark is UNCONDITIONAL, not tied to `cross_verdict` anymore (the
old rule -- checkmark only when `cross_verdict=="confirmed"` -- is what
let the contradiction above happen: a signal could be "not confirmed"
by the old scheme's standards yet still literally be the thing being
sold, correctly, right now). A signal that reaches `format_caption` has,
by construction, already passed the cross-check filter (Правка 3) --
so there is nothing left for the checkmark to conditionally represent.
`build_keyboard()`'s button text is now always `"Купить"` (was
`"Купить на Tonnel"`/`"Купить на Portals"`) -- only the URL still
differs per marketplace (Tonnel vs. Portals deep link), never the label.
The `t.me/nft/<tg_id>` gift-card link (inline, visible "·" character on
the FLOOR line) was already present on every real signal notification;
this requirement is trivially satisfied now that the cross-market type
(which never had this link) no longer exists as a notification path at
all. Per spec: "пользователю не нужно знать, с какой площадки сигнал —
ему нужно нажать кнопку и купить."

**Правка 3 -- cross-check is now a PURE PRE-SEND FILTER, new decision
rule.** `cross_check.py` rewritten: let P be the signal's own price and
N the neighbour's floor price (fee-adjusted where applicable -- Tonnel's
confirmed 10% buyer fee when Tonnel is the neighbour, no fee at all when
Portals is the neighbour). `N` absent -> SEND (`"sent_no_neighbour"`);
`N <= P * (1 + CROSS_MIN_GAP_PCT/100)` -> DO NOT SEND
(`"skipped_neighbour_cheaper"`); `N > that` -> SEND
(`"sent_neighbour_higher"`). `CROSS_MIN_GAP_PCT` (env, default 10)
replaces the removed `CROSS_MARKET_MIN_GAP_PCT`/`CROSS_MAX_RATIO` --
rationale: if the neighbour has the same pair for the same price or only
slightly more, a buyer would just go there, so there's nothing to gain
from sending this signal at all.

**Правка 4 -- CROSS_MIN_NEIGHBOUR_COUNT, the same class of fix applied
a third time.** Confirmed live in real cross-check snapshots: a
would-be "confirmed" verdict was forming at `neighbour_listed_count=1`
-- Lush Bouquet, Moonlight + Seal Brown (neighbour 21.50 at 1 listing);
Voodoo Doll, Electrician + Ivory White (39.20 at 1 listing); Mood Pack,
Moon Power + Onyx Black (82.50 at 1 listing). A single seller's asking
price is not a market price -- the SAME bug already fixed for Portals'
own pair floor (`FLOOR_MIN_LISTED_COUNT`) and Tonnel's own model floor
(`TONNEL_MODEL_MIN_LISTED_COUNT`), now fixed a third time for the
cross-check neighbour. `CROSS_MIN_NEIGHBOUR_COUNT` (env, default 3):
below this many listings, the neighbour's price is NOT used in the
decision at all -- treated exactly like "no comparable neighbour" (send),
recorded as its own verdict `"neighbour_thin"` so the report can
distinguish "no comparable listing at all" from "a comparable listing
existed but wasn't trustworthy enough to act on".

**Правка 5 -- verdicts and accounting.** Five verdict values total:
`"sent_no_neighbour"`, `"sent_neighbour_higher"`,
`"skipped_neighbour_cheaper"`, `"neighbour_thin"`, `"error"` (a
neighbour-side query failure -- never blocks, same principle applied
everywhere else in this project: a Portals outage must not stop Tonnel
signals and vice versa). Only `"skipped_neighbour_cheaper"` is ever in
`cross_check.BLOCKING_VERDICTS` -- callers (`poller.py`/
`tonnel_poller.py`) check membership in that set, not a literal string
comparison scattered across call sites. Both pollers' run summaries
print one counter per verdict (names match the verdict strings exactly:
`sent_no_neighbour`, `sent_neighbour_higher`, `skipped_neighbour_cheaper`,
`neighbour_thin`, `error`/`cross_check_error`) -- replaces the old
`cross_confirmed`/`cross_worse`/`cross_no_data`/`cross_market_signals_sent`
names entirely, not additively.

**`report.py`**: the old, Portals-signal-only `_cross_market_block`
(which built `CrossMarketSignal`s and read the legacy
`tonnel_floor_snapshots` table's `confirmed`/`worse`/`no_data` verdicts)
is REMOVED entirely. `_cross_direction_block` (from the prior delivery)
is updated to the new five-verdict scheme, reading the direction-
agnostic `cross_check_snapshots` table for both directions, with
per-verdict percentage shares and up to 10 `skipped_neighbour_cheaper`
listings (worst gap first) per direction.

**Not removed, deliberately**: `db.record_tonnel_floor_snapshot()` and
the `tonnel_floor_snapshots` table stay in the schema (unused by any
production code path now, but still exercised by
`test_migrations.py`'s ON CONFLICT class-level regression test) --
removing a table/migration is out of scope for this delivery and not
worth the risk; it is simply idle going forward.

**Not verified live** (no live network access in this environment): the
spec's implicit expectation that a real run would show the new unified
header/counters in practice. Covered instead by `test_cross_check.py`
(all КАК ТЕСТИРОВАТЬ items 4-8, both directions), `test_notifier.py`
(header/keyboard unification, item 9's "no CrossMarketSignal/МЕЖБИРЖЕВОЙ
anywhere" source-tree check), `test_tonnel_poller.py` and `test_report.py`
(integration-level coverage for both pollers and the report block).

## Two-way cross-check: each marketplace verifies the other's signal, cross_check.py

**Closes the loop**: the earlier cross-check only ran Portals->Tonnel
(a Portals signal checked against Tonnel's own pair floor). This delivery
adds the reverse direction (a Tonnel signal checked against Portals' own
pair floor) through one shared mechanism, `cross_check.cross_check()`.

**Measured on 19 real signals from both marketplaces** (spec, verbatim):
PAIR-level (same collection+model+backdrop) comparison found a comparable
neighbour listing for 6/12 Portals signals and 5/7 Tonnel signals (~60%
coverage) -- workable. Per-LISTING comparison (checking whether the exact
same gift is separately listed on both marketplaces) was measured at
**0/19 and rejected outright** -- the same physical gift can never be for
sale on two marketplaces at once, so there is nothing to compare at that
granularity. Confirmed vertical case: the cross-check correctly
distinguishes signals WITHIN one pair (B-Day Candle, Crazy Frog + Onyx
Black -- Tonnel 49.50-with-fee; Portals lots at 47/48 confirm, lots at
56/57 get rejected as "worse" -- proving this isn't a blunt pair-level
gate but a real per-signal price comparison).

**`cross_check.py`** (new module) -- `cross_check(conn, signal, *,
portals_client=None, tonnel_client=None, now=None)` dispatches on
`signal.marketplace`:
- **Portals signal -> Tonnel neighbour** (the original direction, moved
  here out of `poller.py` unchanged): `tonnel_client.pair_floor(gift_name,
  model, backdrop, exclude_gift_num)`, compared against the BUYER price
  `tonnel_floor * 1.1` (Tonnel's confirmed 10% buyer fee).
- **Tonnel signal -> Portals neighbour** (new): `collection_id` is looked
  up by `collection_name` via the new `db.get_portals_collection_id_by_name()`
  (a Tonnel listing's own `collection_id` is always None -- Tonnel has no
  such field -- but collection/model/backdrop NAMES are confirmed to
  match across marketplaces, so this borrows whatever Portals
  `collection_id` we've already collected under the same name; no lookup
  possible -> "no_data", never blocking the Tonnel signal). Then
  `portals_client.search_pair_floor(collection_id, model_name,
  backdrop_name)` (sort=price_asc FIRST param, confirmed critical --
  see `portals_client.py`), parsed with `pair_floor.py`'s EXISTING
  `_floor_from_response()` (reused as-is, not reimplemented -- passing
  the Tonnel signal's own external_id as the "exclude" id is a no-op
  exclusion across marketplaces, just reusing that function's floor/
  status computation). **No buyer-side fee** -- Portals' listed price is
  what a buyer actually pays, confirmed, unlike Tonnel.

**Reuse, not duplication, per spec**: the verdict math itself
(`compute_cross_verdict`/`is_tonnel_implausible`, `signals.py`) was
ALREADY a pure, direction-agnostic price/status comparison before this
delivery -- it never cared which side was "the signal" and which was
"the neighbour". Zero changes needed there beyond swapping its ratio
threshold's source from the old `TONNEL_MAX_RATIO` to the new
`CROSS_MAX_RATIO` (same default, 5.0, now explicitly named for both
directions). Only the NETWORK fetch genuinely differs per direction
(two unrelated wire protocols) -- there is nothing left to share there.

**ВАЖНО ПРО ВАЛЮТЫ**: Portals prices are in GRAM, Tonnel in TON.
Confirmed 1:1 (GRAM is a renamed TON) -- `cross_check.py` names this
explicitly as `_RATE_GRAM_PER_TON = Decimal(1)`, a single named constant
at the top of the module, so a price comparison is never silently
treated as "obviously the same unit" without that assumption being
visible and fixable in one place if it's ever wrong.

**Verdicts** (`compute_cross_verdict`, unchanged logic): `price >=
neighbour_floor` -> `"worse"` (never sent, `alerts_sent` gets
`status="skipped_cross_worse"`); `price < neighbour_floor` -> `"confirmed"`
(sent with a checkmark, `notifier.py`'s existing `cross_verdict ==
"confirmed"` header logic, unchanged, already direction-agnostic); no
usable neighbour data OR the neighbour query itself failed -> `"no_data"`
(sent WITHOUT a checkmark, never blocks). `CROSS_MAX_RATIO` protects
against a lonely, arbitrarily-priced neighbour listing skewing a
would-be "confirmed" into "no_data" instead -- applies symmetrically in
both directions now (the mirror-image bug this project already knew
about from Portals' OWN floor computation, `FLOOR_MAX_RATIO_TO_PRICE`).

**Settings (Правка 3)**: `CROSS_CHECK_ENABLED` (env, default `true`,
REPLACES the earlier one-way `TONNEL_CROSS_CHECK_ENABLED` which defaulted
`false`) gates BOTH directions with one flag. `CROSS_MAX_RATIO` (env,
default `5.0`, replaces `TONNEL_MAX_RATIO`, same value, now explicitly
bidirectional). New table `cross_check_snapshots` (schema v16) --
`signal_marketplace`/`checked_marketplace` columns record which
direction each row is, so ONE table serves both without duplicating the
old `tonnel_floor_snapshots` schema per direction. The OLD
`tonnel_floor_snapshots` table is kept and still written to (by the
Portals->Tonnel direction only, for `report.py`'s pre-existing
Portals-only cross-market block) -- not migrated, since there is no way
to retroactively fill in `signal_marketplace`/`checked_marketplace` for
old rows. Stats counters renamed for both pollers: `tonnel_checks_confirmed`/
`_worse`/`_no_data` -> `cross_confirmed`/`cross_worse`/`cross_no_data`
(same meaning, direction-neutral names now that both pollers report
them); `tonnel_poller.py` gained a matching `signals_skipped_cross`
counter (poller.py already had one).

**`tonnel_poller.py`** gained a `portals_client` constructor param (None
by default, always safe to omit -- no network call happens at
construction). `build_default_poller()` constructs a real
`PortalsClient` (via the SAME shared `AuthManager()` credentials
`poller.py`'s own process uses -- Portals auth is not a Tonnel-specific
concept) whenever `CROSS_CHECK_ENABLED`, even though this process's
primary job is collecting Tonnel's own feed. `_maybe_cross_check()`
mirrors `poller.py`'s method of the same name -- both call the SAME
`cross_check.cross_check()`.

**`report.py`** gained `_cross_direction_block()` (Правка 4), printed
after the existing (unchanged) Portals-only cross-market block: reads
the new `cross_check_snapshots` table via `db.latest_cross_check_snapshots()`,
splits by `signal_marketplace`, and for EACH direction prints
signals-checked count, confirmed/worse/no_data with percentage shares,
and up to 10 rejected (`verdict="worse"`) listings with their overpay
ratio, sorted worst-first -- same shape as the existing block, just
doubled for both directions instead of assuming Portals is always the
signal side.

**Not verified live** (no live network access in this environment): the
30-minute two-poller concurrent run with nonzero `cross_*` counters in
both summaries and a real report showing both directions, called for by
the spec's SANITY-CHECK. Covered instead by `test_cross_check.py` (all 8
КАК ТЕСТИРОВАТЬ items, both directions, fee-only-on-one-side,
lonely-neighbour-ratio, failure-never-blocks, disabled-flag), plus
integration tests in `test_tonnel_poller.py` (the new `portals_client`
wiring end-to-end through `_maybe_notify`) and `test_report.py` (the new
report block's content).

## Two-writer SQLite contention: WAL + busy_timeout, db_locked_count

**Bug**: `sqlite3.OperationalError: database is locked` in
`db.touch_listing_lifecycle`, reproduced stably within minutes of running
`poller.py` (Portals) and `tonnel_poller.py` (Tonnel) against the same
`gift_sniper.db` file. Confirmed by symptom (Portals kept running, only
Tonnel kept failing) that Portals is the one holding the writer's seat
when Tonnel loses the race -- consistent with Portals' FAST PATH writing
far more frequently than Tonnel's feed (roughly double the rate). SQLite
allows many concurrent readers but only one writer; in the DEFAULT
rollback-journal mode, a writer that finds the file locked fails
INSTANTLY (zero grace period) -- this was tolerable while Tonnel barely
wrote at all, and stopped being tolerable once both pollers became
equally active.

**Fix 1 -- WAL mode**, `db.connect()`: `PRAGMA journal_mode = WAL` is now
run on every connection. WAL is a property of the DB FILE itself (not
the connection), so this is idempotent to re-run, and lets readers
proceed without blocking a concurrent writer (and vice versa) -- this is
the change that actually eliminates most of the contention, not just
papers over it with a longer wait.

**Fix 2 -- busy_timeout**, same place: `PRAGMA busy_timeout =
{SQLITE_BUSY_TIMEOUT_MS}`, new setting `config.SQLITE_BUSY_TIMEOUT_MS`
(env, default 15000/15s). A writer that still finds the file locked
under WAL (a genuinely overlapping write from the other poller) now
WAITS up to this long for the lock to free, instead of raising
immediately -- this is a wait budget for the residual case, explicitly
NOT a substitute for fixing long transactions (see Fix 3, and per spec:
"не... увеличивать таймаут вместо устранения долгих транзакций" -- this
is why the timeout is a moderate 15s, not something much larger).

**Fix 3 -- transaction-length audit (poller.py + tonnel_poller.py)**:
reviewed every `with conn:` block (all of them live in `db.py`, one per
function, each wrapping only the SQL statements themselves) and every
network call site in both poller files. **Neither poller.py nor
tonnel_poller.py manages a SQLite transaction directly at all** --
`grep`-confirmed zero `with conn:` / `with self.conn:` / `BEGIN`
occurrences in either file (see `test_no_conn_transaction_wraps_a_network_call`
in `test_sqlite_concurrency.py`, which pins this as a tripwire against a
future regression). Every write goes through a `db.py` helper, and every
one of those helpers' `with conn:` blocks is immediately closed around
the SQL, never spanning a network call. **No violation of "network
inside a transaction" was found to fix** -- the reproduced bug's root
cause is Fix 1/2's actual target (zero-grace-period locking under
rollback-journal mode with two active writers), not a long-held
transaction from either poller. This audit conclusion is recorded here
explicitly so it isn't re-litigated as "surely something must have been
missed" -- if a future change ever DOES wrap a network call in a
transaction, this test file's tripwire test will catch it.

**Fix 4 -- `db_locked_count`, not swallowed silently.** Even with Fix
1/2, a writer can still exhaust the 15s busy_timeout wait under heavy
contention (or after some future change makes writes slower). Previously
an `sqlite3.OperationalError` from anywhere inside a poll cycle would
propagate all the way out of `run_forever()` (only `KeyboardInterrupt`
was caught there) and kill the whole process -- exactly what "Tonnel
falls over" in the bug report describes. Both `Poller.run_forever()` and
`TonnelPoller.run_forever()` now catch `sqlite3.OperationalError` around
each poll cycle: if the message contains "database is locked", it's
counted in `stats["db_locked_count"]`, logged as an error, and the loop
continues to the next cycle (this cycle's work is simply skipped, not
retried mid-cycle -- the next iteration naturally retries the same
listings). Any OTHER `OperationalError` (e.g. a real schema bug) is
NOT treated as contention -- it still propagates and crashes the
process, exactly as before. `db_locked_count` is printed in both
pollers' run summaries -- watch it after this fix ships: it should stay
at (or very near) 0 if WAL actually resolved the contention; a nonzero
count under normal operation is the signal that contention persists and
needs further investigation, not a threshold to silently tune away.

**Not verified live** (no live network access in this environment): the
30-minute two-poller concurrent run against a real `gift_sniper.db` file
called for implicitly by the bug report. Covered instead by
`test_sqlite_concurrency.py` (WAL mode, busy_timeout value and behavior
under real two-connection contention on a temp file) and
`test_poller_db_locked.py`/`test_tonnel_poller.py`'s db_locked_count
tests (the crash-avoidance behavior, via a monkeypatched
`OperationalError` rather than a real lock race, since reliably
reproducing genuine contention in a fast unit test is not practical).

## Tonnel's own model-floor criterion, self-contained (not borrowed from Portals) -- pair floor retired

**Reverses the earlier "no model-level floor for Tonnel" verdict** (see
the "Full Tonnel signaller" entry directly below) -- that verdict was
reached by direct analogy with Portals, where model-level IS rejected.
**That reasoning does not transfer to Tonnel and must not be used to
revert this feature later.** The two situations are different:

- **Portals**: a real pair floor (same model, same backdrop) usually
  exists. Model-level there means falling back to a DIFFERENT backdrop's
  price within the same model -- a different item entirely, confirmed
  live to be fabricated/misleading (Durov's Glasses #3685: "floor" 115.00
  was another backdrop's price; real next offer was 94.00). Rejected for
  a good reason: a strictly worse fallback exists alongside a better one.
- **Tonnel**: there is no pair floor to fall back FROM. Confirmed live,
  20/20 significant Tonnel price drops spot-checked had **zero** pair
  floor whatsoever -- Tonnel's feed is roughly half Portals' rate, so a
  second listing of the exact same collection+model+backdrop essentially
  never coexists. Model-level (same collection+model, no backdrop) is not
  a worse alternative to a better option here -- **it is the only basis
  this project has any data for.** Collection-level floor was also ruled
  out (it reflects the single cheapest item in the whole collection, ~4-5
  TON, unrelated to specific lot prices of 20-150 TON) and drop-depth
  alone was ruled out as unanchored to any market price (two measured
  counter-examples: Lol Pop/Gold Star -86% drop to 7.00 but model floor
  3.99 at 30 listings = no real discount; Liberty Figure/Columbia -50%
  drop to 50.00 but model floor 5.00 = still 10x overpriced).

**`tonnel_client.py`**: new `model_floor(gift_name, model, exclude_gift_num)`
method -- same filtering discipline as the retired `pair_floor()` (drops
`underLoan`, non-`forsale`, self by `gift_num`) plus excludes `gift_id < 0`
(bundles). No `backdrop` in the query or the signature. Reports whatever
`listed_count` it measures with no threshold opinion of its own --
`status="ok"` if any usable listing exists, `"no_data"` otherwise; the
minimum-count POLICY is applied by the caller (see below), a deliberate
separation of fact-reporting (client) from policy (poller).

**`tonnel_poller.py`'s `pair_floor()` call is retired entirely** -- it is
never queried again (confirmed: always `"no_data"`, a wasted request per
the 20/20 measurement above). `_maybe_refresh_floor_snapshot()` now calls
`model_floor()` instead and writes to `floor_snapshots` via the renamed
`db.upsert_tonnel_model_floor_snapshot()` (was
`upsert_tonnel_pair_floor_snapshot`, columns `pair_*` -> `model_*`).

**New setting `TONNEL_MODEL_MIN_LISTED_COUNT` (default 5)**, in
`config.py`. Measured ratio of second-cheapest-price to floor-price by
listed count (Tonnel): 2 listings -> 4.64/1.87/2.86/1.63 (floor lonely,
unreliable); 3 -> 1.50/1.30; 4 -> 1.90/1.92; 5-6 -> 1.06/1.05/1.19 (prices
start converging); 8-11 -> ~1.00-1.28; 30 -> 1.08. Extreme case: Durov's
Glasses, Vampire Gaze had exactly 2 listings, 110 and 510 -- calling 110
"the floor" there would have been wrong. Applied in
`_maybe_refresh_floor_snapshot()`, NOT in the client: a real floor
backed by fewer than this many listings is written with
`model_floor_status="thin_model_book"` (a new status value, distinct
from `"ok"`/`"no_data"`/`"error"`), which `signals.py`'s existing
`model_floor_status == "ok"` gate already excludes -- no cascade change
needed for this part.

**`signals.py`**: Tonnel's `floor_level` is now always `"model"` --
`"pair"` is never used for Tonnel (there's nothing to populate it, since
`pair_floor_status` stays at its schema default `"no_data"` forever now).
This falls out of the EXISTING pair-then-model hierarchy in
`_floor_and_level()` with zero changes needed there. Two real bugs fixed
to make this actually work end-to-end:
1. `_signal_from_row()`'s profit computation was gated on `level == "pair"`
   for BOTH marketplaces -- since Tonnel signals are always level="model",
   every Tonnel signal's profit silently stayed `None`. Now gated as
   `(marketplace=="tonnel" and level=="model") or (marketplace!="tonnel"
   and level=="pair")` -- the Tonnel profit formula itself (`floor -
   price*1.1`) was already correct from the earlier delivery, just never
   reached.
2. A new mandatory cascade stage, **Tonnel-only**: price must be strictly
   below the model floor (`ratio > 1`, i.e. `floor/price > 1`) for a
   signal to form at all. Without it, a lot priced at or above its own
   model floor would still survive (the existing `FLOOR_MAX_RATIO_TO_PRICE`
   check only rejects ratios that are too HIGH, not ratios <= 1) -- this
   is `CascadeResult.no_discount`, applied only when `marketplace ==
   "tonnel"`. Portals' pair-level cascade doesn't have this stage; adding
   it there is out of scope for this delivery.
3. `tonnel_poller.py`'s `_maybe_notify()` was filtering candidates via
   `s.floor_level in config.NOTIFY_LEVELS`, and `NOTIFY_LEVELS` defaults
   to `{"pair"}` (a Portals-oriented setting, deliberately excluding
   Portals' rejected model level) -- every Tonnel signal would have been
   silently dropped by this shared gate, and the "obvious" fix of adding
   `"model"` to `NOTIFY_LEVELS` would have also wrongly re-enabled
   Portals' rejected model-level signals (one shared setting). Fixed by
   not applying this particular gate to Tonnel candidates at all --
   every Tonnel signal already IS the correct/only level for Tonnel.

**Explicitly out of scope for this delivery** (per spec): cross-marketplace
comparison of Tonnel signals against Portals data -- deferred as a future
layer, not attempted here.

**Not verified live in this delivery** (no live network access in this
environment): the 30-minute `TONNEL_NOTIFY_ENABLED=true` run and
`send_test_signals --marketplace tonnel` formatting check called for in
spec. All new/changed behavior is covered by unit and integration tests
instead (`test_tonnel_client.py`, `test_tonnel_poller.py`, `test_signals.py`).

## Full Tonnel signaller: notifications, cross-marketplace shared filter logic, deep links

**Confirmed live**: Tonnel's per-lot deep link is
`https://t.me/tonnel_network_bot/gift?startapp=<gift_id>` -- path
`/gift` (not `/market`), `startapp` carries the BARE `gift_id` with NO
prefix (Portals uses `gift_<uuid>`). Checked on two lots, including one
ALREADY SOLD -- the card opened either way. `gift_id` is already this
project's Tonnel `external_id`.

**Правка 1 — signals.py shared, not duplicated.** `run_cascade`,
`backfill_ladder`, `clean_signals`, `_signal_from_row` all gained a
`marketplace` parameter (default `"portals"`, unchanged behavior for
every existing caller). Thresholds are resolved per marketplace via a
new `_thresholds_for()` helper: `TONNEL_PRICE_DROP_MIN_PCT` (1.0),
`TONNEL_FLOOR_MIN_LISTED_COUNT` (3), `TONNEL_FLOOR_MAX_RATIO_TO_PRICE`
(4.0), `TONNEL_SIGNAL_COOLDOWN_MIN` (60) -- all copied from Portals'
own defaults as a STARTING POINT, **UNMEASURED for Tonnel** (its feed
is ~120 new listings/hour total, roughly half Portals' measured rate --
a reasonable guess, not a confirmed-correct one). **Bug fixed in the
same pass**: `backfill_ladder`'s `UPDATE price_history SET is_ladder = 0`
had no `WHERE marketplace` clause -- calling it for either marketplace
would silently wipe the OTHER marketplace's already-computed ladder
flags (both pollers are separate processes against the same DB file).
Now scoped explicitly.

**Правка 2 — Tonnel's own pair floor, written where the cascade already
looks.** On a significant price drop (above `TONNEL_PRICE_DROP_MIN_PCT`),
`tonnel_poller.py` now queries `tonnel_client.pair_floor()` (already
built, self-excluded by `gift_num`) and writes it into `floor_snapshots`
with `marketplace='tonnel'` via new `db.upsert_tonnel_pair_floor_snapshot()`
-- the SAME table/columns `signals.py`'s cascade already reads for the
"snapshot" floor source (`pair_floor_excl_self_nano` /
`pair_listed_count_excl_self` / `pair_floor_status`), so **no cascade
code needed to change** to make Tonnel signals work at all. No
model-level floor for Tonnel (measured median pair/model ratio 2.78,
range 1-201 on 360 Portals records -- not a usable basis for a signal,
per spec) -- a pair with no data simply gets `status="no_data"`, and the
existing floor-no_data cascade stage already excludes it, same as
Portals. **Schema v15**: `floor_snapshots` and `alerts_sent` both widen
from their earlier single/double-column PRIMARY KEY to include
`marketplace` -- Tonnel genuinely writes to both now. Uses the exact
same `_ensure_*_marketplace_pk()` idempotent-rebuild pattern the
price_history/listing_lifecycle bugfix established (this is the THIRD
time this class of fix has been needed -- see the class-level migration
test in `test_migrations.py`, extended to cover these two tables too).

**Tonnel profit formula -- NOT Portals' formula, per spec, verbatim**:
`floor_pair * (1 - 0) - price * 1.1` -- buy at `price * 1.1` (confirmed
10% BUYER fee), sell at the pair floor with **no seller-side
deduction**. Documentation search found only the buyer-side fee; **no
Tonnel seller fee was found or measured** -- if one exists, this profit
number is currently too optimistic. Recorded here as an open question,
not a confirmed zero.

**Правка 3 — notification format.** Header is `"ЛИСТИНГ TONNEL"` for a
Tonnel signal (`"ЛИСТИНГ"` unaffected for Portals), currency `TON`. The
checkmark rule is **exactly the same code path**, unmodified
(`cross_verdict == "confirmed"`) -- in practice this means a
Tonnel-sourced signal currently NEVER shows the checkmark, since nothing
in this delivery cross-checks a Tonnel signal against a third source
(`cross_verdict` stays its `"not_checked"` default). This is the literal
reading of "по тем же правилам, что сейчас" (the SAME rule, not a
special case) -- flagged here so it isn't mistaken for an oversight.
Buy button: `"Купить на Tonnel"` -> the deep link above (`build_keyboard`
branches on `signal.marketplace`). The `t.me/nft/<tg_id>` gift-card link
on the FLOOR line is UNCHANGED and marketplace-agnostic -- it already
works for any `tg_id`. **Note**: the spec text called this a "невидимый
якорь" (invisible anchor), but the actually-established, twice-debugged
mechanism (see the earlier CLOSED QUESTION below) is a VISIBLE `·`
anchor -- reverting to zero-width would reintroduce a confirmed-broken
behavior, so the existing visible-anchor mechanism was kept as-is and
NOT changed back, per "что не нужно делать: копировать формулу Portals"
in spirit if not letter (the working thing was left working).

**Правка 4 — tonnel_poller.py sends notifications.** New
`TONNEL_NOTIFY_ENABLED` (default `false`) gates sending independently of
collection -- recipients (`TELEGRAM_OWNER_ID`/`TELEGRAM_VIEWER_IDS`) and
the bot token are SHARED with Portals, per spec. `tonnel_poller.py` runs
**no `CommandHandler`** -- `getUpdates`' offset is a single global
stream per bot token; two independent processes polling it
independently would be a real conflict, so `/status` and `/last` stay
exclusively `poller.py`'s. The pre-send freshness check reuses the
SAME minimal-filter mechanism lifecycle checking already established
(`search_minimal_by_gift_ids`) -- a sold/delisted lot is caught the same
way a lifecycle disappearance is. Cooldown uses
`TONNEL_SIGNAL_COOLDOWN_MIN` via `db.get_last_sent_alert_for_listing`
(no resend-after-drop exemption -- not asked for, kept simple).
`alerts_sent` gained a `marketplace` column (schema v15, see Правка 2) --
still exactly one row per SIGNAL, never per recipient, unaffected by the
owner/viewer broadcast.

**Правка 5 — `report.py --marketplace tonnel`.** `_price_drops_block()`
(the cascade + clean-signals presentation, already shared machinery)
gained a `marketplace` parameter -- SAME function, SAME output shape,
selects Tonnel's own thresholds and rows. Everything else in
`generate_report()` (name collisions, blocked-gift stats, the
api/own/pair three-way floor comparison) stays Portals-only and out of
scope: those concepts (`collection_floor_nano`, `unlocks_at`,
`api_combo_floor_nano`) have no Tonnel equivalent at all.
`send_test_signals.py --marketplace {portals,tonnel}` (default
`portals`) added the same way.

**SANITY-CHECK REQUIRED, NOT RUN IN THIS ENVIRONMENT** (no network
access here): live Tonnel signals have not been observed or sent. The
user should run `tonnel_poller.py` with `TONNEL_NOTIFY_ENABLED=true`
for a while and confirm: signals actually form (`price_drops_above_threshold`
> 0, `floor_snapshots` gaining `marketplace='tonnel'` rows), notifications
render correctly (`send_test_signals.py --marketplace tonnel` first, to
check formatting before enabling live sends), and the deep link
actually opens the right lot's card in Telegram. Validated here only
via the test suite (new tests across `test_signals.py`,
`test_tonnel_poller.py`, `test_notifier.py`, `test_send_test_signals.py`,
`test_migrations.py` -- 368 total passing).

## FIXED: Tonnel lifecycle false-positives -- BASE_FILTER was too strict for a mere liveness check; verdict on the sale-signal question REVISED

**Спот-проверка 12 лотов по gift_id, живыми запросами**: 8/12 were
still genuinely FOR SALE (`status="forsale"`, price intact) -- this
project was recording them as disappeared only because they'd aged out
of the freshness-sorted, depth-limited feed, never because they were
actually gone. 3/12 were absent under BOTH the full `BASE_FILTER` and a
minimal one -- genuinely not on the platform. 1/12
(`gift_id=10300285`) was found ONLY once `buyer`/`refunded` were
dropped from the filter, with `status="forsale"` and no `buyer`/
`refunded` fields in the response at all -- cause not established, one
case out of twelve.

**Правка 1 — lifecycle checks now use a MINIMAL filter, not
BASE_FILTER.** `TonnelClient.search_minimal_by_gift_ids()` (replaces the
retired `search_by_gift_ids()`) sends only `{"gift_id": {"$in": [...]}},
"asset": "TON"}` -- deliberately WITHOUT `price.$exists`, `refunded`,
`buyer`, `export_at`. Those four conditions are exactly what was
producing false disappearances for still-listed lots. A gift_id
genuinely absent even under this minimal filter is now treated as REAL
disappearance: `listing_lifecycle.disappeared_at` is set,
`final_status="gone_unknown"` (added to `db.DISAPPEARED_STATUSES`) --
unlike Portals, the reason (sold vs. delisted) genuinely cannot be
determined from this endpoint, so the field name says so honestly
rather than guessing. A found item's price is now also compared against
what's stored and written to `price_history` on a real change, exactly
like an ordinary feed sighting (`_maybe_record_price_change`, shared
between the feed path and the lifecycle-check path). A per-item fallback
query that itself FAILS (network/API error, not a confirmed absence)
still lands in `lifecycle_not_returned` -- an unverified state, never
conflated with a confirmed "gone."

**Правка 2 — bundles excluded.** A negative `gift_id` is a BUNDLE (per
Tonnel's own documented convention) -- its price covers the whole set,
never comparable to one gift's price. `tonnel_poller.py`'s `poll_once`
now drops `gift_id < 0` before dedup/collection, counted in
`items_bundles_skipped`. **`gift_sniper/tonnel_bundle_cleanup.py`**
(new, with `db.delete_tonnel_bundle_listings()`) removes the 6
already-collected bundle rows (measured live, before this filter
existed) from `listings`/`price_history`/`listing_lifecycle` -- the
user must run it once (`python -m gift_sniper.tonnel_bundle_cleanup
--db gift_sniper.db`) to clean up data collected before this delivery.

**Правка 3 — the prior "no positive sale signal, liquidity metric
unavailable" verdict is REVISED, not simply confirmed.** See the
now-`SUPERSEDED` block directly below (kept, not deleted, per spec).
**Corrected formulation**: a lot's disappearance from the platform IS
reliably captured by a targeted per-gift_id query using the MINIMAL
filter above (spot-check: 3/3 confirmed-absent lots were absent both
with and without `BASE_FILTER` -- the fact of absence is not in doubt).
What genuinely remains unknown, unlike Portals (whose explicit status
field distinguishes `withdrawn` from `unlisted`), is the REASON for the
disappearance -- sold vs. delisted cannot be told apart from this
endpoint. The earlier framing ("no positive signal at all") was itself
an artifact of checking with the wrong (too-strict) filter -- this
delivery's spot-check is what revealed that.

**SANITY-CHECK REQUIRED, NOT RUN IN THIS ENVIRONMENT** (no network
access here): a 30-minute `tonnel_poller.py` run -- `lifecycle_not_returned`
should be close to zero, `lifecycle_newly_gone` should be nonzero and
in the single/low-double digits (not hundreds) for a run of this length.
Validated here only via the test suite (16 new tests across
`test_tonnel_client.py`'s `search_minimal_by_gift_ids` cases,
`test_tonnel_poller.py`'s lifecycle/bundle cases, and
`test_tonnel_bundle_cleanup.py` -- 352 total passing).

## SUPERSEDED by the "FIXED" entry directly above -- Tonnel poller: server-side price filter (49% of traffic was wasted); lifecycle limitation CLOSED; 5xx counted separately

**Measured live, 10.4h run**: `pages_fetched: 14842`, `items_seen_total:
445260`, `collect_filtered_count: 218477` (49% of everything fetched,
discarded by OUR OWN price threshold), `items_already_known: 225768`,
`new_listings: 123` -- **one new listing per ~120 requests**. Half the
traffic was spent fetching rows this project immediately threw away.

**Правка 1 — moved server-side.** Confirmed live, two independent ways:
`filter: {"price": {"$gte": 15}}` and the top-level `"price_range":
[15, 100000]` both return HTTP 200 with every result actually priced
>= 15 (control: an unfiltered `sort={"price":1}` query shows real lots
as low as 3.85). `TonnelClient.search()` gained `min_price`, MERGED into
the existing `price` key so `BASE_FILTER`'s `{"$exists": true}` survives
alongside `{"$gte": ...}` -- confirmed by test that both conditions
land in the sent JSON, and that `BASE_FILTER` itself (a module-level
dict) is never mutated by one call and leaked into the next.
`tonnel_poller.py`'s `_search_page` now sends `TONNEL_COLLECT_MIN_PRICE`
on every feed request. The own-side `collect_filtered_count` check in
`_process_new_items` STAYS as a defensive backstop, explicitly
re-labeled: if it stays high after this change, that is itself the
signal that the server-side filter stopped being applied -- a
regression to notice, not something to silently tolerate.

**Правка 2 — Tonnel lifecycle limitation, CLOSED.** Measured live:
`lifecycle_newly_gone: 0` against `lifecycle_not_returned: 617` over the
same 10.4h run -- confirms what the previous delivery already suspected:
Tonnel's filtered search only ever returns what is CURRENTLY for sale,
and gives no positive "this lot is gone" signal the way Portals'
explicit status field does. Absence is correctly never treated as
disappearance (see `db.record_lifecycle_check_missing`). **Recorded here
as CLOSED so it is never revisited blindly**: a Tonnel-based liquidity
metric (median time-to-gone, the same measure `pair_liquidity_stats`
computes for Portals) is NOT achievable through this endpoint. One
narrow follow-up question remains genuinely open and is NOT answered in
this delivery (no live network access here): does dropping `{"buyer":
{"$exists": false}}` and `{"refunded": {"$ne": true}}` from the filter
reveal a sold/refunded lot -- i.e. a positive sale signal hiding behind
those two conditions? **`gift_sniper/tonnel_sold_lot_probe.py`** (new)
is the one-request script that answers this -- the user must run it
(`python -m gift_sniper.tonnel_sold_lot_probe --gift-id <a gift_id
already confirmed gone>`) and the result written back here: if the lot
IS found without those two conditions, this CLOSED verdict is reopened;
if NOT found, the limitation is confirmed for good and this becomes a
plain historical note.

**Правка 3 — `tonnel_5xx_count`.** Observed live once: `"unexpected
status 502"` with a Cloudflare HTML error page -- the poller's natural
per-iteration retry (next poll cycle) absorbed it and the run continued
unaffected. Counted separately from `tonnel_403_count`/`tonnel_429_count`
in `TonnelError.status_code` handling -- a platform-side outage is a
different signal from our own client behaving badly (getting rate
limited, or something wrong with the request shape), and conflating them
would hide which one is actually happening.

**SANITY-CHECK REQUIRED, NOT RUN IN THIS ENVIRONMENT** (no network
access here): a 20-minute `tonnel_poller.py` run -- `collect_filtered_count`
should be close to zero, `items_seen_total` should be noticeably lower
for the same `new_listings` count, and `pages_fetched` per new listing
should drop from the measured ~120. Plus `tonnel_sold_lot_probe.py` (see
above). Validated here only via the test suite (12 new tests across
`test_tonnel_client.py`'s `min_price` cases and `test_tonnel_poller.py`'s
`min_price`-forwarding/`tonnel_5xx_count` cases -- 344 total passing).

## FIXED: both pollers crashed on the first price change -- v13's price_history PK migration never actually ran in production

**Reported live**: `sqlite3.OperationalError: ON CONFLICT clause does
not match any PRIMARY KEY or UNIQUE constraint` in
`record_price_change`, on both `poller.py` and `tonnel_poller.py`, at
the very first price change either one tried to record.

**Root cause**: `price_history` has had a `marketplace` COLUMN since
`_migration_3_to_4` -- years before the schema-v13 delivery that
introduced the composite `(marketplace, listing_external_id,
observed_at)` PRIMARY KEY. That v13 migration's rebuild condition was
`if "marketplace" not in _table_columns(...)`. On any real DB migrated
sequentially from an early version, that condition was always **False**
(the column already existed), so the table-rebuild branch never ran --
`schema_version` recorded 13 as applied, but the PRIMARY KEY silently
stayed the old two-column shape. `record_price_change`'s `ON
CONFLICT(marketplace, listing_external_id, observed_at)` doesn't match
that key -- guaranteed failure on the very first write, for BOTH
marketplaces. **This is the third time a column-presence check missed a
real schema drift** (after `api_combo_floor_nano` and `report.py`
bypassing `db.connect()`) -- see the class-level test below, added
specifically because case-by-case fixes kept not being enough.

**Fixed two ways**, per spec ("почини саму миграцию, а не только её
последствия"):
1. `_migration_12_to_13`'s rebuild condition now checks the table's
   ACTUAL primary key (new `db._table_pk_columns()`, reading SQLite's
   own `PRAGMA table_info` `pk` field) instead of column presence --
   correct for anyone running the full migration chain from scratch
   today.
2. New **schema v14** (`_migration_13_to_14`, `CURRENT_SCHEMA_VERSION`
   now `14`) re-checks and, if still wrong, rebuilds `price_history`
   (and, defensively, `listing_lifecycle`) against their real PK -- this
   is what actually repairs an already-`schema_version=13` production
   DB, since the fixed v13 migration function itself will never run
   again on a DB that already recorded 13 as applied. Both migrations
   now share one idempotent, data-preserving rebuild helper
   (`_ensure_price_history_marketplace_pk` /
   `_ensure_listing_lifecycle_marketplace_pk`) instead of duplicating
   the rebuild SQL.

**`floor_snapshots` was audited too** (per spec, same class of
question): its `ON CONFLICT(listing_external_id)` matches its real PK
(`listing_external_id TEXT PRIMARY KEY`, unchanged since before this
project's Tonnel delivery, never widened) -- confirmed correct, no bug
there. `listings`' `ON CONFLICT(marketplace, external_id)` matches its
`UNIQUE(marketplace, external_id)` constraint, present since before the
migration system existed (migrations never `ALTER TABLE listings`) --
also confirmed correct.

**New class-level test**
(`test_full_migration_chain_from_v3_then_real_writes_succeed_for_every_on_conflict_table`,
`test_migrations.py`): builds a DB through the REAL migration chain
(v3 → `CURRENT_SCHEMA_VERSION`, not hand-built at the final shape) and
then calls every real `db.py` write function that uses `INSERT ...
ON CONFLICT` (`upsert_listing_with_floor`, `insert_listing`,
`record_price_change`, `mark_alert_sent`,
`record_tonnel_floor_snapshot`) with real data, including intentional
collisions and cross-marketplace writes -- this reproduces the exact
failure class (not just this one instance of it) and will catch it
again for ANY of these tables, immediately, the same way the real
poller hit it. A companion test
(`test_on_conflict_mismatch_is_actually_detectable_by_sqlite`) proves
the detection mechanism itself is real: an artificially-wrong `ON
CONFLICT` column list on the current schema raises the identical
`sqlite3.OperationalError`. Plus dedicated
`test_migration_13_to_14_*` tests reproducing the exact reported
production state (`schema_version=13`, `marketplace` column present,
old two-column PK) and confirming the fix, idempotency, and that an
already-correct table is left untouched.

## Multiple notification recipients: owner + read-only viewers

**Two roles, split from the previous single `TELEGRAM_USER_ID`.**
`TELEGRAM_OWNER_ID` (env, required when `NOTIFY_ENABLED=true`) receives
every notification and can run every bot command. `TELEGRAM_VIEWER_IDS`
(env, optional, comma-separated, default empty) also receive every
notification but can only run READ-ONLY commands (`/status`, `/last`) --
`CommandHandler.READONLY_COMMANDS`. `TELEGRAM_USER_ID` is kept as a
synonym for `TELEGRAM_OWNER_ID`: if only the old name is set, it's used
and a warning is logged (`config.get_telegram_owner_id()`).

**`TelegramNotifier`** now takes an optional `viewer_chat_ids` list.
`send_signal`/`send_cross_market_signal`/`send_text(chat_id=None)`
broadcast to the owner + every viewer via a new `_broadcast()` ->
`_send_to_chat()` pair -- each recipient is attempted independently; one
recipient's failure is logged and does NOT stop the rest (per spec). The
broadcast's return value is gated on the OWNER's send specifically
(`db.mark_alert_sent`/cooldown/retry logic in `poller.py` is unchanged,
still driven by one boolean). A 403 whose message contains "can't
initiate conversation with a user" (Telegram's own wording -- the common
real cause: the recipient never sent `/start` to the bot) gets its own
distinct log line with that exact hint, instead of blending into the
generic failure log.

**`alerts_sent` is unaffected** -- still exactly one row per signal
(`listing_external_id`, `observed_at`), never per recipient; cooldown
and anti-duplicate logic are unchanged, driven by the signal alone.

**Command replies now target the ASKING chat, not a broadcast.**
`TelegramNotifier.send_text(text, chat_id=...)`: passing `chat_id`
sends to exactly that one chat (used by every `CommandHandler` reply,
including "доступ ограничен" to a stranger); omitting it broadcasts (used
for the poller's own "...и ещё N сигналов" notice, which is
signal-volume information, same audience as the signals themselves).
Before this delivery `send_text` always targeted the single configured
chat, which was harmless with one recipient but would have been wrong
(broadcasting every command reply to everyone) once viewers exist.

**`CommandHandler`** takes `owner_id` + `viewer_ids` instead of a single
`user_id`. A viewer running a non-read-only command (`/start`, or
anything future that changes system behavior) is silently ignored --
same treatment as an unrecognized command -- NOT the "доступ ограничен"
reply, which is reserved for senders who are neither the owner nor a
viewer at all.

**`NOTIFY_MAX_PER_MINUTE` clarified, not changed.** It already counts
per SIGNAL (`select_signals_to_send`), and every signal becomes exactly
one message in each recipient's own chat regardless of recipient count
-- so this cap already equals the per-chat rate every individual
recipient sees, unaffected by N. What DOES scale with N is the total
Telegram API request count per check (relevant to the ~30/sec GLOBAL
limit, not the per-chat one) -- documented in config.py, no code change
needed since the existing per-signal cap was already the right
per-chat unit.

## ДОПОЛНЕНИЕ: Tonnel lifecycle batch format CONFIRMED live -- supersedes "format UNCONFIRMED" below

**Confirmed live**: `{"gift_num": {"$in": [...]}}` does NOT work --
HTTP 400, `{"error": "Invalid gift filter"}` (same for string-valued
gift_num). `{"gift_id": {"$in": [...]}}` DOES work -- HTTP 200, 4/4
requested items returned. `{"$or": [{"gift_num": n}, ...]}` also works
but is more verbose and grows with batch size. **`gift_id` is used**
(via the new `TonnelClient.search_by_gift_ids()`, replacing the retired
`search_by_gift_nums()`) -- it's also this project's Tonnel
`external_id` (`external_id = str(gift_id)`), so no extra `gift_number`
lookup is needed to map a `listing_lifecycle` row back to a query id.

**Fallback is now per-BATCH, not permanent.** A batch failure gets ONE
retry of the same call; only if that also fails does this specific
batch fall back to one `search_by_gift_ids([gift_id], limit=1)` call per
listing (counted in `lifecycle_batch_fallback_count`). The NEXT batch
always tries the batched call again -- the earlier permanent
mode-switch-on-first-failure design (this project's prior delivery) is
retired: it would throw away the ~30x request savings for the rest of a
run over a single transient error. `limit` is now always passed
explicitly, matching the batch size (`TONNEL_LIFECYCLE_BATCH_SIZE`, env,
default `30` -- the confirmed `pageGifts` ceiling) -- confirmed
elsewhere in this client that an implicit/default page size can silently
truncate a result set.

**A gift_id absent from the batch response is NEVER treated as
disappearance** -- counted in `lifecycle_not_returned` instead (same
discipline `poller.py` already applies to Portals' `search_by_ids`: an
absence could mean sold/withdrawn, or could simply mean the item fell
out of `BASE_FILTER` for an unrelated reason, e.g. now `underLoan`).
Found items get `record_lifecycle_status(..., "forsale", ...)`
(`last_checked_at` bump only); missing ones get
`record_lifecycle_check_missing(...)` (same). **This means nothing in
`tonnel_poller.py` currently sets `disappeared_at`/`lifecycle_newly_gone`
for a Tonnel listing** -- Tonnel's filtered search endpoint gives no
positive "this exact item is gone" signal the way Portals' explicit
status field does. `lifecycle_newly_gone` stays in the stats dict
(always 0 for now) for shape parity with Portals' summary -- a real
Tonnel disappearance signal, if one is found, is future work.

## ДОПОЛНЕНИЕ: Tonnel tg_id construction CONFIRMED live (8/8) -- supersedes "UNCONFIRMED" below

**Confirmed live on 8 Tonnel lots**: `<NameБезПробелов>-<gift_num>` finds
the correct `t.me/nft/<tg_id>` page every time --
`"Skull Flower" #8238 -> "SkullFlower-8238"`,
`"Vice Cream" #427292 -> "ViceCream-427292"`,
`"Desk Calendar" #175454 -> "DeskCalendar-175454"`, and 5 more. This
**supersedes** the "tg_id construction is UNCONFIRMED for Tonnel"
paragraph and the "user must run tonnel_tgid_probe.py before trusting
Tonnel tg_id links" requirement below -- both are no longer accurate.

**`tonnel_parsing.py`'s `_tg_id_for()`** now also strips apostrophes
(both `'` and the curly `’`), same treatment as spaces, matching the
Portals convention per spec -- none of the 8 confirmed examples happened
to contain one, so specifically the apostrophe-stripping piece is
applied by analogy, not independently verified by this measurement.

**`tonnel_tgid_probe.py`** is no longer a blocking prerequisite for
anything -- it remains in the repo as an optional spot-check tool (e.g.
after a fresh collection batch, or if an edge case like unusual
punctuation is suspected). It never fabricates or substitutes a tg_id on
a page-not-found result -- it only reports it read-only, per spec.

## Tonnel full collector (этап 1): its own feed, price history, lifecycle tracking -- separate process, shared schema

**Why**: on-demand pair cross-checking (the previous Tonnel delivery)
only ever covers 3-11% of signals -- a signal is a rare model+backdrop
combination, and that exact combination is almost never simultaneously
for sale on Tonnel. Falling back to a MODEL-level floor covers 88% but
its verdict is useless: measured on 360 Portals records, the
pair-floor/model-floor ratio has median 2.78, p75 6.71, p90 14.82, max
201 -- a several-fold gap between pair and model levels is NORMAL, not
evidence of overpaying, and the two can't be told apart from a model
floor alone. Tonnel has no public pair-floor endpoint of its own
(`filterStats`/`saleHistory` both require auth -- confirmed live,
`saleHistory` without a token returns `{"status":"error","message":
"Invalid auth data"}`). The only path to an accurate cross-check is
accumulating Tonnel's OWN listing history, exactly like this project
already does for Portals -- that's this delivery. **Signals and
cross-market verification on top of that history are этап 2**,
deliberately out of scope here until this collector is verified live.

**Schema v13 -- shared tables, `marketplace`-scoped.** `listings`
already had `marketplace` + `UNIQUE(marketplace, external_id)`.
`price_history` already had a `marketplace` column but its PRIMARY KEY
didn't include it -- widened to `(marketplace, listing_external_id,
observed_at)` (a full SQLite table rebuild, the standard pattern for a
PK change). `listing_lifecycle` gained a `marketplace` column and the
same PK widening -- Tonnel has its own disappearance tracking now (see
below). `floor_snapshots` gained a plain `marketplace` column (default
`'portals'`) but NOT a PK change -- Tonnel never writes there (see
below), so widening the key would be churn with no present benefit.
Every existing Portals code path that reads these tables (`own_floors.py`,
`report.py`'s `_fetch_joined`, the lifecycle functions in `db.py`) now
filters `marketplace = 'portals'` EXPLICITLY -- this matters for real:
collection/model/backdrop **names are confirmed to match** across the
two marketplaces, so an unfiltered query would have silently blended
Tonnel prices (a different currency and fee model) into a Portals floor
computation. `db.own_combo_floor()`/`db.pair_liquidity_stats()` both
default to `marketplace="portals"` for backward compatibility with every
existing Portals call site.

**`gift_sniper/tonnel_parsing.py`** (new): `parse_tonnel_listing()` maps
one Tonnel `pageGifts` item onto the SAME `Listing` dataclass Portals
uses (`marketplace="tonnel"`, `external_id=str(gift_id)`). Rarity is
split out of trait names (`"Fried Chicken (1.5%)"` ->
`model_name="Fried Chicken"`, `model_rarity_raw=Decimal("1.5")`) --
required for cross-marketplace matching later, since Portals stores
rarity separately. `collection_id` is always `None` (Tonnel has no such
concept). **`listed_at` is always `None`**: there is NO listing-time
field in the Tonnel response -- `export_at` is when the gift becomes
MINTABLE, not when it was listed for sale, confirmed NOT the same thing;
using it would be actively wrong, not just imprecise. `first_seen_at` is
the only timing signal available for a Tonnel listing, same limitation
this project already documents for Portals' unreliable `listed_at`.
`is_purchasable_tonnel_item()` filters `underLoan`/`premarketData`/
`auction`/`dutchAuctionData` items out BEFORE they ever reach the DB
(other trading mechanics, not an ordinary buy).

**tg_id construction -- SUPERSEDED, see the "ДОПОЛНЕНИЕ" section at the
top of this file: now CONFIRMED live (8/8).** The rule
(`<NameБезПробелов>-<gift_num>`) matches Portals' CONFIRMED rule.
**`gift_sniper/tonnel_tgid_probe.py`** (new) is the spot-check script
used for this confirmation -- read-only, GETs collected Tonnel listings'
pages and reports how many were actually found. No longer a blocking
prerequisite for anything (see the addendum above).

**`gift_sniper/tonnel_poller.py`** (new): a SEPARATE process
(`python -m gift_sniper.tonnel_poller`), explicitly NOT a subclass of
`poller.Poller` (per spec) but reusing the same shared `db.py`
functions with `marketplace="tonnel"`. Mirrors `poller.py`'s FAST PATH
(paginate the feed sorted by freshness, dedupe by `(marketplace,
external_id)`, record real price changes to `price_history`, touch
`listing_lifecycle` for every seen item) but has NO floor-analytics path
at all: Tonnel's pair floor is computed live via
`tonnel_client.pair_floor()` (already built, this project's earlier
Tonnel delivery), never stored per listing -- `db.insert_listing()` (new
function) writes ONLY to `listings`, no `floor_snapshots` row, since
there is nothing to fill one in later. Settings
(`TONNEL_POLL_INTERVAL_SEC=15`, `TONNEL_MAX_PAGES_PER_ITERATION=6`,
`TONNEL_COLLECT_MIN_PRICE=15`, `TONNEL_REQUEST_DELAY_MS=600`) are kept
fully separate from Portals' equivalents -- two independent processes
against unrelated APIs, no reason to couple their tuning.
`TonnelError` gained an optional `status_code` so 403/429 responses are
counted in their own run-summary stats separately from other failures
-- per spec, Tonnel showing no rate limit at measurement time doesn't
mean it never will under load.

**Lifecycle (disappearance) checking -- SUPERSEDED, see the
"ДОПОЛНЕНИЕ" section at the top of this file: batch format is now
CONFIRMED (`gift_id` $in, not `gift_num`), and the fallback is per-batch,
not permanent.** Batches up to `TONNEL_LIFECYCLE_BATCH_SIZE`
not-yet-disappeared Tonnel listings per pass.

**Правка 4 — `TONNEL_CROSS_CHECK_ENABLED` default flipped to `false`.**
The on-demand cross-check (previous delivery) almost always lands on
`no_data` without accumulated Tonnel history to compare against -- the
code is NOT removed, it will be reused once этап 2 gives it this
collector's data instead of a single live query per signal.

**`report.py --marketplace {portals,tonnel,all}`** (default `all`): a
new `=== listings by marketplace ===` block always shows both sources'
counts/date-range, regardless of the flag. Everything after it (name
collisions, price drops, cross-market verification) is Portals-only
content this delivery -- printed for `all`/`portals`, replaced with a
one-line note for `tonnel` (nothing Tonnel-specific exists there yet).

**SANITY-CHECK REQUIRED, NOT RUN IN THIS ENVIRONMENT** (no network
access here): a 20-minute `tonnel_poller.py` run -- `new_listings` and
`price_changes_seen` should be nonzero, `parse_errors` should be 0, and
there should be zero `tonnel_403_count`/`tonnel_429_count`. (tg_id
construction and the lifecycle batch format are already confirmed, see
the addenda at the top -- this remaining check is about collector
throughput/stability under sustained live load, not either of those.)
The user must run this before the collector is trusted for anything
beyond this delivery's own test coverage (test suite across
`test_tonnel_parsing.py`, `test_tonnel_poller.py`, `test_tonnel_client.py`'s
`search_by_gift_ids` cases, `test_migrations.py`'s v12->v13 tests, and
`test_report.py`'s `--marketplace` cases -- 318 total passing).

## Tonnel cross-check enabled in the real send path -- measured on 30 live signals: filters out ~36% of the checkable ones

**Измерено на 30 живых pair-сигналах**: Tonnel had a comparable pair
for 11/30 (~37%, matching the previously-observed ~40% pair-overlap
rate). Of those 11: 7 confirmed, 4 filtered out (worse) -- **36% of the
checkable signals were false positives that would have been overpaying
purchases**. One of the four filtered cases (Victory Medal, Dunk Master
+ Onyx Black -- Portals 17.42 vs. Tonnel-with-fee 11.44, a 52% overpay)
had ALREADY been sent to the user in a prior delivery, before this
cross-check existed.

**Правка 1 — enabled by default.** `TONNEL_CROSS_CHECK_ENABLED` was
already defaulting to `"true"` in `config.py` from the original Tonnel
delivery -- no code change needed here, just confirmed and documented.
Every clean signal is cross-checked before sending; cost is one Tonnel
request per signal (2-3/hour at current volume, no rate limit observed).

**Правка 2 — verdict now gates the send.** `poller.py`'s `_maybe_notify`
loop: `cross_verdict == "worse"` -> the signal is **not sent at all**,
recorded in `alerts_sent` with `status="skipped_cross_worse"`, counted
in the new `signals_skipped_cross` stat (printed in the run summary).
`"confirmed"` sends normally. `"no_data"` (no comparable Tonnel lot, or
the Tonnel request itself failed) still sends -- absence of a
counter-signal is not evidence against the signal, and a Tonnel-side
outage must never silently stop all Portals signals. A `TonnelError` is
caught, logged, and treated exactly like `"no_data"` -- never blocks the
send.

**Правка 3 — TONNEL_MAX_RATIO (schema v12).** Confirmed live: Liberty
Figure, Obsidian + Neon Blue -- Portals 27.11, Tonnel 330.00 (ratio
~12.2, a single arbitrarily-priced lot). `signals.is_tonnel_implausible()`
downgrades `"confirmed"` to `"no_data"` whenever
`tonnel_floor_with_fee / portals_price > TONNEL_MAX_RATIO` (env, default
`5.0`, **unmeasured** -- a deliberately generous starting point, to be
tightened once more implausible cases accumulate). This is the mirror
image of the already-handled Portals bug where a floor built from one
bad lot gets treated as real (`FLOOR_MAX_RATIO_TO_PRICE`). The flag is
recorded on the snapshot as `tonnel_floor_snapshots.tonnel_implausible`
(new column, migration `_migration_11_to_12`) independently of
`cross_verdict`, so `report.py` can tell "no comparable Tonnel lot" apart
from "Tonnel had a lot but it was a nonsense price."

**Правка 4 — user-visible change: the checkmark now means something.**
The TONNEL line stays removed (prior delivery). What changed: the
`"ЛИСТИНГ"` header now gets its `"✓"` **only** when
`cross_verdict == "confirmed"` -- every other case (cross-check
disabled, no comparable Tonnel pair, the check failed, or an
implausible-ratio downgrade) shows a plain `"ЛИСТИНГ"` with no
checkmark. Previously the checkmark was unconditional and therefore
meaningless. Nothing else was added to the notification -- the user
still isn't shown any Tonnel numbers, only whether Tonnel independently
confirmed the deal.

**Правка 5 — report.py's cross-market block** now also prints: a
`no_data share` percentage, a `skipped by TONNEL_MAX_RATIO (...)` count,
and up to 10 `"worse"`-verdict signals that were never sent (collection,
model, backdrop, Portals price, Portals floor, Tonnel price-with-fee,
and the overpay ratio `"переплата x..."`), sorted by overpay ratio
descending. This block is what the sanity-check run judges the
cross-check by.

**Not yet re-verified live in this delivery** (no network access here)
-- the change was validated against the 30-signal measurement already
supplied and the existing/new test suite (272 tests). The required
sanity check is a live 2-hour poller run: `signals_skipped_cross` should
be nonzero if any signal in that window had a comparable Tonnel pair,
and `report.py`'s cross-market block should show a real
confirmed/worse/no_data breakdown -- the user runs this.

## CLOSED QUESTION: warm-up GET before sending does NOT improve preview reliability -- do not implement in the send path

**Measured live, 40 messages, 20/20 split**: group A (no warm-up) sent
20/20, group B (warm-up `GET https://t.me/nft/<tg_id>` before send) sent
20/20. The gift-card preview was missing on 2 messages total, distributed
across BOTH groups (not concentrated in A). A pre-send fetch of the
preview page has no measurable effect on whether Telegram's client
renders the card.

**Verdict, recorded here so this is never re-investigated**: warm-up is
NOT implemented in the real send path (`poller.py`) -- the hypothesis it
was testing is rejected by direct measurement.
`gift_sniper/preview_ab_test.py` stays in the repo as a standalone
diagnostic tool but is never called from the poll loop or any other
production code path.

**Combined with the already-recorded facts**: the link itself is built
and recognized correctly (confirmed by the Bot API's own response --
`ok=true`, `link_preview_options`, and a correct `text_link` entity, on
every send in every prior measurement) and the anchor is on a visible
character, not zero-width (see the CLOSED QUESTION below). The
remaining ~5% miss rate is Telegram CLIENT-side unfurl behavior --
nothing observed on the sending side (message construction, link
placement, or a warm-up fetch) has any measured effect on it, and there
is no further sender-side lever to pull here.

## TONNEL line removed from notifications; preview_ab_test.py measures whether a warm-up GET helps

**`format_caption` no longer shows the Tonnel cross-check result.** Per
spec: "пользователю приходит только: заголовок, название, трейты, PRICE,
FLOOR со ссылкой-якорем" -- the algorithm still needs the comparison to
filter false signals, the user does not need to interpret it. The
message is back to exactly PRICE/FLOOR as the last two content lines
(FLOOR carrying the visible "·" link anchor).

**`cross_verdict` is NOT removed from the pipeline** -- it is still
computed in `poller.py`'s `_maybe_cross_check`, still recorded in
`tonnel_floor_snapshots` (schema v11), and `report.py`'s "=== cross-market
verification (Tonnel) ===" block still prints it. Only the
user-facing notification text changed. The `cross_market` ("МЕЖБИРЖЕВОЙ")
signal type is unaffected -- there, the Tonnel price is the entire point
of the message.

**`gift_sniper/preview_ab_test.py`** added: a measurement-only script (no
change to the real send path in `poller.py`). Confirmed by response
logging on all 5 test signals that Telegram already returns `ok=true`
with a correctly-formed `link_preview_options`/`text_link` entity for
every send -- our message construction is not the defect. The unmeasured
hypothesis under test: Telegram's client fetches the preview
asynchronously, after receiving the message, and a cold/slow
`t.me/nft/<tg_id>` response at that moment may be why a card is
sometimes missing. The script takes N clean signals (via
`signals.clean_signals()`, same selection as `send_test_signals.py`),
splits them deterministically by index parity (even → group A, sent as
today; odd → group B, one `GET https://t.me/nft/<tg_id>` awaited before
sending), 3s between any two sends, each message prefixed with
`"[A] <n>"`/`"[B] <n>"` for manual counting afterward. Writes nothing to
`alerts_sent`, runs no freshness check — a delivery test, not a trading
decision. **Run live by the user**: 40 messages, 20/20 split, result
above -- see the CLOSED QUESTION at the top of this file. Warm-up was
NOT implemented in `poller.py`; the script itself remains available as a
standalone diagnostic, not called from any production code path.

## CLOSED QUESTION: zero-width link anchor does NOT unfurl reliably -- never revert to it

**Confirmed live TWICE, independently**: a `t.me/nft/<tg_id>` link placed
on a zero-width anchor (a trailing space with no visible glyph, e.g.
`<a href="..."> </a>`) does not reliably trigger Telegram's link-preview
unfurl -- the exact same message, link, and page `og:` tags sometimes
show a preview and sometimes don't.
- First occurrence: Diamond Ring #23673 -- switching the anchor to a
  VISIBLE character fixed it; a later delivery reverted to zero-width
  (on the theory that it "shouldn't matter" and kept the title
  non-blue), which broke it again.
- Second occurrence: Fine Pen #11525 arrived with no preview even though
  `t.me/nft/FinePen-11525`'s own page source had correct `og:title`/
  `og:image`/`og:description`, and the identical link unfurled fine when
  pasted directly into Saved Messages -- ruling out the page/tg_id as the
  cause and confirming the anchor's invisibility as the variable.

**Fix (this delivery)**: the link is now attached to a VISIBLE `·`
character appended inline at the end of the FLOOR line (`FLOOR: 29.00
GRAM <a href="...">·</a>`) -- not the title (stays plain, non-blue text,
per spec), and not a separate line (nothing looks like an inserted
element). When `tg_id` is `None`, FLOOR stays a plain line with no link
at all.

**Verdict, recorded here so this is never re-investigated**: a zero-width
anchor for the preview-triggering link is NOT an acceptable
implementation, confirmed unreliable on two separate real signals. Any
future link placement must use a visible character.

## Tonnel Market added as a second price source (Этап 1: client + pair floor + cross-check)

**`gift_sniper/tonnel_client.py`** added: a standalone client for Tonnel
Market (`POST https://gifts2.tonnel.network/api/pageGifts`), confirmed
live to need no auth (`user_auth=""` → 200) and `curl_cffi` with
`impersonate="chrome"` (plain `requests`/`httpx` are rejected on the TLS
fingerprint). Deliberately does **not** subclass or reuse
`portals_client.py` — the protocols are unrelated (Tonnel's `sort`/`filter`
are JSON-encoded **strings**, not nested objects; no ordered-query-param
discipline; a completely different fee model, see below). `MAX_LIMIT = 30`
is enforced client-side (confirmed live: `limit=50` → `{"error": "limit is
too big"}`). `model`/`backdrop` filters use an anchored prefix regex
(`re.escape()`'d) because rarity is baked into the stored name (e.g.
`"Fried Chicken (1.5%)"`) and isn't known ahead of a query.

**Fee models confirmed to differ and must never be compared without
conversion**: Tonnel adds a 10% BUYER fee (`price * 1.1` is what a buyer
actually pays); Portals charges a 2% SELLER fee
(`config.MARKETPLACE_FEE_RATE`, applied at sale). GRAM (Portals) and TON
(Tonnel) are the same network/token, 1:1 — confirmed, not assumed — but
stored/compared as explicitly separate fields, never merged.

**Schema v11**: new `tonnel_floor_snapshots` table (one row per
cross-check, keyed on `(listing_external_id, fetched_at)` since a listing
can be checked more than once over time — this is intentionally not a
`REPLACE`-style single-row-per-listing table).

**Poller integration (`Poller._maybe_cross_check`)**: after all existing
filter/cooldown/freshness gates, but before `send_signal`, the poller
queries Tonnel's self-excluded pair floor for the same signal's
(collection, model, backdrop), records a snapshot, and mutates the
`Signal` with `tonnel_floor_nano`, `tonnel_floor_with_fee_nano`,
`tonnel_listed_count`, `tonnel_status`, `cross_verdict`
(`"confirmed"`/`"worse"`/`"no_data"`). A Tonnel-side failure
(`TonnelError`) never blocks the underlying Portals send — it only leaves
the signal at `tonnel_status="error"`, `cross_verdict="no_data"`, exactly
like a Portals freshness-check failure never blocks a send.
`config.TONNEL_CROSS_CHECK_ENABLED` (default `true`) gates the whole
check; when `false`, `TonnelClient` is never called at all (verified with
a mock that raises `AssertionError` if invoked).

**Cross-market signal (`signals.CrossMarketSignal` / `build_cross_market_signal`)**:
a genuinely separate signal type (not a flag on `Signal` — its shape,
buy-here/sell-there + gap% + profit, doesn't map onto `Signal`'s
price-drop-specific fields), formed when Tonnel's floor-with-fee is
below the Portals price by at least `config.CROSS_MARKET_MIN_GAP_PCT`
(default 15%, an unmeasured but deliberately conservative threshold below
the one confirmed live example: Input Key, Gold Star + Amber — Portals
83.00, Tonnel 60.00/66.00-with-fee, a 20% gap). Sent via
`TelegramNotifier.send_cross_market_signal`, header `"МЕЖБИРЖЕВОЙ"`
instead of `"ЛИСТИНГ"`, explicit "КУПИТЬ на Tonnel" / "ПРОДАТЬ на
Portals" lines. No Tonnel-side deep link is offered — no Tonnel per-lot
URL format has been confirmed live, so nothing unconfirmed is
hardcoded.

**Notification format (`format_caption`)**: a `TONNEL: ...` line was
added after `FLOOR:` — `"TONNEL: 66.00 TON (с комиссией) · подтверждено"`
when checked and confirmed, `"... · дешевле на Tonnel"` when checked but
worse (Tonnel is cheaper), `"TONNEL: нет данных"` otherwise. Existing
`format_caption` tests were updated for the extra line (the tests
asserting FLOOR was the last visible line now assert TONNEL is).

**`gift_sniper/report.py`** gained a `=== cross-market verification
(Tonnel) ===` block: count of signals checked, confirmed/worse/no_data
breakdown, and up to 10 cross_market opportunities sorted by gap% —
built entirely offline from `tonnel_floor_snapshots` (latest snapshot per
listing) joined against `signals.run_cascade()`'s clean signals, no
network call.

**`gift_sniper/tonnel_verify.py`** added: the required sanity-check
script — takes the 10 most recent clean signals (via
`signals.clean_signals()`, same selection as `send_test_signals.py`),
queries Tonnel's pair floor for each, prints a table (collection, model,
backdrop, Portals price, Portals floor, Tonnel floor-with-fee, verdict).
Read-only, writes nothing to the DB.

**Explicitly deferred, per spec** ("этап 2"): Tonnel feed/history
collection, Tonnel auth, Tonnel sales history.

**Not yet verified live in this delivery** (no network access in this
environment) — `tonnel_client.py` was tested only against a mocked
`curl_cffi`-shaped session (13 unit tests covering body format, limit
cap, regex escaping including the `"Durov's Cap"` apostrophe case,
`underLoan`/self-exclusion/`premarketData` filtering, throttling, and
error handling) and the poller integration was tested only against a
`FakeTonnelClient` stub (confirmed/worse/error/disabled-flag/cross-market
paths, 5 tests). The user must run
`python -m gift_sniper.tonnel_verify --db gift_sniper.db` against the
real Tonnel API and confirm the printed table looks sane (floors are
plausible numbers, verdicts match visual inspection of a couple of
rows) before relying on `TONNEL_CROSS_CHECK_ENABLED=true` in production.

## Gift card confirmed page-source facts; prefer_small_media removed; send_test_signals.py added

**Confirmed by direct inspection of `https://t.me/nft/<tg_id>`'s page
source** (not a guess): `twitter:card = summary` (the page itself is
already asking for a COMPACT card -- Telegram's client ignores this for
NFT links specifically), `og:image` is a plain JPG on `cdn4.telesco.pe`,
and there is NO `og:video` tag at all. This reinforces the existing
closed-question verdict below: preview size and media type are decided
entirely by the Telegram CLIENT for this specific link category, not by
anything in the page's own metadata or anything a bot can send. No
further action on preview size -- see the "CLOSED QUESTION" entry.

**`prefer_small_media` removed** from `link_preview_options` (confirmed
to have no effect on gift-card previews, and risked interfering with the
unfurl firing at all) -- only `is_disabled: false` and
`show_above_text: false` remain.

**`gift_sniper/send_test_signals.py`** added: sends the N most recent
clean signals (via `signals.clean_signals()` -- the exact function the
poller uses, not a separate query) to Telegram for a manual formatting/
rendering check, without waiting for a real signal to occur. Every
message gets a trailing "— тестовая отправка" line
(`format_caption(..., test_send=True)`). Deliberately does NOT: write to
`alerts_sent` (so it can never affect `SIGNAL_COOLDOWN_MIN` or real-bot
dedup), or run the pre-send freshness check (this tests formatting/
delivery, not tradeability). If fewer clean signals exist than
requested, sends what's available and says so.

**Not yet verified live in this delivery** (no network access in this
environment) -- the user must run `send_test_signals.py` and confirm the
gift card now unfurls reliably with the link after FLOOR (not on the
title), and that a missing `tg_id` still sends without error.

## CLOSED QUESTION: compact animated gift-card preview is not achievable -- do not revisit

**Дефект 3, confirmed live**: the compact-animation experiment from the
immediately preceding delivery (sendAnimation -> sendSticker ->
sendDocument -> link-preview fallback) reached its LAST step in
practice: Telegram rejected the gift's own `.lottie.json` animation file
as both `sendAnimation` and `sendSticker` content, and `sendDocument`
delivered it as a raw ~340KB **FILE** into the chat
(`khabibspapakha-5245.lottie.json`) -- a real, user-visible defect (a
file dump, not a card of any kind), not an acceptable degraded
fallback.

**Verdict, recorded here so this is never re-investigated**:
- A compact ANIMATED gift-card preview cannot be produced by this bot.
  `.lottie.json` renders ONLY inside Telegram's own native gift-card UI
  (the one seen when opening a gift directly in Telegram/the Portals
  mini-app) -- there is no Bot API call that invokes that renderer for
  an arbitrary file sent by a bot.
- Preview SIZE for a `t.me/nft/<tg_id>` link-preview unfurl is controlled
  entirely by Telegram, confirmed inconsistent even for the identical
  link sent multiple times (small sometimes, large other times).
  `link_preview_options.prefer_small_media` has no observed effect on
  gift-card previews specifically (tried in the delivery immediately
  before this one).
- `sendAnimation`, `sendSticker`, and `sendDocument` are REMOVED from
  `notifier.py` entirely (not merely unused/dead code -- deleted), so a
  raw file can never be sent to the user again under any code path.
  `TelegramNotifier.send_signal()` now ALWAYS uses `sendMessage`, with
  the `t.me/nft/<tg_id>` link present only as a zero-width anchor on a
  trailing space (never on the title -- the title stays plain, non-blue
  text, per spec) so Telegram's own preview unfurl is the only thing
  that can produce a card at all, with whatever size Telegram chooses.
- **Do not attempt further attachment-format guessing for the gift
  animation.** The three plausible Bot API calls for delivering a
  Telegram-hosted animation/sticker/document were all tried and all
  failed in the ways described above; there is no fourth reasonable
  guess -- accept the link-preview's inconsistent sizing as a platform
  limitation.

## is_noise defect investigated -- NOT reproducible from repo code; historical currency rows fixed

**Дефект 1 investigated, not a code bug found.** Measured live: over 4h,
48827 drops recorded, 43072 (88%) came back `is_noise=0` (significant),
against a historical baseline of 482/11850 (4%) on the same
`PRICE_DROP_MIN_PCT=1.0`. Reviewed every candidate cause listed in the
task (formula, wrong field, threshold not read from config): `poller.py`
computes `is_noise = abs(delta_pct) < config.PRICE_DROP_MIN_PCT` in
`_process_known_items`, `delta_pct = (new-old)/old*100` is the only
place it's computed, and `db.record_price_change`'s positional
parameters line up exactly with the call site -- there is exactly ONE
write path to `price_history.is_noise` in the entire codebase (checked
via grep). This exact formula is already pinned by two pre-existing
tests -- `test_small_drop_below_threshold_is_flagged_noise` (0.16% ->
`is_noise=1`) and `test_real_drop_above_threshold_is_not_noise` (2.92%
-> `is_noise=0`) -- both of which pass against the current code, and two
more were added this delivery for direct traceability to this task's
exact numbers. **The measured 88% figure did not reproduce from
anything in this repository.** This is the same failure signature as an
earlier delivery's pair_floor.py sync investigation: strong circumstantial
evidence that the LIVE poller process producing that 88% figure was
running CODE OLDER than what's in this repo (not restarted after a
previous fix), not that the current code has a bug. New
`gift_sniper/noise_diagnostic.py` (read-only) prints exactly the two
numbers the task asked for -- the last-1h significant-fraction sanity
check and the last-24h `|delta_pct|` bucket breakdown for `is_noise=0`
rows -- so this can be confirmed directly against the real DB rather
than guessed at again. **Action for the user**: restart the deployed
poller process onto this repo's current code, then run
`python -m gift_sniper.noise_diagnostic --db <path>` and compare against
the numbers above; if the 88% figure persists even after a confirmed
restart, that would be a genuine new finding worth a fresh investigation
(at that point the "process is stale" theory would be ruled out).

**Дефект 2 fixed**: `gift_sniper/fix_currency.py`
(`db.fix_listings_currency()`) updates `listings.currency` to
`config.CURRENCY_DEFAULT` on every row where it currently differs (11925
`'TON'` rows measured live, against 1088 correct `'GRAM'` ones, from
before `CURRENCY_DEFAULT`'s own default was corrected in the immediately
preceding delivery). Idempotent -- a second run updates 0 rows. Never
touches any column but `currency`, never deletes rows.

**Not yet verified live in this delivery** (no network/DB access in this
environment) -- the user must run `noise_diagnostic.py` before and after
confirming the poller process is on current code, and run
`fix_currency.py` once against the real DB.

## Notification format tightened further; sendAnimation/sendSticker/sendDocument attempt chain; CURRENCY_DEFAULT bug fixed

**Правка 1 -- final compact text format**: retired the title-as-link
trick from the previous delivery (title is plain `<b>` text again, no
blue color) and merged the two price lines into `PRICE:` / `FLOOR:`
consecutive lines (no blank line between them). Exact structure now:

```
ЛИСТИНГ ✓

<title>
<traits>

PRICE: <price> <currency>
FLOOR: <floor> <currency>
```

5 content lines + 2 blank separators, nothing more, nothing after
`FLOOR:`.

**Found and fixed in passing: `CURRENCY_DEFAULT` was wrong**. Its env
default was `"TON"`, but `/market/config` confirms this marketplace
prices in GRAM -- `parsing.py` always falls back to `CURRENCY_DEFAULT`
(the `/nfts/search` response carries no currency field of its own), so
every listing's currency was being silently mislabeled "TON" the whole
time. Caught because a live notification showed a GRAM-denominated price
tagged "TON". Default corrected to `"GRAM"`.

**SUPERSEDED by the "CLOSED QUESTION" entry at the top of this file --
the experiment below concluded with a negative result (Дефект 3:
sendDocument delivered a raw file to the user) and
sendAnimation/sendSticker/sendDocument were removed from the codebase
entirely.** Kept for history only.

**Правка 2 -- compact-animation experiment.** Confirmed live: Telegram's
own link-preview sizing for `t.me/nft/` links is inconsistent -- the
SAME link was observed unfurling both small and large across different
sends, and `prefer_small_media` (added in the immediately preceding
delivery) had no observed effect specifically on gift-card previews.
`TelegramNotifier.send_signal()` now tries, in this order, stopping at
the first success:

1. **`sendAnimation`** with `animation = listings.animation_url` (the
   `.lottie.json` file Telegram's own client uses for the gift).
2. **`sendSticker`** with the same URL -- `.lottie.json` is a STICKER
   format, not a video/animation one, so `sendAnimation` may reject it
   outright; `sendSticker` is the more likely-correct API call for the
   same file. (Sticker messages cannot carry `caption`/`parse_mode`/
   `reply_markup`, so those fields are omitted for this method only.)
3. **`sendDocument`** with the same URL -- last-resort attachment call.
4. **`sendMessage`** with the pre-existing link-preview approach
   (`format_caption(..., invisible_link=True)`: a zero-width link on a
   trailing space, NEVER on the title) -- falls all the way back to
   relying on Telegram's own unreliable-sizing preview. This step alone
   runs with no `animation_url` at all; steps 1-3 are skipped entirely
   (never attempted with an empty/missing file) when it's absent.

**NOT independently verified live in this delivery (no network access
in this environment)** -- the ordering above is what SHIPS, but which
of these four actually produces a compact result (or whether Telegram
rejects `.lottie.json` at every attachment step and it always falls
through to step 4) has not been confirmed against the real Bot API. The
user must run this live and report which step actually succeeds and
whether the resulting preview is visibly more compact than the
plain-link approach -- record the outcome here once checked, per spec,
so this isn't re-investigated in a future delivery. If it turns out
Telegram rejects all three attachment methods, that's the ANSWER (not a
bug in this code) and step 4 already exists as the graceful fallback
-- no further attachment-format guessing should be attempted beyond
these three.

## Notification polish: clickable title instead of a link line; no trailing blank line; link_preview_options

**Правка 1**: the previous fix for the preview-unfurl defect (see entry
below) added a visible `·` line at the end of the message purely to
carry the `t.me/nft/<tg_id>` link. That line had no other purpose, so
it's now retired -- the LINK MOVED onto the title itself:
`<b><a href="...">Astral Shard #1467</a></b>`. The title is clickable,
the link is genuinely visible (not the original zero-width-anchor
defect), and there's no extra line the user has no reason to read. If
`tg_id` is missing, the title is left as plain, unlinked `<b>` text --
never raises, just sends without a preview.

**Правка 2**: with the link line gone, `FLOOR: <value>` is now the LAST
line of the message -- no trailing blank line before wherever Telegram
renders the gift-card preview.

**Правка 3**: `TelegramNotifier.send_signal()` now sends
`link_preview_options` (Bot API 7.0+: `{"is_disabled": false,
"prefer_small_media": true, "show_above_text": false}`) instead of the
deprecated `disable_web_page_preview` boolean. `prefer_small_media` is a
HINT, not a guarantee -- Telegram, not the sender, controls actual
preview rendering size. **Not yet verified live in this delivery** (no
network access in this environment) whether Telegram actually honors
the hint and renders a visibly smaller card; if a live run shows no
size difference, that's a platform limitation, not a bug in this code --
record the observed outcome here once checked.

## Two live-notification defects fixed: unreliable preview unfurl, repeat notifications for one listing

**Дефект 1 -- preview card not unfurling, confirmed live**: Diamond Ring
#23673's notification arrived with no gift card, while a neighboring
one (Cupid Charm #8599) unfurled normally. Root cause: the
`t.me/nft/<tg_id>` link was a zero-width HTML anchor (invisible link
text) -- Telegram's link-preview generation is confirmed unreliable for
that specific case. **Fixed**: `notifier.format_caption()` now appends
the link as a genuinely VISIBLE line at the end of the message (short
"·" anchor text), never zero-width. If `tg_id` is missing (confirmed to
happen occasionally), the link line is omitted entirely -- the message
still sends, just without a preview; this never raises.

**Дефект 2 -- repeat notifications for the same listing, confirmed
live**: Cupid Charm #8599 was notified twice within a minute (22.00,
then 21.60) -- two genuinely distinct `price_history` rows (different
`observed_at`), so the `(listing_external_id, observed_at)`
anti-duplicate check in `alerts_sent` never catches this; it's a
different failure mode (repeat notifications about the SAME listing in
quick succession) from the one that check was built for. **Fixed**: new
`SIGNAL_COOLDOWN_MIN` (default 60 minutes) -- if this
`listing_external_id` was already notified more recently than that, the
new notification is suppressed (`signals_suppressed_cooldown` in the
run summary), UNLESS the new price is lower than the last-notified price
by more than `SIGNAL_RESEND_DROP_PCT` (default 10%), in which case it's
sent anyway with the header marked `ЛИСТИНГ · ЦЕНА СНИЖЕНА` so the user
understands why they're seeing it again so soon. Implemented via
`Poller._check_cooldown()` (a local DB query, no network cost) run
BEFORE the pre-send freshness check, so a cooldown-suppressed signal
never wastes a freshness-check API call. The last-sent price is read
back via `db.get_last_sent_alert_for_listing()`, joining `alerts_sent`
to `price_history` on the `(listing_external_id, observed_at)` pair
`alerts_sent` already keys on -- no new price column was needed.

**Not yet verified live in this delivery** (no network access in this
environment) -- the user must confirm notifications now unfurl
consistently (not just for the specific lot that failed before), and
watch for `signals_suppressed_cooldown` incrementing appropriately
without silently swallowing genuinely new price drops.

## Multi-id batching confirmed live; faster lifecycle sweep; explicit limit=; not-returned counter

**CONFIRMED live** (retracts the "UNCONFIRMED" note in the entry below):
`ids=` DOES accept multiple comma-separated values in one call --
`ids=00321f3c-c872-4299-ab1e-1858698556b6,0056c416-cc80-4a6e-892e-8f1a61f3de7b`
returned 200 with both listings, correct `status`/`price` for each.

**Правка 1 -- faster sweep, using the now-confirmed batching**:
`LIFECYCLE_BATCH_SIZE` default raised 20 -> **50** (the confirmed max
effective page size on `/nfts/search`, same limit `search()` already
hits). `LIFECYCLE_CHECK_INTERVAL_SEC` default lowered 600 -> **300**.
Recomputed budget: at ~3500 tracked listings, batch 50, interval 300s, a
full sweep is 3500/50 = 70 batches × 300s ≈ **5.8 hours** (previously
~29.2h at batch 20 / interval 600s) -- comfortably inside
`LIQUIDITY_WINDOW_HOURS` (168h), and now fast enough that same-day
disappearance data is realistic for most listings.

**Правка 2 -- explicit `limit=`**: `PortalsClient.search_by_ids()` now
always sends an explicit `limit=` equal to the number of ids requested
(`?ids=...&limit=<N>`) -- without it, the server's default page size
could silently truncate the result set below what was asked for, the
same failure class as the floors-by-model-name endpoint's silent
name-dropping. Tested: a 50-id request puts `limit=50` in the URL.

**Правка 3 -- `lifecycle_not_returned` counter**: `poller.py` now diffs
the requested id set against the ids actually present in the response
on every lifecycle-check batch; the count of ids the API silently
dropped is accumulated into `lifecycle_not_returned` in the run
summary. Per spec, this is diagnostic only -- a missing id is still
never treated as disappearance (unchanged from the entry below) -- but
if this counter turns out large in a live run, it means `/nfts/search`
silently drops some requested `ids` values, the same way
`/collections/models/backgrounds/floors` silently drops some requested
model names (see `floors.py`'s retry-once-then-give-up handling for
that precedent).

**Not yet verified live in this delivery** (no network access in this
environment) -- the user should watch `lifecycle_not_returned` over a
real run; if it's consistently non-zero at a meaningful rate, treat it
the same way the floors endpoint's silent drops were treated (assume
some ids are dropped, don't assume a single retry fixes it).

## Listing lifecycle tracking was measuring feed resurfacing, not disappearance -- fixed (schema v10)

**Confirmed live, the original mechanism was wrong**: overnight,
`lifecycle_newly_gone = 39325` against only 11705 listings ever
collected and 993 new that same night -- disappearing more times than
rows exist is impossible for a real signal. In `listing_lifecycle`:
3542 rows, 2006 marked disappeared, 2333 had reappeared at least once;
one listing (`d851447b-afc5-4f97-b082-1cbfff590413`) hit
`reappeared_count=41` in a single night.

**Root cause**: the rule "not seen in the feed for
`LIFECYCLE_GONE_AFTER_SEC` (600s) -> disappeared" is unsound. The
poller only reads the first `MAX_PAGES_PER_ITERATION` pages of
freshly-surfaced listings; a listing sitting deep in the order book for
a while (not gone at all, just not near the top) drops out of that view
and gets marked "disappeared" ~10 minutes later purely because it
stopped resurfacing near the top. The moment its seller so much as
touches its price, it resurfaces on page 1 and gets marked "returned".
The whole mechanism was measuring how often a listing bubbles to the
top of the feed, not whether it left the market. **`LIFECYCLE_GONE_AFTER_SEC`
is retired -- no time-based rule of any kind decides disappearance any
more.**

**Fix**: disappearance is now decided ONLY by an explicit status check --
`GET /nfts/search?ids=<id1>,<id2>,...` (confirmed live: this endpoint
returns each queried listing's CURRENT status; a withdrawn lot returned
`status="withdrawn", price=null`). A background pass
(`Poller._run_lifecycle_check_batch()`, on its own
`LIFECYCLE_CHECK_INTERVAL_SEC` timer, default 600s) batches
`LIFECYCLE_BATCH_SIZE` (default 20) not-yet-disappeared listings, oldest
`last_checked_at` first, through this check:
- `status == "listed"` -> only bumps `last_checked_at`.
- `status in ("withdrawn", "unlisted")` -> sets `disappeared_at` +
  `final_status`, the only two values confirmed to mean "gone" (see
  `db.DISAPPEARED_STATUSES`).
- any OTHER status value -> logged as unrecognized, treated as
  "unknown" (bumps `last_checked_at` only, never marked disappeared;
  see `db.KNOWN_LIFECYCLE_STATUSES`).
- a listing MISSING from the response entirely -> logged, `disappeared_at`
  is **never** set from an absence (an absence proves nothing) --
  `last_checked_at` still bumps so it doesn't monopolize the queue.

Reappearance detection (`disappeared_at` cleared + `reappeared_count`
incremented) is unchanged and still lives in
`db.touch_listing_lifecycle()`, called from the FAST PATH whenever the
poller sees a listing again in the ordinary feed scan -- the background
check never re-queries already-disappeared rows, so a relist is only
ever caught by resurfacing in the feed, exactly as before. That half of
the mechanism was never the bug.

**SUPERSEDED by the entry above -- batch/interval defaults changed
(50/300s, ~5.8h sweep) and multi-id batching is now CONFIRMED, not
unconfirmed.** Original numbers kept for history only:

**Request budget, recorded so thresholds are chosen deliberately, not
guessed**: at ~3500 tracked listings, batch 20, interval 600s, a full
sweep of every tracked row takes ~3500/20 batches × 600s ≈ **29.2
hours**. Acceptable against `LIQUIDITY_WINDOW_HOURS` (168h = 1 week),
but genuinely slow -- do not expect same-day disappearance data for most
listings.

**UNCONFIRMED**: `ids=` with MULTIPLE comma-separated values in one
`search_by_ids()` call. Only the single-id case
(`?ids=<uuid>`, used by the pre-send freshness check) was independently
verified live before this delivery shipped; the batched form is assumed
to behave the same way other comma-joined filter params on this
endpoint do (`filter_by_models`, `filter_by_backdrops`), but was not
separately confirmed. If a live run shows a batched call returning
fewer or wrong results, lower `LIFECYCLE_BATCH_SIZE` toward 1 and
report it.

**Cleanup**: `gift_sniper/lifecycle_reset.py` (`db.reset_lifecycle_data()`)
resets `disappeared_at`, `reappeared_count`, `final_status`, and
`last_checked_at` to their defaults on every `listing_lifecycle` row --
data collected under the retired rule is confirmed unreliable and must
not be treated as real. `first_seen_at`/`last_seen_at` (still-accurate
observation timestamps) and the pair identity columns are preserved.
Run once after upgrading to schema v10; the table itself is never
dropped, only its corrupted columns reset. Schema v10's own migration
(`_migration_9_to_10`) is schema-only and does NOT reset data on its
own -- the reset is a deliberate, explicit, separately-run action, never
a silent side effect of migrating.

**IMPORTANT, still true and reinforced by this fix**: disappearance
(confirmed via `status="withdrawn"`/`"unlisted"`) is still NOT proof of
a sale -- the owner could simply have delisted it. This codebase still
has no way to distinguish the two; only whether a disappeared listing
later reappears (delist/relist) is observable.

**Not yet verified live in this delivery** (no network access in this
environment) -- the user must run `lifecycle_reset.py` against the real
DB, then run the poller for ~30 minutes and confirm:
`lifecycle_newly_gone` is now comparable to (not many multiples larger
than) the number of listings actually checked that run, and no
`reappeared_count` exceeds 2-3 across the whole table. Also worth
independently confirming the `ids=` multi-value batching assumption
above with a real multi-id request.

## Notification text stripped to three blocks -- decision, not data dump

Per spec: "Убрать из уведомления всё, что требует от пользователя
думать. Софт считает — пользователь получает решение." `notifier.py`'s
`format_caption()` now emits exactly three blocks and nothing else:

```
ЛИСТИНГ

<collection> #<gift_number>
<model> · <backdrop> · <symbol>

<price> <currency>

FLOOR: <floor>
```

Removed from the VISIBLE Telegram text: delta% (`−1.1%`), the old
price, `в стакане: N` (listed_count), the entire "Ликвидность" line, the
"Флор по модели, не по фону" caveat, and the absolute profit line. No
emoji anywhere in the body (the zero-width preview anchor and the single
"Купить на Portals" button are unaffected, per spec).

**Critically, none of this touched the filtering logic.** Every field
removed from the message is still computed and still stored on
`signals.Signal` (`delta_pct`, `old_price_nano`, `listed_count`,
`pair_gone_count`, `pair_median_time_to_gone_hours`, `profit_nano`,
`profit_usd` -- all present, all written to the DB where applicable, all
still printed by `report.py`'s diagnostics), and `signals.run_cascade()`
(thin-book, `is_ladder`, `is_anomaly`, `is_bulk_update`,
`is_implausible`, floor no_data, the ratio check, the pre-send freshness
check in poller.py) is completely unchanged -- this delivery only
touched `notifier.format_caption()`. Verified by two regression-pin
tests: `test_clean_signal_set_unchanged_by_minimal_notification_format_delivery`
(re-runs the full mixed-cascade fixture from prior deliveries and
asserts the identical surviving signal set) and
`test_report_still_prints_all_diagnostic_fields_after_minimal_notification_format_delivery`
(asserts report.py's diagnostic columns/lines are all still present).
The principle going forward: the notification is a decision, not a data
dump -- the filtering IS the decision-making, already done before the
message is built; what changes here is only what the user has to read.

## Per-lot deep link found (retracts prior "no per-lot link" conclusion); minimal message format; pre-send freshness check (schema v9)

**Правка 1 -- a per-lot Portals deep link DOES exist, confirmed live:**

```
https://t.me/portals_market_bot/market?startapp=gift_<external_id>
```

opens that exact lot's card with a buy button. `external_id` is the
listing's own `id` (UUID), already stored. The suffix seen in an
original share link (e.g. `_mpb14p`) is confirmed NOT required. **This
retracts the previous delivery's conclusion** that no per-lot deep link
exists on this platform -- that conclusion was drawn from only seeing
the calling side of the mini-app bundle's share-link code
(`formatShareNftLink`); the function body that actually builds the
`gift_` URL lived in a different bundle chunk that hadn't been
inspected. Lesson for future bundle analysis: a call site alone is not
enough to conclude a feature doesn't exist -- trace into the callee
before ruling something out. `notifier.PORTALS_DEEP_LINK_CONFIRMED =
True` now covers the per-lot case; the earlier collection-level link
(`_portals_collection_link`) is retired -- the precise lot link makes it
unnecessary. The keyboard is now exactly ONE button ("Купить на
Portals"); the old second button ("Подарок в Telegram") is gone -- the
gift card already renders as a clickable preview in the message itself.

**Правка 2 -- message format overhauled to be compact, no emoji.**
Header changed from "Новый листинг" to "ЛИСТИНГ". Traits (model,
backdrop, symbol) collapsed onto one `·`-joined line instead of three
labeled lines. The `t.me/nft/<tg_id>` link that used to be a visible
first line is now a zero-width HTML anchor
(`<a href="...">&#8203;</a>`) placed at the end of the message -- it is
still present in the text (so Telegram's link preview still unfurls the
gift card with its animation, which was its only real job) but is no
longer shown as readable text. The old "🔎 Найти в маркете: #N" line is
retired -- it only existed to compensate for the old collection-level
button not landing on the exact lot; with the per-lot link, it serves
no purpose.

**Правка 3 -- pre-send freshness check (schema v9, `alerts_sent.status`).**
Confirmed live: `/nfts/search?ids=<id>` returns a listing's CURRENT
status/price -- a withdrawn lot returned `status="withdrawn",
price=null`. `poller.py`'s `_maybe_notify()` now calls
`PortalsClient.search_by_ids()` immediately before sending each signal
and requires `status == "listed"` AND the current price to still match
what was notified. A stale result (withdrawn, sold, or repriced) is
never sent; it's recorded in `alerts_sent` with
`status='skipped_stale'` (a new column, migration `_migration_8_to_9`)
so it is never re-checked either, and `signals_stale` is incremented in
the run summary. A freshness-check FAILURE (network/API error) is
deliberately NOT treated the same as a confirmed-stale result -- it's
unverified, so the signal is retried on the next check exactly like a
plain send failure, and is not written to `alerts_sent` at all
(`Poller._check_signal_still_fresh()` returns `True` / `False` /
`None` for these three distinct outcomes). One extra `/nfts/search`
request per signal about to be sent (not per page) -- negligible at the
measured 2-3 signals/hour.

**Not yet verified live in this delivery** (no network access in this
environment) -- the user must confirm the `gift_<external_id>` deep
link actually opens the buy page for a real lot, that the gift preview
still unfurls correctly with the link now hidden as a zero-width
anchor, and watch for a real withdrawn/repriced lot being correctly
skipped as stale during a live run.

## Working market button, listing lifecycle tracking (schema v8), liquidity in notifications

**SUPERSEDED by the entry above ("Per-lot deep link found") -- a per-lot
deep link DOES exist; do not act on the "no per-lot link" conclusion
below.** Kept for history only.

**Правка 1 -- confirmed collection-level deep link, no per-lot link
exists.** Verified against the Portals mini-app bundle's own parsing
code (`parseStartCollectionId`: checks a `"collection-"` prefix on the
`startapp` param and reads everything up to the first `"_"` as the
value -- a UUID, the same `collection_id` stored in `listings`):

```
https://t.me/portals_market_bot/market?startapp=collection-<collection_id>
```

opens the market storefront filtered to that collection.
`notifier.PORTALS_DEEP_LINK_CONFIRMED = True`, but **collection-level
ONLY** -- a per-lot deep link does not exist on this platform. Confirmed
absent, not merely unconfirmed: the bundle's full list of `to`
destination values (`market`, `terms`, `privacy`, `tournament`, `hub`,
`escape`, `arena`, `staking`, `coinflip`, `ladder`, `race`, `blocks`) has
nothing for an individual listing, and `"collection-"` is the only
recognized content prefix. Do not search for this further. The
"Коллекция на Portals" button uses this link; "Подарок в Telegram" keeps
the confirmed `t.me/nft/<tg_id>` link; the caption gained a
`🔎 Найти в маркете: #<gift_number>` line so the user can locate the
exact listing via the market's own search once the storefront is open.

**Правка 2 -- listing lifecycle tracking (schema v8, `listing_lifecycle`
table).** `poller.py` now calls `db.touch_listing_lifecycle()` for
EVERY listing seen on EVERY poll iteration (new or already-known),
updating `last_seen_at`. A separate, coarser sweep
(`maybe_run_lifecycle_check()`, on its own `LIFECYCLE_CHECK_INTERVAL_SEC`
timer, default 300s) marks `disappeared_at` for anything not seen in the
last `LIFECYCLE_GONE_AFTER_SEC` (default 600s). If a "disappeared"
listing is seen again, `disappeared_at` is cleared and
`reappeared_count` incremented -- this is a delist/relist, not counted
toward liquidity.

**IMPORTANT LIMITATION, stated explicitly per spec: disappearance is NOT
proof of a sale.** A listing can disappear from `/nfts/search` because
it sold, OR because its owner delisted it for any other reason. This
codebase has NO way to distinguish the two from the collector data
alone -- the only thing it CAN tell is whether a disappeared listing
later reappears (delist/relist) or stays gone (consistent with, but not
proof of, a sale). Every place `listing_lifecycle`/`pair_gone_count` data
is presented -- notifier.py's "Ушло с рынка" line, any future report --
must describe it as "listings that left the market", never as "sold" or
"sales". Do not let this distinction erode in future deliveries.

**Правка 3 -- liquidity in notifications.** `db.pair_liquidity_stats()`
counts, for one exact `(collection_id, model_name, backdrop_name)`
triple, how many listings disappeared (and have not since reappeared)
within `LIQUIDITY_WINDOW_HOURS` (default 168h = 1 week), plus the median
hours from `first_seen_at` to `disappeared_at`. `signals.clean_signals()`
attaches this to every `Signal` as `pair_gone_count` /
`pair_median_time_to_gone_hours`, but ONLY when `pair_gone_count >=
MIN_LIQUIDITY_SAMPLE` (hardcoded 3, not env-configurable -- not asked
for) -- below that, both stay `None` rather than showing a number
computed from too little data. `notifier.py` prints either
`📉 Ушло с рынка: N шт · медиана H ч` or, when there isn't enough data,
`📉 Ликвидность: недостаточно данных`.

**Правка 4 -- NOTIFY_LEVELS defaults to "pair" only (already true, now
re-confirmed and documented with reasoning).** Manual review of four
real level=model notifications (Durov's Cap #1559, Electric Skull #4908,
Mini Oscar #891, Bling Binky #3796) found none of them were actionable
-- the model floor refers to a different backdrop, sale price unknown.
This is the same class of problem as the previously-confirmed Durov's
Glasses #3685 case (bot showed $29 "profit" against a model floor for a
listing that was itself the cheapest in its own pair). level=model
remains available in the code and in `report.py`'s diagnostics, opt-in
via `NOTIFY_LEVELS=pair,model` in env -- never sent by default.

**Not yet verified live in this delivery** (no network access in this
environment) -- the user must run the poller for long enough to
accumulate real `listing_lifecycle` data, confirm the collection deep
link actually opens the Portals storefront filtered correctly, and watch
for real disappear/reappear cycles to sanity-check `reappeared_count`
against what actually happened on the marketplace.

## Fixed: fabricated absolute profit at level=model; message format overhaul

**БАГ 1, confirmed live**: Durov's Glasses #3685 (Underwater + Chocolate),
price 91.37, "floor" 115.00 at level=model -- the bot reported ~20.98 TON
(~$29) profit. Checked on the marketplace: this exact listing was ITSELF
the cheapest in its (model, backdrop) pair; the next real offer was
94.00. Real flip potential was ~2.5 TON before fees, not $29. Root cause:
`profit = floor*(1-fee) - price - withdrawal` used the MODEL floor, which
is another item's price (a different backdrop within the same model) --
not a price this listing could realistically sell for. The formula is
only valid at level=pair, where floor is a truly comparable item (same
model, same backdrop).

**Fixed**: `signals.Signal.profit_before_withdrawal_nano` /
`profit_nano` / `profit_usd` are now `None` for level="model" -- computed
ONLY at level="pair" (see `signals._signal_from_row`). Every consumer
handles this: `report.py`'s profit-based counts now explicitly scope to
`level=pair` rows; `notifier.py` shows the discount% and a warning
instead of a profit line for model-level signals (see Правка 3 below).
`NOTIFY_MIN_PROFIT_USD` now applies ONLY to level=pair signals;
level=model signals are gated by the new `NOTIFY_MIN_DISCOUNT_PCT` (env,
default 15, UNMEASURED like `FLOOR_MAX_RATIO_TO_PRICE`) instead --
`notifier.passes_notify_threshold()` is the single place this branches.

**БАГ 2 investigated**: a raw `clean_signals()` diagnostic dump over 2
hours showed mostly NEGATIVE profit (-10.13, -770.49, -45.60), while the
bot in the same window sent signals with positive profit. Root cause:
these are not actually in conflict. `clean_signals()` itself never
filtered by profitability -- it returns every signal that survives the
cascade, profitable or not; only the bot's `_maybe_notify()` additionally
filters by `NOTIFY_MIN_PROFIT_USD`/`NOTIFY_MIN_DISCOUNT_PCT` before
sending. Since ~90% of clean signals are level=model (most (model,
backdrop) pairs have exactly one listing -- see pair_floor.py), and
БАГ 1's fabricated model-level profit could swing wildly negative
whenever the model floor happened to be a cheaper backdrop than the
listing's own price, the raw diagnostic dump was dominated by nonsense
numbers from exactly the bug fixed above. With profit_nano/profit_usd
now `None` at level=model, a raw dump no longer produces these
numbers at all for that level. Verified BOTH callers see identical
output: `test_clean_signals_identical_across_two_independent_connections`
seeds a real on-disk DB, opens it via two SEPARATE `db.connect()`
connections (simulating the poller process and an external diagnostic
script), and asserts `clean_signals()` returns byte-for-byte identical
`Signal` lists from both -- confirming the function is a pure function
of DB state (plus its `since`/`now`/`usd_rate` arguments), never of which
process called it.

## Telegram notifications (notifier.py), and the filter cascade moved into signals.py

**Architecture change first**: the entire "what counts as a clean signal"
filter cascade (is_anomaly -> noise -> is_ladder -> floor no_data ->
thin-book -> is_implausible -> is_bulk_update) was moved out of
report.py's `_price_drops_block()` into a new module, `signals.py` --
`signals.run_cascade()` (every stage, for report.py's diagnostic
counts) and `signals.clean_signals()` (the final survivor list, as
`Signal` dataclasses) are now the SINGLE source of truth. report.py's
`_price_drops_block()` is now presentation-only, built from
`signals.run_cascade()`'s output; `backfill_ladder` and `_floor_and_level`
are re-exported from `report.py` for backward compatibility but live in
`signals.py`. This was necessary because the Telegram bot needs the
exact same signal set report.py prints -- duplicating the cascade would
have meant a filter change (e.g. tightening `FLOOR_MAX_RATIO_TO_PRICE`)
silently applying to only one of the two consumers. Verified: a test
(`test_clean_signals_matches_report_py_on_the_same_db`) asserts
`signals.clean_signals()`'s listing set matches report.py's printed
`CLEAN signals: N` count on an identical DB, exercising every cascade
stage in one fixture.

**New `notifier.py`**: `TelegramNotifier` (HTTP-only via `requests`, no
external Telegram library -- same approach as `portals_client.py`;
`TELEGRAM_BOT_TOKEN` is NEVER logged, matching `PORTALS_AUTH`'s handling
in `auth.py`) sends ONE `sendMessage` per clean signal (UPDATED this
delivery -- see "Message format overhaul" below; `sendPhoto` was retired
entirely), with an inline keyboard. `CommandHandler` answers `/start`,
`/status`, `/last` from `TELEGRAM_USER_ID` only -- every other sender
gets a fixed "доступ ограничен" reply; this is a personal tool, not a
public bot.

**UNCONFIRMED, ACTION NEEDED FROM THE USER**: the exact Portals-market
deep-link format (something like `t.me/portals/market?startapp=...`) was
never verified to actually open a listing in the mini-app -- per spec,
guessing was refused. Both inline buttons ("Открыть на Portals" and
"Подарок") still fall back to the CONFIRMED-working
`https://t.me/nft/<tg_id>` link (`notifier.PORTALS_DEEP_LINK_CONFIRMED =
False`). **To unblock this**: open any Portals lot directly in the
Telegram mini-app, use its "поделиться"/share menu, and send the coder
the exact URL it copies -- that reveals the real deep-link format so
`_portals_market_link()` can be implemented and
`PORTALS_DEEP_LINK_CONFIRMED` flipped to `True`.

**Integration with poller.py**: `Poller._maybe_notify()` is hooked into
`maybe_run_floor_worker()`'s existing timer (`FLOOR_WORKER_INTERVAL_SEC`)
-- "после каждого прохода ANALYTICS PATH", per spec, not the faster
FAST PATH collection cadence. It calls `signals.clean_signals(conn,
since=<last check>, usd_rate=<from the latest /market/config
usd_course>, now=<real time>)`, filters by `NOTIFY_LEVELS` (default
`{"pair"}` ONLY -- model-level signals are never notified by default,
per spec, since they're the coarser, less-trustworthy comparison
`model_level_audit.py` exists to measure) and
`notifier.passes_notify_threshold()` (`NOTIFY_MIN_PROFIT_USD` for
level=pair, `NOTIFY_MIN_DISCOUNT_PCT` for level=model -- see "Fixed:
fabricated absolute profit" above), and skips anything already in the
`alerts_sent` table (schema v7 -- `(listing_external_id, observed_at)`
primary key, durable across restarts, unlike an in-memory set).
`notifier.select_signals_to_send()` caps a burst at
`NOTIFY_MAX_PER_MINUTE` (default 10, Telegram's own ~20/min-per-chat
limit confirmed in the Bot API docs), ranking level=pair signals by
profit_usd ABOVE level=model signals ranked by discount% (`_priority()`
-- a real dollar profit outranks an unknown one), and summarizing the
rest in one line. A send failure
(`TelegramNotifier.send_signal()` returns `False`, never raises) is
logged and the signal is simply retried on the next check -- it is
never marked `alerts_sent` on failure, and a notification failure can
never take the poll loop down (`_maybe_notify`/`_maybe_process_commands`
are the only things standing between a Telegram outage and the FAST
PATH collection loop, and both are exception-isolated). `usd_course` is
reused from the already-collected `/market/config` snapshots
(`_current_usd_rate()`) rather than adding a second, easily-stale manual
env var for the same number; falls back to a `Decimal(1)` placeholder
(logged once) if no snapshot has been observed yet.

**Schema v7**: `alerts_sent(listing_external_id, observed_at, sent_at)`,
migration `_migration_6_to_7` (idempotent, tested for both fresh-migrate
and no-op-on-rerun). Version detection needed a new branch ahead of the
column-based checks (`_table_exists(conn, "alerts_sent")`) since this is
the first migration in this project that adds a whole new TABLE rather
than columns on an existing one, and floor_snapshots' columns alone can't
distinguish v6 from v7.

**Not yet verified live in this delivery** (no Telegram bot token /
network access in this environment) -- the user must set
`NOTIFY_ENABLED=true`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_ID` and run
the poller for ~20 minutes, confirming: notifications arrive as a
message whose gift link unfurls into a card with animation (see
"Message format overhaul" below), with working buttons; a restart does
not re-send the same signals
(alerts_sent survives); `/status` and `/last` respond correctly to the
owner's messages and correctly refuse anyone else's.

## Dual-floor sampling (poller.py), and a fixed test-fragility root cause

`model_level_audit.py`'s Part 1 came back "rows with BOTH pair and model
floor filled: 0" -- not a script bug, but the direct, mechanical
consequence of the fetch discipline added earlier: the model floor is
only ever requested when the pair floor came back `"alone_in_pair"` (see
Правка 2 in the earlier delivery), so the two levels are mutually
exclusive by construction and there was never any row to compare them
on.

**Added `DUAL_FLOOR_SAMPLE_PCT`** (env, default 0 -- disabled, so
ordinary runs spend nothing extra): for a deterministic sample of rows
whose pair floor already came back `"ok"`, `run_floor_worker()` ALSO
fetches the model floor purely for comparison, writing it into the same
existing `model_floor_*` columns (no schema change). Selection is
`zlib.crc32(external_id.encode()) % 100 < DUAL_FLOOR_SAMPLE_PCT` --
deliberately NOT Python's builtin `hash()`, whose string hashing is
randomized per process (`PYTHONHASHSEED`) by default and would silently
give a different sample on every restart, defeating the point of a
repeatable sample. `run_floor_worker`'s sampling check never fires when
the pair level is `"alone_in_pair"` (the existing model-fallback fetch
already covers that case) -- it only adds a fetch on top of an
already-`"ok"` pair result. New `dual_floor_samples` counter in the run
summary. At 20% sampling against the measured ~30 pair-`"ok"` floors per
20-minute run, this is ~6 extra floor-endpoint requests per 20 minutes --
negligible against the confirmed 5 req/s limit on that endpoint.

**Fixed a latent test-fragility root cause, properly** (not a datestamp
patch, which would have broken again the next day): three tests in
`test_price_drops_report.py` hardcoded `observed_at` fixture dates on
2026-09-06 while `backfill_ladder()`'s ladder window was computed from
real `datetime.now()` -- once wall-clock time passed 2026-09-07T15:00,
the fixtures fell outside the 24-hour `LADDER_WINDOW_HOURS` window and
the tests started failing, despite nothing about ladder detection
actually being broken. `backfill_ladder(conn, now=None)` now accepts an
optional, explicit `now` (defaults to real time -- production behavior
unchanged); `_price_drops_block()` and `generate_report()` both gained
the same optional pass-through, as `drops_now` on `generate_report()`
specifically (NOT `now`, to avoid colliding with `generate_report`'s own
pre-existing local variable `now = datetime.now().isoformat()`, used for
the report's `generated_at` line -- the two are unrelated concepts that
happen to want the same name; the first attempt at this fix silently
shadowed the parameter with that string and broke every test in the
file with `TypeError: unsupported operand type(s) for -: 'str' and
'datetime.timedelta'`, caught immediately by the full suite). The three
affected tests now pass a fixed `drops_now`; no other caller of
`generate_report()`/`report.py`'s CLI is affected (parameter is optional
and defaults to unchanged behavior).

## model_level_audit.py -- measuring how much model-level floor disagrees with pair-level

New, standalone, read-only, offline script (`python -m
gift_sniper.model_level_audit --db <path>`). No network calls, no schema
change, no changes to `poller.py` or `report.py` -- this is measurement
only, triggered by the observation that ~180 of ~200 clean price-drop
signals are computed against the coarser MODEL floor and only ~20 against
the precise PAIR floor (most (model, backdrop) pairs have exactly one
listing -- see pair_floor.py -- so pair_floor_status is usually
"alone_in_pair" and the model level is used as a fallback far more often
than not).

**Four parts**, each printed by `generate_audit()`:
1. Direct level comparison on every `floor_snapshots` row where BOTH
   `pair_floor_excl_self_nano` and `model_floor_excl_self_nano` are
   filled: `ratio = model_floor / pair_floor`, with percentiles and the
   fraction beyond a 1.5x / 0.67x disagreement in either direction.
2. Re-runs report.py's exact filter cascade (`is_anomaly` -> noise ->
   `is_ladder` -> floor no_data -> thin-book -> `is_implausible` ->
   `is_bulk_update`) IN MEMORY, without writing anything to the DB
   (`_ladder_listings`/`_find_bulk_update_ids` are non-mutating
   reimplementations of report.py's `backfill_ladder`/bulk-update logic
   -- report.py itself is untouched, per spec). For each clean
   MODEL-level signal with a pair floor available within 1 hour of the
   drop (via `floor_snapshots.floor_fetched_at`), classifies whether the
   signal would survive (discount vs. pair floor still > 10%) or
   disappear, with up to 15 line examples.
3. Groups models by backdrop-price spread (`max/min` over the prices in
   the saved `raw_model_block` API-combo-floor block, bucketed <1.5 /
   1.5-3 / 3-10 / >10) and cross-tabulates against Part 2's
   not-confirmed-by-pair fraction per bucket.
4. Three-line summary: median level disagreement, fraction of model
   signals not confirmed, and a heuristic (>= 15 percentage point gap
   between the lowest- and highest-populated spread buckets) yes/no
   verdict on whether spread predicts unreliability -- explicitly NOT a
   statistical test, a judgment-call threshold to be revisited with more
   data.

One SQL pitfall hit and fixed while building this: `SELECT ph.*, ...,
f.floor_fetched_at` on the `price_history JOIN floor_snapshots` query
silently returns the WRONG value for `row["floor_fetched_at"]` --
`price_history` has its own same-named column (the moment-of-drop floor
re-fetch timestamp, usually NULL for a snapshot-fallback signal), and
`ph.*` expands first, so `sqlite3.Row`'s name-based lookup resolves to
that one instead of `floor_snapshots`'s. Fixed by aliasing the join
column (`f.floor_fetched_at AS f_floor_fetched_at`); caught immediately
by the test for a model-signal-with-a-worse-pair-floor scenario, which
silently reported zero cross-checkable signals until the alias was
added -- a useful reminder that any query joining two tables with
overlapping column names needs an explicit alias, not just wildcard
selects.

**Not yet run on a real DB in this delivery** -- the user should run it
against their live database and share all four parts' output, especially
Part 4's summary line on whether spread predicts unreliability (this
determines whether report.py should eventually gain a spread-based
model-level trust filter -- explicitly out of scope for this delivery,
which is measurement-only).

**Unrelated, pre-existing test fragility observed while verifying this
delivery** (NOT caused by this delivery, and out of scope to fix here --
touching report.py wasn't requested): three tests in
`test_price_drops_report.py` (`test_price_drops_block_detects_ladder...`,
`test_three_significant_drops_form_a_ladder`,
`test_mixed_noise_and_significant_drops_counts_only_significant_ones`)
hardcode `observed_at` timestamps on 2026-09-06, and `backfill_ladder()`'s
ladder window is `datetime.now(timezone.utc) - LADDER_WINDOW_HOURS`
(24h) -- real wall-clock time has now passed 2026-09-07T15:00, pushing
those fixed fixture timestamps outside the 24-hour window, so the ladder
detection they're testing no longer fires and all three fail. This is a
latent fragility in those tests (relative time window vs. hardcoded
absolute fixture dates), not a regression from this delivery -- confirmed
by reproducing the same 3 failures with `test_model_level_audit.py`
excluded entirely. 120 of the 123 pre-existing tests still pass; this
delivery's own 6 new tests all pass. Flagging rather than fixing, since
report.py is explicitly out of scope for this task.

## Thin-book / implausible-ratio / bulk-reprice filters (report.py only, no schema change)

Manual review of 20 "clean" price-drop signals (after all prior filters --
`is_anomaly`, noise, `is_ladder`, floor no_data) found 18 of 20 were still
artifacts. All 18 had `pair_listed_count_excl_self == 1`: after excluding
the listing itself, exactly ONE other lot remained in the book, and its
price was taken as "the floor". Concrete examples: Khabib's Papakha
(Chokha+Hunter Green) price 23.91 vs. "floor" 230.00 (9.6x); Eternal Rose
(3D Glow+Black) 139.00 vs. 1000.00; Easter Egg (Scrambull+Black) 88.00
vs. 600.00. Confirmed by the liquidity breakdown itself: `cnt=1` -> 85
signals deeper than 35%, `cnt=2-3` -> 17, `cnt=4-9` -> 2, `cnt=10+` -> 0
-- the "discount" shrinks monotonically as real competition increases,
the signature of an artifact (one mispriced/unsold listing), not a
market pattern.

**Fixed, three independent filters, report.py only** (no DB schema
change -- these are report-time exclusions, applied fresh on every run,
not persisted flags):

1. **`FLOOR_MIN_LISTED_COUNT`** (default 3, env-configurable, floor
   enforced at 2 by a `ConfigError` -- a value of 1 would defeat the
   filter's purpose): a floor is only usable if `listed_count_excl_self`
   (pair OR model level, whichever resolved) meets this minimum.
   Excluded rows are counted in a separate "thin-book (excluded)" block
   (main distribution) and a "thin-book" cascade stage (price-drops
   block), broken out by `cnt=1` vs `cnt=2` so the shape of what's being
   cut stays visible.
2. **`FLOOR_MAX_RATIO_TO_PRICE`** (default 4.0, **UNMEASURED** -- unlike
   `FLOOR_SANITY_MAX_RATIO` above, this default has no live-measured
   basis yet; it is a deliberately conservative starting point, to be
   tightened or loosened once enough clean signals have accumulated to
   measure a real ratio distribution): a signal with `floor / new_price`
   above this is flagged `is_implausible` and excluded, with up to 10
   examples printed. This is a second, independent net on top of the
   listed-count filter -- even a floor computed from several other
   listings can still be an outlier.
3. **Bulk-reprice detection** (`SAME_SECOND_WINDOW`, default 5 seconds):
   confirmed live, three DIFFERENT Khabib's Papakha listings all recorded
   the identical drop 24.60 -> 23.91 at the identical timestamp
   19:14:32 -- one seller adjusting several of their own lots at once,
   not three independent market signals. Rows are grouped by
   `(collection_name, delta_pct)` and clustered by proximity in
   `observed_at`; a cluster spanning >= 2 DISTINCT `listing_external_id`
   values is flagged `is_bulk_update` and excluded entirely.

Cascade order in the price-drops block: `is_anomaly` -> noise -> 
`is_ladder` -> floor no_data -> thin-book -> `is_implausible` ->
`is_bulk_update` -> CLEAN. The same `FLOOR_MIN_LISTED_COUNT` gate is
applied identically in the main "new listing" distribution's
`_floor_and_level()` hierarchy (pair first, model fallback), so a floor
computed from too few listings is never usable at either call site. The
per-line clean-signal table gained a `ratio` (`floor/new_price`) column
-- the single number that makes an artifact visible at a glance.

**Not yet verified live in this delivery** -- the user must re-run
`report.py` on the existing DB (no new collection needed) and confirm:
Khabib's Papakha (Chokha+Hunter Green), Eternal Rose (3D Glow), and
Easter Egg (Scrambull) no longer appear among clean signals; the clean
signal count drops from ~292 to roughly 10-20; the new cascade stages
show non-zero counts; and Light Sword (Doom Slayer+Silver Blue, cnt=3,
floor 44.00, price 20.61, ratio 2.1) still survives all filters.

## Status/value desync fixed; model-level floor added as a fallback (schema v6)

Live diagnostic: in `floor_snapshots`, `pair_floor_status='ok'` on 3014
rows in one hour, but `pair_floor_excl_self_nano` filled on only 30 of
them (e.g. `pair_floor_nano=20400000000, excl_self=None, status='ok'`).
`report.py` filters `eligible` rows on `status='ok'`, so it silently
dropped nearly all of them at the "floor no_data" stage -- all 315
significant price-drop signals that hour, and all but 7 of `price_history`'s
9216 significant drops (`floor_at_drop_nano` filled on 7). Root cause,
confirmed live: 336 measured (model, backdrop) pairs had exactly one
active listing, zero had ten -- self-exclusion on a 1-listing book
almost always leaves nothing, so `"alone_in_pair"` is the COMMON outcome,
not a rare edge case.

**Fixed (`pair_floor.py`)**: `status` is now computed strictly from the
POST-exclusion result -- `floor_excluding_self_nano is None` ->
`"alone_in_pair"` (new value), filled -> `"ok"` (now a hard guarantee that
`floor_excluding_self_nano` is not `None`), nothing listed at all (not
even the excluded lot) -> `"no_data"`, request failure -> `"error"`.
`"ok"` co-occurring with an empty `pair_floor_excl_self_nano` is no longer
possible by construction.

**Added: model-level floor as a fallback comparison** (`pair_floor.py`'s
`PairFloorCache.get_model_floor()` / `get_model_floor_fresh()`, backed by
`PortalsClient.search_model_floor()` -- same `/nfts/search` query as the
pair level but WITHOUT `filter_by_backdrops`). Requested ONLY when the
pair level came back `"alone_in_pair"` -- never when pair is already
`"ok"`, so it never spends extra rate-limit budget on a comparison that
won't be used (tested: a mock client that raises if
`search_model_floor` is called when pair already gave `"ok"`).
Deliberately coarser than the pair level -- different backdrops price
differently within one model (e.g. measured live: Black 22.49 vs.
Emperor's median 4.10) -- so `report.py` NEVER mixes pair- and
model-level signals into one discount distribution; each row's resolved
`floor_level` (`"pair"` / `"model"`) determines which distribution it
counts toward, and the hierarchy (`pair` first, `model` fallback,
otherwise excluded) is applied identically for the main "new listing"
distribution, the price-drops block, and `floor_at_drop_nano` /
`floor_level_at_drop` (poller.py, on a significant drop where the pair
level is `"alone_in_pair"`).

**Schema v6**: `floor_snapshots` gained `model_floor_excl_self_nano`,
`model_listed_count_excl_self`, `model_floor_status`; `price_history`
gained `floor_level_at_drop`. `poller.py`'s run summary gained
`floor_ok_pair` / `floor_alone_in_pair` / `floor_ok_model` /
`floor_no_data`, so the level ratio is visible without querying the DB.

**Not yet verified live in this delivery** (no `PORTALS_AUTH` in this
environment) -- the user must run a 10-minute live poll and confirm:
`floor_alone_in_pair` is a noticeable share of resolved rows;
`floor_ok_model` is non-zero (model floor actually gets found where pair
floor is absent); and in the DB, fresh rows with `pair_floor_status='ok'`
have `pair_floor_excl_self_nano` filled (not the pre-fix 30/4407). Also
worth noting: before this delivery, the in-repo `pair_floor.py` already
computed `status` from the post-exclusion result and `poller.py` already
wrote it through consistently -- the live "ok + excl_self=None" symptom
therefore looks like it came from a poller process that hadn't been
restarted onto that code (schema was already at v5), a possibility flagged
to the user but not confirmable from here. All five ПРАВКА items were
implemented regardless, since `alone_in_pair`/model-level fallback are
genuine new capabilities independent of whether that particular symptom
was a stale-deploy issue.

## Filter cascade order bug, and historical data cannot be fully repaired

Live measurement after shipping the artifact filters (`is_anomaly` /
`is_ladder` / `is_noise` / floor no_data): `is_ladder` alone removed
11090 of 11850 drops (94%). Inspecting the detail confirmed these
"ladders" had `total drop 0.0-0.1%` and `avg step 0.0%` -- machine-level
bot relisting ticks, not real walk-downs. Root cause: `backfill_ladder()`
counted **every** drop toward `LADDER_MIN_DROPS`, and the filter cascade
checked `is_ladder` **before** `is_noise` -- so a listing with dozens of
0.01%-ish ticks (each individually going to be discarded as noise one
stage later anyway) looked like a "ladder" long before noise was ever
removed.

**Fixed**: `backfill_ladder()` now counts only SIGNIFICANT drops
(`is_noise = 0` in the SQL, i.e. `abs(delta_pct) >= PRICE_DROP_MIN_PCT`)
toward `LADDER_MIN_DROPS`. The cascade order is now `is_anomaly` ->
`is_noise` -> `is_ladder` -> floor no_data -- noise is gone before
`is_ladder` is even evaluated, so each stage's "removed" count means what
it says. A real ladder (Low Rider #23134-shaped: many 4-5%-ish steps)
still gets caught; a listing that's ALSO flagged `is_ladder` still has
`is_ladder=1` written to every one of its `price_history` rows (noise
included, since that's a property of the lot) -- only the cascade's
*displayed* "ladder rows" count is drawn from the post-noise-filter
remainder, which is the number that actually matters for interpreting
the report.

**Historical `pair_floor_excl_self_nano` cannot be recomputed offline --
confirmed, not fixed by a script.** Inspecting the schema and the actual
code path (`floors.py`) showed `floor_snapshots.raw_model_block` holds
the API COMBO-FLOOR block (`{"<backdrop>": "<price>"}`, keyed by backdrop
name) -- NOT `pair_floor.py`'s per-listing order-book response, which was
never persisted to any column. There is no `listing_external_id` in
`raw_model_block` to exclude, for any row that exists today. `backfill.py`
was written anyway (see "Offline backfill" below) as the honest version
of this: it processes every `floor_snapshots` row, defensively handles a
hypothetical future per-listing shape (fully tested), and for the actual,
current shape leaves every row `NULL` with the reason counted and
printed -- confirmed with the user rather than fabricating a value.
**Practical consequence**: the main discount distribution stays empty for
data collected before self-exclusion existed; it becomes non-empty only
for listings collected under the current `poller.py`, which computes
`pair_floor_excl_self_nano` live at ANALYTICS PATH time.

## report.py bypassed migrations entirely -- fixed, and the bug class closed

Live failure: `python -m gift_sniper.report` on a real, previously-created
DB raised `sqlite3.OperationalError: no such column:
f.pair_floor_excl_self_nano`. Root cause was NOT a missing migration --
`db.py` already had `CURRENT_SCHEMA_VERSION = 5` and a correct, tested
`_migration_4_to_5()`. The actual bug: `report.py`'s CLI entry point
(`main()`) called a bare `sqlite3.connect(args.db)` instead of
`db.connect(args.db)`, so **migrations never ran at all** for this code
path, regardless of how correct they were. This is the second time this
exact class of bug has hit the project (the first was
`api_combo_floor_nano` at v1->v2) -- both times because every unit test
creates its DB from scratch via `db.connect()`, so a caller that bypasses
it never gets exercised by the test suite.

Fixed two ways, not just patched:
- `report.py main()` now calls `db.connect()` (runs migrations) instead
  of `sqlite3.connect()`, and `generate_report()` itself calls
  `db.verify_schema(conn)` as its very first line -- so even a caller
  that still passes in a raw, unmigrated connection gets a clear
  `db.SchemaError` instead of an `OperationalError` from deep inside a
  query.
- `test_schema_regression.py` builds a REAL on-disk DB file shaped like
  each historical schema version (1 through 4) the way an actual old
  file would look, points `db.connect()` at it (the real production
  path, not a hand-picked internal function), and runs report.py's
  actual main query and poller.py's actual main insert against the
  result. A future column added to `FloorSnapshot`/`price_history`
  without a matching migration now fails this test immediately, instead
  of waiting for a second real-world report.py crash to surface it.

Also fixed in the same delivery: `config.py` used to read `PORTALS_AUTH`
at **import time**, so `report.py` -- which never touches the network --
couldn't even be imported without a token set. `get_portals_auth()`
reads it lazily now, at the point `auth.AuthManager` actually needs it
(client construction), not at module import.

## Price-drop signals before this fix were mostly artifacts

Manual review of the 20 largest recorded price drops found **17 of 20
were artifacts**, of three distinct kinds -- all now filtered, see "Price
drop artifact filtering" below:

1. **Self-comparison**: a listing alone in its pair (or currently the
   cheapest) IS the pair floor. Its own price cut then looks like a
   discount against a price nobody ever paid -- confirmed on Nail
   Bracelet #4695 (195->150, `pair_listed_count=1`, floor was its own old
   price of 195) and three more of the same shape.
2. **Relister-bot ladders**: Low Rider #23134 stepped 179.9 -> 146.51 ->
   ... -> 113.35, exactly 5% each step, ~30 minutes apart -- one bot
   mechanically walking its own price down, reported as 6 separate
   "signals" before this fix.
3. **Anomalous outliers**: Jelly Bunny #2627 went 999 -> 99 -> 29 within
   10 seconds -- an input error, test data, or manipulation, not a real
   90% single-step repricing.

Only 3 of the 20 were real: Vintage Cigar #3979 (275->244, floor 300,
where the floor is NOT the listing's own price), Xmas Stocking #224017
(35->30, floor 47.5), Jingle Bells #62308 (58.54->55.61, floor 73).

**Sanity-check status: run on SYNTHETIC data reconstructing these exact
7 examples (this session has no access to the user's actual collected
DB), not on live data.** All 7 came out exactly as expected: the two
self-comparison cases and Low Rider excluded (no_data / is_ladder), Jelly
Bunny excluded (is_anomaly), and all 3 genuine cases present among clean
signals. **Run `report.py` on the real, already-collected DB and confirm
the same 7 outcomes before considering this delivery closed** -- the
exact acceptance criteria are unchanged from the task: Low Rider must be
`is_ladder` and absent from clean signals; Jelly Bunny must be
`is_anomaly`; Nail Bracelet #4695 and Artisan Brick #4547 must be absent
(no_data after self-exclusion); Vintage Cigar, Xmas Stocking, and Jingle
Bells must all still be present.

## Data collected before this fix is unreliable

Every prior delivery's `poll_once()` bailed out of a page's-worth of
listings the moment it hit the first already-known `external_id`.
Confirmed live: known and new items on a page are **interleaved**, not
grouped -- a known id at position 3 of 50 meant positions 4-50 (which
could easily include dozens of new listings) were never even looked at.
Measured impact: over 174 seconds against a live stream running at
**~8 listings/second**, the poller found 4 new listings instead of the
~1400 that should have existed -- **under 1% of the actual stream**. Any
report or number generated before this fix (including the throughput
estimates of ~0.89/sec and ~0.02/sec recorded earlier in this README)
was computed on that same broken collection and should not be trusted.
See "FAST PATH / ANALYTICS PATH split" below for that fix, and
"COLLECT_MIN_PRICE: narrowing the collection loop" below for a second,
independent fix on top of it (the pagination fix alone still couldn't
keep up with the full stream under Portals' rate limit -- even after
fixing pagination, sustained throughput measured at ~1.3 new listings/sec
against a real stream of ~8/sec, i.e. still only ~16% visibility).

**Live sanity-check: performed since the note above was written, with a
surprising result.** Pagination itself was confirmed CORRECT at this
point (offsets 100 and 150 checked directly: zero unknown records,
pages tie together with no overlap and no gap -- the earlier loss
suspicion did not hold up at this depth). But the *real* new-listing
rate at `COLLECT_MIN_PRICE=15` measured at only **~0.2/sec** (126 over
600s), nowhere near the ~1.3-2/sec this file previously estimated.
Root cause, also confirmed live: **`listed_at` updates on a mere "touch"
of an already-known lot, with NO price change** (one lot's `listed_at`
jumped from the previous day to today at the same price,
28.01 both times) -- so most of the *visible* feed activity is known
listings resurfacing, not new ones, and the previous dedup logic
silently discarded ALL of that resurfacing, price changes included. See
"Price history: tracking known-listing price changes" below for the fix
that stopped throwing this away.

## Status: ШАГ 0 fully executed against the live API

Two live runs of `step0.py` have now produced results for every
question the probe was designed to answer — no open items remain in the
table below. Settings in `config.py` (`REQUEST_DELAY_MS`,
`FLOORS_BATCH_SIZE`) have been tuned to match what was actually measured,
not what was originally guessed.

## API combo-floor is unusable as a source of truth

`/collections/models/backgrounds/floors` answers by **model name only,
globally, with no collection scoping** — `collection_id`/`short_name`
params are ignored (confirmed live, byte-identical responses with or
without them). Model names collide across unrelated collections
constantly: a 30-minute sample had **32 model names appearing in 2-4
different collections each**. The consequence, measured on real data:

- **Berry Box**, real collection floor `9.35` → API combo-floor `200.0`.
- **Liberty Figure**, real collection floor `4.39` → API combo-floor `65-97`.

All 59 "signals" in the previous delivery's report were artifacts of this
substitution — the API returned some *other* collection's floor for the
same model name. The `name_collision` filter from that delivery does
**not** catch this: it only sees collisions inside the collected sample,
not across the whole market, and most of the corrupted rows had
`name_collision=0`.

**First attempted fix (also retired, see below):** `own_floors.py`
computed the combo-floor from our own collected `listings`, grouped by
`(collection_name, model_name, backdrop_name)` — correctly scoped by
collection, unlike the API floor. This fixed the *wrong-collection*
problem but introduced a different one: see "own_floors.py is not the
source of truth either" below. `api_combo_floor_nano` is still recorded
on every `FloorSnapshot`, but **only for diagnostics/comparison** —
discount and profit are computed exclusively from `pair_floor_nano` (see
"pair_floor.py: the actual fix").

**Safety net (diagnostic, not a fix):** `floor_sanity` flags an API
floor that is implausible relative to the listing's own
`collection_floor_nano` (`api_combo_floor_nano > collection_floor_nano *
FLOOR_SANITY_MAX_RATIO`, default ratio `8`, deliberately generous and NOT
independently measured). `report.py` excludes non-`ok` rows from the
"three methods side by side" comparison's denominator implicitly (rows
need `pair_floor_nano` known regardless) and reports it for visibility.

## own_floors.py is not the source of truth either

Grouping by `collection_name` fixed the cross-collection contamination,
but a second, independent problem surfaced once enough live data
accumulated: **own_floors.py is systematically too high, by roughly 2x**
(measured median own/pair ratio ≈ 1.85, i.e. true/own ≈ 0.54). Root
cause: the poller only ever observes listings at the moment they're
*newly listed* — a cheap listing put up weeks ago and still active in the
order book is never seen, so `own_floors.py`'s "minimum of everything
we've collected" silently excludes exactly the cheap, older listings that
would have been the real floor. In the last report before this fix,
**98.9% of rows were excluded by `own_confidence` being too low** —
the module wasn't just biased, it barely had data to be biased with.

`own_floors.py` is **not removed**: it keeps computing its value on every
snapshot, retained for comparison and as a fallback if the marketplace
ever closes off the search filters `pair_floor.py` depends on. It no
longer feeds discount or profit.

## pair_floor.py: the actual fix

The fix: query `/nfts/search` directly, filtered to one exact
`collection_id` + `model` + `backdrop` triple, sorted by price ascending.
This is a real, current order-book read — not an aggregate from an
opaque endpoint, not a lagging self-collected sample.

Confirmed live, all load-bearing:

- **Filter param names**: `filter_by_models`, `filter_by_backdrops`,
  `filter_by_symbols`, `filter_by_collections` — comma-separated string
  values. The bracketed form (`filter_by_models[]=X`) gets the connection
  **reset**. The unprefixed short forms (`model=`, `backdrop=`) are
  **silently ignored** — 200 OK, unfiltered data, no error at all. This
  is the worst failure mode possible and is exactly why `pair_floor.py`'s
  test suite checks the actual computed floor value, not just the HTTP
  status.
- **`collection_id` actually filters here** — unlike the floors endpoint,
  which ignores it. It is passed on every `pair_floor` request, always.
- **Parameter order is significant.** `sort` must be the FIRST query
  parameter. Measured: with `sort` placed after `filter_by_models`, the
  response came back in `listed_at` order (6.98, 13.58, 17.89, 5.49,
  5.77 — unsorted by price) with sort silently not applied; with `sort`
  first, prices came back strictly ascending (4.39, 4.39, 4.39, 4.9, 5.0,
  ...). `portals_client.PortalsClient.search_pair_floor` builds the URL
  manually with `urlencode` over an explicit ordered list of tuples —
  deliberately NOT a `dict` passed through `requests`' own `params=`
  handling, because that path offers no order guarantee a future
  refactor couldn't quietly break.
- **`status="unlisted"` / `price=null` entries appear in results, INCLUDING
  as the first element when sorted by price ascending** — a null price
  apparently sorts before every real value. Concretely: the pair
  Emperor+Black in the Ice Cream collection returned exactly one entry,
  unlisted, with no price. Taking the first element as the floor is
  explicitly wrong (it returns nothing, or worse, silently wrong data if
  such an entry ever carried a stale price) — `pair_floor.py` filters to
  `status=="listed"` and `price is not None` before taking the minimum,
  and a fixture-based test fails if that filtering is removed.
- **Order-book depth is real and uneven.** Emperor in Ice Cream alone had
  over 100 active listings (two full pages of 50) — sparsity lives at the
  (model, backdrop) PAIR level, not at the model level. This is why
  `limit=20` (not `limit=1`): enough room to skip past unlisted noise and
  still get a meaningful `pair_listed_count` as a liquidity signal.

`pair_floor.py` caches by `(collection_id, model_name, backdrop_name)`
with `PAIR_FLOOR_CACHE_TTL_SEC` (default 300s) — this query shares the
tightly-limited `/nfts/search` endpoint with the main listing poll, so
cache hits directly reduce contention on the 2-requests-per-window limit.

## Facts vs. assumptions

### Confirmed facts (from live requests)
- Base URL: `https://portal-market.com/api` (NOT `portals-market.com` — no A record).
- `GET /nfts/search?limit=2&offset=0` → 200, body `{"results": [...], "total_count": <int>}`.
- **Max effective page size is 50.** `limit=100` and `limit=200` both
  return HTTP 200 but only 50 items — the server silently caps the page,
  it does not reject the oversized request. `poller.py` and `step0.py`
  both hardcode `limit=50` for this reason; requesting more is pointless.
- **Default sort order is already `listed_at` descending** (newest
  first), with no `sort` param at all. `sort=latest` and the explicit
  `sort=listed_at desc` all produce the same order — **all three variants
  confirmed** to give identical, correctly-descending results.
- **Observed throughput (corrected again): ~8 new listings/second**,
  measured directly from `listed_at` timestamps on a single live page
  (50 records spanning a 6-second window) — roughly **600,000
  listings/day**. Both earlier figures on this line (~6.5/s from a
  3-second burst, and ~0.89/s from a 30-minute run) were measured on a
  poller that was silently dropping >99% of the stream (see "Data
  collected before this fix is unreliable" at the top of this file) --
  neither is trustworthy. This measurement instead comes from a direct,
  one-shot page fetch compared against what was already in the DB (40 of
  50 already known, 10 new), independent of the buggy pagination loop
  entirely. At ~8/s, a 15-second poll interval means ~120 new listings
  per cycle, comfortably more than two pages (50 each) -- pagination
  within an iteration is not optional headroom, it is required just to
  keep up.
- **Rate limits are CONFIRMED DIFFERENT per endpoint, and both have
  large headroom at the corrected throughput.** `/nfts/search`:
  `x-ratelimit-limit: 2`, `x-ratelimit-reset: 0`, no `Retry-After`. The
  floors endpoint: `x-ratelimit-limit: 5`, `x-ratelimit-reset: 12` —
  a different, more generous limit and a longer reset window. Actual
  usage in the 30-minute run: 144 search + 95 floor requests over 1802s
  ≈ 0.13 req/s combined — multiple times under either limit. Because the
  limits differ per endpoint, `portals_client.py` throttles **per path**,
  not with one shared clock — two requests to different endpoints never
  wait on each other, only two requests to the *same* endpoint do
  (`_throttle(path)`, tested in `test_per_path_throttle.py`).
  `REQUEST_DELAY_MS=400` (2.5 req/s) was measured to still trigger 429s
  on `/nfts/search` under load; the default is `600` (~1.67 req/s per
  path), with a hard floor of 500 in `config.py`. `portals_client.py`
  reads `x-ratelimit-remaining` on every successful response and, if it's
  `0`, adds one extra `REQUEST_DELAY_MS` pause on that path before the
  next request to it — backing off *before* getting 429'd, not just
  after. On an actual 429, wait-source priority is `Retry-After` (not yet
  observed live, but honored if a well-behaved response sends it) >
  `x-ratelimit-reset` (if > 0) > exponential backoff from 2s.
- Given the corrected throughput and confirmed headroom,
  `POLL_INTERVAL_SEC` default is lowered from 15 to **5** (measured
  median detection delay drops from ~3.8s to ~1.3s at this interval); the
  floor is now 3, a residual safety margin rather than a measured limit.
- In the 30-minute run, 173 of 1609 listings (n=1436 used for the delay
  calculation) had no `listed_at` value at all. `poller.py`'s run summary
  now includes a `listings_without_listed_at` counter so this doesn't
  silently shrink the sample size unnoticed.
- `GET /collections/models/backgrounds/floors?models=Emperor,Jackpot` → 200,
  body `{"model_backgrounds": {"<model>": {"<backdrop>": "<price>"}}}`.
  `collection_id` / `short_name` params are ignored (byte-identical
  response with/without them). `backdrops` param is also ignored — all
  backgrounds always returned.
- **The floors endpoint silently drops model names it doesn't
  recognize** — a batch of 20 or 50 requested names came back with only
  19 keys both times, with no error. `floors.py` batches in chunks of
  `FLOORS_BATCH_SIZE`, diffs the response against the request, and
  retries missing names individually once before giving up
  (`floor_skip_reason="model_not_returned"`).
- **Floor batch size ceiling confirmed: `models=30` → 200 OK, all 30 keys
  returned; `models=60` → 400 Bad Request.** Whether the limit is on
  count or URL length wasn't isolated (and doesn't matter practically).
  `FLOORS_BATCH_SIZE` defaults to `25` (headroom under the confirmed-good
  30) with a hard ceiling of 30 in `config.py`. A larger batch matters a
  lot under a confirmed 2-req/window rate limit: it directly divides the
  number of floor requests needed.
- Without `Origin` / `Referer` / `User-Agent` headers, the floors endpoint
  resets the TCP connection instead of returning 403.
- `price`, `floor_price`, and floor values are decimal strings, sometimes
  with float artifacts (`"4.079999995"`). Must go through `decimal.Decimal`
  only, never `float`.
- `total_count` is unreliable (observed `0` with 2 non-empty `results`) —
  never used as a pagination stop condition or volume estimate. Empty
  `results` is the only valid end-of-page signal.
- Rarity values (`rarity_per_mille` field) are fractional (`1`, `0.7`,
  `1.5`, `0.2`) and likely percentages, not actual per-mille integers.
- **Unit reconciliation passed**: a sampled listing had `price=7.87`,
  `floor_price=4.3`, and the independently-fetched `combo_floor=4.71` —
  all the same order of magnitude, in the same ordinary units. No
  cross-unit mismatch between listing fields and the floors endpoint.
- `tg_id` field: observed live: `name="Ice Cream"` -> `tg_id="IceCream-45374"`,
  `name="Pretty Posy"` -> `tg_id="PrettyPosy-57354"` (spaces stripped; the
  rule for apostrophes and other punctuation is unknown). `parsing.py`
  does NOT attempt to reconstruct `tg_id` when missing — a wrong guess
  produces a broken deep link, worse than a missing one. A missing
  `tg_id` becomes `None` with a WARNING logged (`external_id` included).
- `/market/config` fields observed: `commission=0.02`, `offer_fee=0.01`,
  `withdrawal_fee=0.35`, `usd_course=1.42`, `cooldown=60000000000`.
  **`user_cashback` changed from `0.05` to `0` within the same day, and
  separately `usd_course` changed from `1.42` to `1.43` within the same
  day** — two independent confirmations that these values are NOT
  constants. This is why `poller.py` fetches
  `/market/config` at startup and every `CONFIG_REFRESH_SEC`, and writes
  a new `market_config_snapshots` row only when a value actually changed.
  `report.py` prints every distinct snapshot in the collection period and
  warns if more than one was observed, since the profit formula uses a
  single env-configured fee value for the whole period.

### Assumptions (NOT independently confirmed — do not treat as fact)
- `RARITY_SCALE_TO_PM=10`: assumed conversion factor from the raw
  `rarity_per_mille` field to actual per-mille. Unconfirmed.
- `FLOOR_CACHE_TTL_SEC=600`: taken from `staleTime: 6e5` observed in the
  frontend JS bundle. This is a guess about the frontend's own cache
  behavior, not a confirmed fact about how fast the underlying floor
  actually changes.
- `MARKETPLACE_FEE_RATE=0.02` is read from a real `commission` field in
  `/market/config` (confirmed above) — but whether `commission` is
  specifically a *sale-time* fee (vs. a listing fee, or something applied
  elsewhere) is still an interpretation, not confirmed by documentation.
- `WITHDRAWAL_FEE_FLAT=0.35` matches the observed `withdrawal_fee` field —
  same caveat: the field's existence and value are confirmed, its exact
  semantics (flat fee per withdrawal, in what circumstances) are assumed.
- Domain confirmation is via the frontend JS bundle's `apiUrl` string
  plus live authorized 200 responses with correctly-shaped bodies — **not**
  via direct observation of live mini-app network traffic. A stronger
  confirmation (opening the real mini-app inside Telegram and reading
  `window.location` / the Network tab from there) is still recommended
  before scaling usage.
- `FLOOR_MIN_PRICE=8`: not from a live measurement, but from a breakeven
  calculation using the confirmed fee values above. At `commission=0.02`
  and `withdrawal_fee=0.35`: a $5 profit at a 20% discount needs a combo
  floor around 21 units; at a 35% discount, around 12 units. Listings
  priced under ~8 units can't clear a meaningful profit bar even at a
  generous discount, and a floor lookup is the most rate-limit-expensive
  operation in the pipeline — so it's skipped below this price
  (`floor_skip_reason="below_price_threshold"`), while the listing itself
  is still always written to `listings`. The same threshold is now also
  used to decide whether `raw` is stored (see "Storage volume" below) —
  this reuse is a size/simplicity tradeoff, not something separately
  justified by storage economics on its own.

## Storage volume

At ~600k listings/day (see throughput above), storing the full `raw`
JSON blob for every listing is a meaningful, avoidable cost at scale.
`raw` is now `NULL` for listings
priced under `FLOOR_MIN_PRICE_NANO`; every parsed column (price,
attributes, rarity, timestamps, etc.) is still populated for **all**
listings regardless of price, so nothing needed for throughput stats or
the name-collision backfill is lost. `report.py` prints DB file size and
the count of listings with `raw` retained, in its header.

### Step 0 result table (all rows confirmed, none outstanding)

| Question | Result | Confirmed? |
|---|---|---|
| Working `sort` param value | default (no param), `sort=latest`, and `sort=listed_at desc` all give identical, correctly-descending order | YES |
| Max `limit` accepted by `/nfts/search` | server caps at 50 regardless of requested value | YES |
| Max number of `models` accepted per `/collections/models/backgrounds/floors` call | 30 works (200, all keys returned); 60 fails (400 Bad Request) | YES |
| `/market/config` fee field names + values | commission=0.02, offer_fee=0.01, withdrawal_fee=0.35, usd_course=1.42→1.43 (changed same-day), cooldown=60000000000, user_cashback varies (0.05→0) | YES (values are NOT constants) |
| Observed throughput | ~8 new listings/sec (50 records, 6-second listed_at span) ≈ 600k/day. Earlier figures (~6.5/s, ~0.89/s) were measured on a poller silently dropping >99% of the stream — not trustworthy, see top of file | YES |
| Rate limit behavior | DIFFERENT per endpoint: /nfts/search limit=2, reset=0, no Retry-After; floors endpoint limit=5, reset=12. Request counts recorded under the pre-fix collection bug are not representative of real usage; re-measure after this fix | YES (limits); usage numbers need re-measurement |

## Units discipline (do not violate)

- Any field ending in `_nano` is an integer, already scaled by `10**9`,
  safe to use directly in arithmetic against other `*_nano` fields.
- Any field without that suffix (env vars, raw API values) is in ordinary
  currency units and **must** be converted via `money.to_nano()` /
  `config.WITHDRAWAL_FEE_FLAT_NANO` / `config.FLOOR_MIN_PRICE_NANO` before
  it touches `*_nano` arithmetic. `MARKETPLACE_FEE_RATE` is the one
  exception — it's a dimensionless ratio, never scaled.
- `decimal.Decimal` only for anything price-shaped. `float` is never used
  in the price/floor/fee chain.

## Profit formula caveat

`report.py` assumes a listing can actually be sold at `pair_floor_nano`
with no slippage. In practice, especially in the low-liquidity rare
segments this project targets, selling quickly often requires listing at
or below the current floor. Treat the profit numbers in the report as an
**optimistic upper bound**, not a guaranteed execution price. On top of
that, `/market/config` fees are confirmed to change over time
(`user_cashback` did, mid-day; `usd_course` did too, separately) — if
`report.py` shows more than one `market_config_snapshots` row for the
period, the single env-configured fee values used in the profit formula
are an approximation across however many real fee regimes were actually
in effect. Unlike the retired `own_confidence` thresholds, `pair_floor.py`
does not gate rows by liquidity — `report.py` instead breaks the discount
distribution down by `pair_listed_count` (1 / 2-3 / 4-9 / 10+) so a
one-listing floor stays visible as exactly that: a real market fact with
a thin order book behind it, not a filtered-out low-confidence estimate.

## FAST PATH / ANALYTICS PATH split

The root cause of the >99% listing loss (see top of file) had two parts,
both fixed here:

1. **Pagination exit condition.** `poll_once()` used to stop scanning a
   page the instant it hit a known `external_id`, on the assumption that
   known and new items would be neatly separated by `listed_at` sort
   order. Confirmed live, they are not -- known and new items are
   interleaved within a page. A page is now **always processed in
   full**; pagination only advances to the next page if the current one
   contained at least one new item, and stops only when a page is
   entirely known (caught up) or genuinely empty (real end of stream).
   `MAX_PAGES_PER_ITERATION` remains as a hard safety cap.
2. **Floor requests inside the collection loop.** Even with pagination
   fixed, doing a `pair_floor` (or API combo-floor) network call for
   every qualifying listing *inside* the page-processing loop stretched
   each iteration badly enough, at ~8 listings/sec, to fall behind the
   stream. The collection loop (**FAST PATH**, `poll_once` /
   `_process_page_items`) now does nothing but fetch pages and write
   listings -- every `FloorSnapshot` is written with
   `pair_floor_status="pending"` and zero floor-related network calls.
   A separate **ANALYTICS PATH** (`Poller.run_floor_worker()`, invoked on
   its own `FLOOR_WORKER_INTERVAL_SEC` timer, default 30s) later selects
   up to `FLOOR_BATCH_PER_RUN` (default 20) `pending` listings priced at
   or above `FLOOR_MIN_PRICE_NANO`, computes API/own/pair floors for
   them, and updates the row in place. Listings below the price
   threshold are resolved to `pair_floor_status="no_data"` immediately
   and for free (no network call, not counted against the per-run
   budget) since they'd never be selected otherwise and would sit
   `pending` forever. Listings the worker hasn't reached yet stay
   `pending` and are picked up on a later run — `report.py` and the
   poller's run summary (`floor_pending_count`) both surface this
   backlog rather than hiding it.

The run summary also now reports `items_seen_total` and
`items_already_known` alongside `new_listings` — this is what makes a
regression like the one described at the top of this file visible
immediately (a large gap between "seen" and "written" is the symptom),
instead of silently invisible the way it was before this fix.

## COLLECT_MIN_PRICE: narrowing the collection loop

Fixing pagination (above) was necessary but not sufficient. Confirmed
live, sustained: Portals' 2-requests/sec limit on `/nfts/search`, combined
with ~92% of every unfiltered page being already-known records, caps the
poller's actual new-listing throughput at **~1.3/sec** (measured: 792 new
listings over 616 seconds) — it costs roughly 0.7 requests per net-new
listing just to walk past everything already seen. Against a real stream
of **~8 listings/sec**, that ceiling means **the full stream can never be
kept up with**, no matter how well pagination behaves.

Confirmed live, page-timing measurements at three price floors:

| `min_price` | records/window | rate |
|---|---|---|
| none | 50 / 6.5s | ~7.7/s |
| 15 | 50 / 25.3s | ~2.0/s |
| 30 | 49 / 80.9s | ~0.6/s |

At `min_price=15` the segment's own rate (~2/s) fits comfortably inside
the ~1.3-2/s ceiling the rate limit allows. `min_price=8` was also
measured and gives **no savings at all** — the segment above 8 is nearly
the whole stream (50 records/6.5s, same as unfiltered) — so filtering
that low is pointless; `COLLECT_MIN_PRICE` refuses values between 0 and
10 for this reason (see `config.py`).

**`COLLECT_MIN_PRICE` (default 15) is sent as a `min_price` filter on
every `/nfts/search` call the collection loop makes.** Listings priced
below it **never enter the database at all** — not as a row with `raw`
nulled, not in any form. This is a deliberate tradeoff: full
completeness of the whole market is abandoned in exchange for complete,
representative visibility of the segment that actually matters, instead
of an unrepresentative ~16% sample of everything. A defensive check in
`poller.py` also drops (and counts, `collect_filtered_count`) any
listing the server returns below the threshold anyway, in case the
server-side filter is ever imperfect.

**This is a different threshold from `FLOOR_MIN_PRICE`.**
`COLLECT_MIN_PRICE` decides what is collected at all; `FLOOR_MIN_PRICE`
(now also defaulting to 15, up from 8) decides what gets a floor
computed once it's already in the DB. Setting `FLOOR_MIN_PRICE` below
`COLLECT_MIN_PRICE` is meaningless (nothing that cheap was ever
collected), and `config.py` logs a `WARNING` at import time if that
happens.

**The floor worker also needed to catch up again** after this change:
even with the stream narrowed to ~2/s, the previous
`FLOOR_WORKER_INTERVAL_SEC=30` / `FLOOR_BATCH_PER_RUN=20` ceiling (40
floors/min) wasn't enough (measured: `floor_pending_count` at 130 and
climbing against only 18 `floor_requests` for 792 new listings). Defaults
raised to `FLOOR_WORKER_INTERVAL_SEC=10` / `FLOOR_BATCH_PER_RUN=30` (180
floors/min ceiling). The pair-floor cache means actual network calls are
fewer than listings processed whenever several pending listings share a
`(collection_id, model_name, backdrop_name)` triple within
`PAIR_FLOOR_CACHE_TTL_SEC` — the run summary's `floor_cache_hits` (sum of
both the API-floor cache and the pair-floor cache) makes this visible.
`floor_pending_trend` (`growing`/`stable`/`shrinking`, compared between
the start and end of the run) and a `WARNING: floor worker is not
keeping up` line (printed only if `floor_pending_count` never decreased
across the whole run) surface whether the worker is actually keeping
pace.

## Price history: tracking known-listing price changes

Previously, any `external_id` already in the DB was pure dead weight --
seen, counted as "known", and discarded. Confirmed live that this threw
away most of the market's actual dynamics: of 239 listings observed
across repeated feed appearances, 109 had no price change, 55 went up,
and **60 went down**. Examples of drops: `24.99 -> 24.95` (0.16%),
`22.99 -> 22.98` (0.04%) -- machine-step relister-bot noise -- and
`67.91 -> 65.93` (2.9%) -- a real, human-scale price cut.

**`listed_at` is NOT a signal of anything by itself** -- confirmed live,
a lot's `listed_at` can jump forward by nearly a full day with the exact
same price. Price comparison is the only thing that decides whether
something changed; `listed_at` is stored alongside a real price change
for reference, never used to detect one.

For every already-known listing on a page, `poller.py` now compares the
page's price against `listings.price_nano`:
- unchanged -> nothing written, exactly as before;
- changed -> a row in the new `price_history` table (schema v4:
  `listing_external_id`, `old_price_nano`, `new_price_nano`,
  `delta_pct`, `is_noise`, `old_listed_at`, `new_listed_at`,
  `observed_at`), and `listings.price_nano`/`listed_at` are updated to
  match.

`PRICE_DROP_MIN_PCT` (default `1.0`) separates a real repricing from
bot noise: a drop is still always recorded (it's a real observed price
change, never discarded), but flagged `is_noise=True` if
`abs(delta_pct) < PRICE_DROP_MIN_PCT` -- the 0.16%/0.04% examples above
are exactly what this threshold is tuned to catch; the 2.9% example is
exactly what it's meant to let through as signal.

**Price-change detection does NOT alter the pagination stop
condition.** A page entirely composed of known ids still stops
pagination, even if every single one of those known listings had a
price change recorded -- prices change constantly, so if a
changed-but-known page counted as "still going", the poller would
paginate forever.

`report.py`'s `=== price drops ===` block is a **second, independent
signal type**, alongside the existing "new listing below floor" signal,
not a replacement for it. See the next section for how it filters
artifacts before computing anything.

The run summary also gained `price_changes_seen`, `price_drops`,
`price_raises`, and `price_drops_above_threshold`.

## Price drop artifact filtering

Three sequential filters, each drawn from the REMAINDER of the previous
one (see "Price-drop signals before this fix were mostly artifacts" at
the top of this file for the concrete examples each one targets):

1. **`is_anomaly`** (set by `poller.py` at write time, never by
   `report.py`): a single-step drop bigger than `PRICE_DROP_MAX_PCT`
   (default 60%), OR part of a burst -- more than one drop on the same
   listing within `PRICE_DROP_BURST_SEC` (default 60s) of each other.
   Burst detection is retroactive: `db.flag_burst_drops()` re-flags the
   EARLIER row too, since it only looked like an isolated signal at the
   time it was written. Confirmed live: Jelly Bunny #2627, 999->99->29
   within 10 seconds.
2. **`is_ladder`** (set by `report.py`'s `backfill_ladder()`, idempotent,
   same pattern as `name_collision`): a listing with
   `>= LADDER_MIN_DROPS` (default 3) drops within the last
   `LADDER_WINDOW_HOURS` (default 24) gets ALL of its `price_history`
   rows flagged -- a relister bot's mechanical walk-down is a property of
   the LOT's behavior, not of one row. Confirmed live: Low Rider #23134,
   6 steps of exactly 5%, ~30 minutes apart.
3. **`is_noise`** (unchanged from the previous delivery): below
   `PRICE_DROP_MIN_PCT`.
4. **Floor `no_data` at drop time**: `floor_at_drop_nano IS NULL` --
   either the drop was noise (floor was never re-fetched, see below), or
   the listing was alone in its pair even AFTER self-exclusion (see next
   section) -- there was nothing left to compare it to. Confirmed live:
   Nail Bracelet #4695 (195->150) and 3 more of the same shape, where the
   "floor" was the listing's own pre-drop price.

What survives all four filters is a **CLEAN signal**. `report.py` prints
the filter cascade with a running count, a drop-size distribution
(1-3%/3-10%/10-25%/>25%) and profit for clean signals only, AND -- this
is the part that actually surfaces artifacts rather than hiding them
inside an aggregate -- up to 20 clean signals **printed one per line**
(collection, model, backdrop, gift number, old price, new price, floor,
`pair_listed_count_excl_self`, timestamp). Aggregates alone hid all three
artifact types; only the line-by-line dump made them visible during
manual review.

## Self-exclusion: a lot cannot be its own floor

Confirmed via the same manual review: `pair_floor_nano` (both the
existing new-listing signal and, before this delivery, the price-drop
signal) **includes the listing's own price** in the floor computation. A
listing alone in its pair -- or the current cheapest -- is therefore
compared against itself. `pair_floor.py`'s `search_pair_floor()` /
`PairFloor` now take a **mandatory** `exclude_external_id` and compute
`floor_excluding_self_nano` from the order-book response with that
listing's own entry removed BEFORE taking the minimum;
`self_was_floor=True` records when the exclusion actually changed the
result. If nothing is left after exclusion, `status="no_data"` -- a
correct, unremarkable outcome (a lot with no other active listing in its
pair simply cannot be signaled against), not an error.

This required a cache redesign: `PairFloorCache` used to cache the
*computed floor* per `(collection_id, model_name, backdrop_name)`, but
exclusion is per-listing, so two listings in the same pair need
different excluding-self results from the SAME order-book response. The
cache now stores the **raw response**, and exclusion is computed
per-call from it at no extra network cost -- cache hits are unaffected.
A separate `get_fresh()` bypasses the TTL entirely (still updating the
cache for subsequent `get()` calls): used only when recording a price
drop above `PRICE_DROP_MIN_PCT`, so the floor at that moment is as
current as possible rather than whatever was cached up to
`PAIR_FLOOR_CACHE_TTL_SEC` ago.

`floor_snapshots` keeps the OLD `pair_floor_nano`/`pair_listed_count`
columns (including self) for diagnostics/comparison, alongside the new
`pair_floor_excl_self_nano`/`pair_listed_count_excl_self`/
`pair_self_was_floor` -- the main new-listing discount distribution in
`report.py` now reads exclusively from the `_excl_self` columns.
Expected consequence, per the task that drove this change: signal counts
drop, since a large fraction of previous "signals" were self-comparisons
(measured: 38 of a prior 51 signals had `pair_listed_count=1`) --
**this is correct**, not a regression.

## Offline backfill (backfill.py)

```
python -m gift_sniper.backfill --db gift_sniper.db
```

Attempts to fill `pair_floor_excl_self_nano` / `pair_listed_count_excl_self`
/ `pair_self_was_floor` for `floor_snapshots` rows written before
self-exclusion existed, from the already-saved `raw_model_block` -- no
network calls, no data deleted, idempotent. As established above, under
the current schema this recomputes **0 rows** (prints why for every row)
-- `raw_model_block` is backdrop-keyed, not per-listing, so there is
nothing to exclude. The script still runs the real reconstruction logic
against a hypothetical per-listing shape (tested in `test_backfill.py`),
in case a future code path ever populates `raw_model_block` that way.

`report.py`'s price-drops block already tolerates a missing
`floor_at_drop_nano` by falling back to `floor_snapshots.pair_floor_excl_self_nano`
(labeled `floor_source="snapshot"` vs `"at_drop"` in the per-line output
and the `clean signal floor source: at_drop=N snapshot(backfilled)=M`
summary line) -- so if `backfill.py` (or a later live ANALYTICS PATH
run) ever does populate that field for an old row, it becomes usable
automatically, with no further code change.

## Schema versioning

`db.connect()` runs migrations automatically: a `schema_version` table
tracks the applied version, and each migration (see `MIGRATIONS` in
`db.py`) is idempotent -- safe to re-run, checks column existence before
`ALTER TABLE`. If `schema_version` has no row yet (a DB from before this
system existed), the version is detected from the actual columns present
via `PRAGMA table_info`, never guessed. An unrecognized column layout
raises `db.SchemaError` with an actionable message instead of silently
picking a version. `build_default_poller()` also calls
`db.verify_schema()` right after connecting -- before any network
request -- so a still-mismatched schema is caught with a clear message
at startup, not mid-write on the first batch. Current version is **5**
(v4 added the `price_history` table; v5 added the self-exclusion floor
columns on `floor_snapshots` and the drop-time-floor/ladder/anomaly
columns on `price_history`). Since `price_history` was a brand-new table
rather than new columns on an existing one, `connect()` runs
`_migrate()` (detection + any needed `ALTER TABLE`s) **before** running
the rest of `SCHEMA`'s `CREATE TABLE IF NOT EXISTS` statements -- doing
it the other way around would let this same connect() call's own
table-creation contaminate a legacy pre-versioning DB's fallback
detection (making it look like `price_history` already existed, and
therefore skipping the real `floor_snapshots` column migrations it still
needed).

## What this module does NOT do

Alerts, user filters, Telegram bot, second marketplace, Pyrogram /
authData auto-refresh (separate phase), sale tracking (separate phase).

## Running

```
export PORTALS_AUTH=...           # required, never commit this
export DB_DSN=gift_sniper.db      # optional
python -m gift_sniper.poller      # runs forever, Ctrl+C to stop (prints a summary on exit)

# Bounded acceptance run (e.g. the 30-minute live test), stops itself and
# prints the summary automatically:
python -m gift_sniper.poller --run-seconds 1800

python -m gift_sniper.report --db gift_sniper.db --usd-rate 1.42
```

`1.42` is the `usd_course` value observed in `/market/config` on the date
of data collection; the rate moves, always pass the current one, never
reuse this example value blindly (it has already been observed to move
to `1.43` within a single day).

The run summary (printed on Ctrl+C or when `--run-seconds` elapses)
includes: duration, iterations, `collect_min_price` (the value actually
in effect), pages fetched, items seen total, items already known,
`collect_filtered_count`, new listings, listings/sec, search requests,
floor requests, `floor_cache_hits`, 429 count, preemptive-pause count,
pagination-cap hits, WAF_RESET count, parse errors, detection-delay
median/p90, floor-pending count, and `floor_pending_trend`
(growing/stable/shrinking) with a `WARNING: floor worker is not keeping
up` line if it never decreased across the run. `items_seen_total` vs.
`items_already_known` vs. `new_listings` is the diagnostic that would
have caught the >99% loss bug immediately — watch for `new_listings`
being suspiciously small relative to `items_seen_total -
items_already_known` on a live run.

## Tests

```
python -m pytest gift_sniper/tests -v
```

108 tests, all passing as of this delivery. Newest
(`test_backfill.py`, plus new cases in `test_price_drops_report.py`): a
listing with 4 noise-level drops (0.05% each) is NOT a ladder; 3
significant drops (5% each) IS one; 10 noise ticks + 3 significant drops
on the same listing is a ladder counted as exactly 3 (not 13) at the
cascade's ladder stage, while all 13 rows still get `is_ladder=1` in the
DB; a row with `floor_at_drop_nano=NULL` but a populated
`pair_floor_excl_self_nano` is still usable, `floor_source="snapshot"`.
`backfill.py`: the real (backdrop-keyed) `raw_model_block` shape always
leaves rows NULL with the reason counted; a hypothetical per-listing
shape is correctly reconstructed (including the "excluded listing was
the pre-exclusion floor" case and the "excluded listing was the only one"
no-data case); idempotent. Previous delivery
(`test_schema_regression.py`, `test_lazy_portals_auth.py`): every
historical schema version (1-4) migrates via the real `db.connect()`
path and survives report.py's actual query plus poller.py's actual
insert; `report.py`/`config.py` import cleanly in a subprocess with no
`PORTALS_AUTH` set at all, while `AuthManager()` still raises
`ConfigError` when actually constructed without one. Previous delivery
(`test_price_drop_anomalies.py`, plus new cases in `test_pair_floor.py`,
`test_price_drops_report.py`, `test_migrations.py`): self-exclusion --
excluding the cheapest of 3 listings correctly promotes the next-cheapest
and sets `self_was_floor=True`; a lot alone in its pair (the excluded one
being the only listing) gives `status="no_data"`; `PairFloorCache` caches
the RAW response so two listings in the same pair get correctly different
excluding-self results at no extra network cost; `get_fresh()` bypasses
and refreshes the TTL cache. A drop above `PRICE_DROP_MIN_PCT` triggers a
real `get_fresh()` floor re-fetch (`floor_at_drop_nano` populated); a
noise-level drop uses a client that raises `AssertionError` if
`search_pair_floor` is called at all, proving it never is. A single-step
90% drop is flagged `is_anomaly`; 30% is not. Two drops on the same
listing 10 seconds apart both end up `is_anomaly` -- including the
EARLIER row, retroactively flagged by `db.flag_burst_drops()`. Ladder
detection (`backfill_ladder()`, `LADDER_MIN_DROPS=3` default): three
5%-ish steps flags all three rows `is_ladder` and excludes them from
clean signals; two steps do not qualify; a second `report.py` run
produces byte-identical output (idempotency). Migration 4→5 adds the
self-exclusion and anomaly/ladder columns without touching pre-existing
`listings`/`floor_snapshots`/`price_history` rows. Previous delivery's
tests (`test_price_history.py`: a lower price on an already-known
listing writes a correctly-signed `delta_pct` and updates
`listings.price_nano`; an unchanged price, even with `listed_at`
"touched", writes nothing; a known listing with `price=null` is skipped
without error; a page entirely of known listings with real price changes
still stops pagination; `test_collect_min_price.py`: `min_price`
ordering and omission, the below-threshold defensive drop, the
`COLLECT_MIN_PRICE > FLOOR_MIN_PRICE` `WARNING`, pair-floor cache hits;
`test_pagination.py::test_whole_page_processed_even_when_known_and_new_are_interleaved`
failing on the old "break on first known id" logic by construction;
`test_fast_path_no_floor_requests.py` proving `poll_once` makes zero
floor-related network calls; `test_floor_worker.py`'s ANALYTICS PATH
batch limit and pending→resolved transitions) and everything further
back (parsing, dedup, rate-limit retry and wait-source priority,
preemptive pause, floor-batch retry-once, `raw` nulling, market-config
snapshotting, `--run-seconds`, own-floor's collection-scoped grouping,
`floor_sanity` classification, per-path throttling, pair-floor
unlisted/null-price filtering and caching, `/nfts/search` parameter
order, schema migrations 1→2→3→4, name collisions, blocked-gift
exclusion, tg_id fallback removal, step0's token-safety guard) still pass
unchanged. Step 0 is fully confirmed against the live API — no remaining
"NOT RUN" items.

**Live sanity-check for THIS delivery (price history): NOT YET
PERFORMED.** The task requires a 10-minute live run expecting
`price_changes_seen` in the hundreds (measured standalone: ~115 changes
per 239-listing feed pass), `price_drops` comparable to `price_raises`,
and a non-empty `price_history` table; `price_changes_seen == 0` means
this fix isn't working. This session has no live `PORTALS_AUTH` token:
```
PORTALS_AUTH=<token> python -m gift_sniper.poller --run-seconds 600
```
Send the run summary output (specifically `price_changes_seen`,
`price_drops`, `price_raises`) before considering this delivery closed.
