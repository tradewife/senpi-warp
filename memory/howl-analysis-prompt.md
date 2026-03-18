# HOWL v2 — Hunt, Optimize, Win, Learn

Nightly analysis procedure for the senpi-waifu hybrid trading agent.
Run at 23:55 UTC. Read this file at the start of every HOWL session.

## Data Sources

Gather all of these before starting analysis:

- `memory/trade-journal.json` — last 24h of trades (filter by `recordedAt`)
- `state/*/dsl-*.json` — all active DSL state files
- `state/orca-scan-history.json` — ORCA scanner signal history
- `state/komodo-events.json` — KOMODO momentum event history
- `config/risk-regime.json` — current regime and guardrails
- `config/scanner-config.json` — ORCA + KOMODO thresholds
- `config/condor-config.json` — CONDOR configuration
- `config/barracuda-config.json` — BARRACUDA configuration
- `config/bison-config.json` — BISON configuration
- `outputs/arena-state.json` — Senpi Predators performance snapshot
- `outputs/arbiter-state.json` — Risk arbiter peak/drawdown tracking
- `outputs/cron-heartbeats.json` — cron job health
- `memory/howl-*.md` — last 3 HOWL reports (for drift detection)

## Analysis Pillars

### 1. Core Metrics

- Total trades (opened + closed)
- Win rate (% of closed trades with positive PnL)
- Gross profit factor (gross wins / gross losses, before fees)
- Net profit factor (net wins / net losses, after fees)
- Average win size vs average loss size
- Largest win, largest loss

### 2. Scanner Source Breakdown

Break down ALL metrics by entry source tag (`entrySource` field):

| Source | Trades | Wins | Losses | Win Rate | Net PnL | Avg Hold |
|--------|--------|------|--------|----------|---------|----------|
| ORCA STALKER | | | | | | |
| ORCA STRIKER | | | | | | |
| KOMODO | | | | | | |
| CONDOR | | | | | | |
| BARRACUDA | | | | | | |
| BISON | | | | | | |

Flag any scanner with <30% win rate across 5+ trades. Recommend disabling if net-negative.

### 3. Monster Trade Dependency

Sort closed trades by absolute net PnL descending.

- What % of total gross PnL came from the top 3 trades?
- If >80%: **strategy is dependent on outliers. Flag this.**
- Report: "Without top 3: net PnL would be +/- $X"

This reveals whether the strategy survives on skill or luck.

### 4. Fee Drag Ratio (FDR)

Compute:
- `cumulativeFees` = sum of all fees paid (entry + exit) in the period
- `FDR` = cumulativeFees / account start value × 100
- Gross PF vs Net PF comparison

If gross PF > 1.0 but net PF < 1.0: **"The problem is trade count, not trade quality. Say this explicitly."**

FDR thresholds:
- < 5%: healthy
- 5-10%: elevated, consider reducing scan frequency
- > 10%: **critical — over-trading is killing a net-profitable strategy**

### 5. Rotation Cost Tracking

A rotation = closing a position and immediately reopening another (same wallet, <5 min gap).

- Count rotation trades in the period
- Total rotation cost: ~$65 per rotation (close fee ~$32 + reopen fee ~$32)
- Net rotation PnL: did the new position beat the old one's trajectory?
- If net rotation PnL < total rotation cost: **rotations are value-destroying**

### 6. Holding Period Buckets

Bucket every closed trade by duration:

| Bucket | Trades | Win Rate | Net PnL | Avg PnL |
|--------|--------|----------|---------|---------|
| < 30 min | | | | |
| 30-90 min | | | | |
| 90 min - 4h | | | | |
| > 4h | | | | |

Flag the worst-performing bucket. If <30 min trades are net-negative, recommend extending Phase 1 timeouts.

### 7. Direction Regime Detection

Separate all trades by direction (LONG vs SHORT):

- Win rate per direction
- Net PnL per direction
- If one direction has <30% win rate across 5+ trades: **"Stop taking {direction}s — regime mismatch"**

Do not wait for 0-for-12 to surface. Flag early.

### 8. DSL Tier Distribution

How many closed trades reached each tier:

| Exit Point | Count | Avg PnL |
|------------|-------|---------|
| Phase 1 floor | | |
| Phase 1 hard timeout | | |
| Phase 1 weak peak cut | | |
| Phase 1 dead weight | | |
| Stagnation TP | | |
| Tier 0 breach | | |
| Tier 1 breach | | |
| Tier 2 breach | | |
| Tier 3+ breach | | |

If >60% of trades exit in Phase 1: entry quality is poor, consider raising min scores.
If most Tier 0 exits are profitable: Phase 2 trigger ROE may be too low.

### 9. Arena Benchmarking

Read `outputs/arena-state.json`. Compare our metrics against the top 5 Senpi Predators:

- Our win rate vs top 5 average
- Our profit factor vs top 5 average
- What scanners/strategies are they using that we aren't?
- What holding times are they targeting?

### 10. Drift Detection

Read the last 3 HOWL reports from `memory/howl-*.md`.

If the **same recommendation appears in 3+ consecutive reports** and has NOT been implemented:
- Escalate to `RECURRING ⚠️` status
- List: "[change] — suggested N consecutive days, not yet implemented"

This catches chronic issues that keep getting deferred.

## Output Format

### Report File

Save to `memory/howl-YYYY-MM-DD.md` with sections:

```
# HOWL Report — YYYY-MM-DD

## Summary
[2-3 sentence executive summary]

## Core Metrics
[table]

## Scanner Breakdown
[table]

## Monster Trades
Top 3 trades: +$X (X% of total gross PnL)
[list them]
Without top 3: net PnL would be +/- $X

## Fee Drag
FDR: X% | Gross PF: X.XX | Net PF: X.XX
[assessment]

## Rotation Analysis
Rotations: X | Cost: $X | Net outcome: +/- $X

## Holding Periods
[table + flag worst bucket]

## Direction Regime
LONG: X% WR (N trades) | SHORT: X% WR (N trades)
[flag if regime mismatch]

## DSL Distribution
[table]

## Arena Comparison
[our metrics vs top 5]

## Recommendations
### Auto-applied (risk-reducing only)
[list any changes made]

### Requires manual approval (risk-increasing)
[list with rationale]

### Recurring ⚠️ (suggested 3+ consecutive days)
[list with day count]
```

### MEMORY.md Update

Append a 3-line distilled summary to `memory/MEMORY.md`:
```
### HOWL YYYY-MM-DD
[key insight 1]. [key insight 2]. [action taken or recommended].
```

### Telegram Summary

Send a condensed version (~500 chars) with: trades, win rate, net PnL, FDR, top recommendation.

## Auto-Apply Rules

HOWL may auto-apply **ONLY risk-reducing** changes:
- Tighten entry score thresholds (raise minScore)
- Reduce leverage caps
- Increase cooldown durations
- Disable a scanner with <25% win rate and >10 trades

HOWL must **NOT** auto-apply risk-increasing changes:
- Lowering score thresholds
- Increasing leverage
- Adding new entry modes
- Reducing cooldowns

Risk increases go into "Requires manual approval" with supporting data.

## Commit

After generating the report:
1. `git add memory/howl-*.md memory/MEMORY.md config/*.json`
2. `git commit -m "howl: nightly report YYYY-MM-DD"`
3. `git push`
