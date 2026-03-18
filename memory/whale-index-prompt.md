# Whale Index Manager — Daily Rebalance Procedure

Version-controlled local prompt for the Oz Whale Index manager.
Read this file at the start of every Whale Index run.

## Goal

Run a daily mirror-trader portfolio review and rebalance cycle for Discovery traders on Hyperliquid.

This manager should:
- discover top traders,
- score and allocate candidates,
- maintain a watch/swap state machine,
- create or replace mirror strategies when warranted,
- report the portfolio state back through Telegram,
- commit any state/config changes to git.

## Required State Files

- `outputs/whale-index-state.json`
  Use this as the persistent state file for Whale Index.
  If missing, create it with a sensible bootstrap structure.

Suggested structure:

```json
{
  "updatedAt": null,
  "riskTolerance": null,
  "budget": null,
  "targetSlots": 0,
  "portfolioValue": 0,
  "slots": [],
  "watchlist": {},
  "notes": []
}
```

Each slot should track at minimum:
- `traderAddress`
- `strategyId`
- `allocationPct`
- `status` (`HOLD`, `WATCH`, `SWAP`)
- `watchCount`
- `createdAt`
- `lastCheckedAt`
- `lastRank`
- `lastConsistency`
- `lastDrawdown`
- `lastActiveAt`

## Step 1: Load Context

1. Run `bash senpi-waifu/scripts/oz/agent-init.sh`
2. `git pull` in the `senpi-waifu` repo
3. Read:
   - `outputs/whale-index-state.json` if it exists
   - this file
   - `memory/MEMORY.md` if needed for user context

## Step 2: Onboarding or Rehydrate Existing Portfolio

If `outputs/whale-index-state.json` does not exist or does not contain an initialized portfolio:

Collect or infer:
- total budget
- risk tolerance: `conservative`, `moderate`, or `aggressive`

Slot count:
- `$500-$2k` → 2 slots
- `$2k-$5k` → 3 slots
- `$5k-$10k` → 4 slots
- `$10k+` → 5 slots

Risk mapping:
- `conservative` → labels `ELITE` only, max leverage `10x`
- `moderate` → labels `ELITE`, `RELIABLE`, max leverage `15x`
- `aggressive` → labels `ELITE`, `RELIABLE`, `BALANCED`, max leverage `25x`

If the budget/risk choice is not discoverable from state, write a note into `outputs/whale-index-state.json` instead of guessing wildly.

## Step 3: Discover Traders

Call:

```text
discovery_top_traders(limit=50, timeframe="30d")
```

Apply hard filters:
- consistency label matches the chosen risk level
- risk label matches the chosen risk level
- minimum 30d track record
- not already in the portfolio for new-candidate selection

## Step 4: Score Candidates

Use the upstream Whale Index score:

```text
score = 0.35 × pnl_rank + 0.25 × win_rate + 0.20 × consistency + 0.10 × hold_time + 0.10 × drawdown
```

Normalize all components to a 0-100 range before weighting.

Also run:
- overlap checks across active positions for the selected set
- flag more than 50% position overlap

Allocation weighting:
- score-weighted allocation
- hard cap of 35% per slot
- renormalize after capping

## Step 5: Create or Confirm Mirror Strategies

For each selected trader, create mirror strategies as needed.

When creating a strategy, include upstream attribution fields:

```json
"skill_name": "whale-index",
"skill_version": "1.0"
```

The mirror execution flow is:
1. `strategy_create_mirror`
2. set strategy-level stop loss
   - `-10%` conservative
   - `-15%` moderate
   - `-25%` aggressive
3. confirm mirroring is active

Record the resulting `strategyId`, trader, allocation, and current status into `outputs/whale-index-state.json`.

## Step 6: Daily Monitoring

Run once daily.

For each active slot:
1. re-fetch trader stats:
   - `discovery_get_trader_state(traderAddress)`
   - `discovery_get_trader_history(traderAddress)`
2. assess health:
   - still top 50 on 30d Discovery?
   - consistency stable or improved?
   - max drawdown within 2x historical average?
   - active in last 48h?

Classify each slot:
- `HOLD` → top 50, consistency stable, actively trading
- `WATCH` → rank 30-50, or one-tier consistency drop, or inactive 24-48h
- `SWAP` → only if all swap criteria below are met

Swap criteria, all must be true:
1. degraded:
   - below rank 50, or
   - consistency fell to `STREAKY`/`CHOPPY`, or
   - inactive 48h+, or
   - drawdown exceeded 2x historical average
2. sustained:
   - `watchCount >= 2`
3. better alternative:
   - replacement candidate scores at least 15% higher
4. slot not already implicitly closed by strategy-level stop

## Step 7: Swap Execution

When a swap is justified:
1. close all positions in the old mirror strategy
2. wait for positions to close
3. close the strategy itself so funds return
4. select one replacement
5. create the replacement mirror strategy
6. update `outputs/whale-index-state.json`

Do not churn on a single bad day.
The two-day watch period is mandatory.

## Step 8: Rebalance Without Swap

If all traders are healthy but slot allocations have drifted materially:
- top up underweight slots from withdrawable balance if feasible
- otherwise write the rebalance recommendation into state and Telegram

## Step 9: Reporting

Send a Telegram summary after each run.

If changes happened, include:
- portfolio value
- each slot’s trader, rank, status, today PnL, total PnL
- any watch transitions
- any swap candidates or executed swaps
- estimated monthly fee drag if available

If nothing changed and all slots are `HOLD`, send a short “all clear” summary.

## Step 10: Persist and Commit

Always:
1. update `outputs/whale-index-state.json`
2. `git add outputs/whale-index-state.json`
3. include any other changed config/state files
4. `git commit -m "whale-index: daily rebalance YYYY-MM-DD"`
5. `git push`

## Constraints

- Prefer replacing only one slot at a time unless multiple slots clearly meet swap criteria.
- Respect the chosen risk profile.
- Do not silently increase aggressiveness.
- Preserve the 2-day watch rule.
- Avoid allocating more than 35% to a single slot.
