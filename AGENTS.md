# AGENTS.md — Operating Manual

This document teaches both AI agents and human operators how to use the waifu CLI to operate the trading system.

## Quick Reference

```bash
# Always activate venv first
source venv/bin/activate

# Configuration (first-time setup)
waifu config show                 # View current config
waifu config validate             # Check required vars
waifu config set SENPI_AUTH_TOKEN "your-token"  # Set token

# Read-only checks
waifu status                    # Current state
waifu debug status              # Railway + local health
waifu debug logs -f             # Live logs

# Strategic actions
waifu regime [--dry-run]        # Classify regime
waifu evaluate [--dry-run]      # Process signals
waifu review                    # Portfolio report

# Skill management
waifu dev list-skills           # Browse catalog
waifu dev add-skill orca        # Install skill
```

---

## Commands Deep Dive

### waifu status

**Purpose:** Read-only snapshot of system state.

**Shows:**
- Current regime (RISK_ON/BASELINE/RISK_OFF)
- Effective parameters (slots, leverage, allocation)
- Open positions with ROE
- Pending entries count
- Stale cron detection
- Active alerts

**When to run:** Before any action, to understand current state.

**Example:**
```
$ waifu status
==================================================
  WAIFU STATUS — 2026-03-26T10:00:00Z
==================================================

📊 Regime: BASELINE
   Reason: Mixed signals (slope=0.5%, ATR=2.1%)
   Updated: 2026-03-26T09:00:00Z

⚙️  Effective params:
   Max slots: 2
   Max leverage: 10.0x
   Alloc/slot: 25.0%
   Auto-entry: True
   Entries allowed: True

🛡  Guardrails:
   Leverage: 7-10x
   Max positions: 3
   Daily loss limit: 10%
   Catastrophic DD: 20%
   Cooldown: 120min

📈 Open positions: 1
   ETH LONG (+3.2% ROE)

📋 Pending entries: 2
   BTC via orca
   SOL via komodo

✅ All mechanical crons healthy
==================================================
```

---

### waifu regime

**Purpose:** Classify market regime as RISK_ON / BASELINE / RISK_OFF based on BTC price action.

**Inputs:** Fetches BTC 4h and 1h candles via Senpi MCP.

**Outputs:** Updates `config/risk-regime.json`

**Logic:**
- **RISK_ON:** Strong trend (slope >1.5%), low volatility (ATR <5%)
- **RISK_OFF:** High volatility (ATR >6%) or extreme chop
- **BASELINE:** Everything else (default)

**When to run:** Hourly, or when market conditions change significantly.

**Example:**
```
$ waifu regime
[regime] 2026-03-26T10:00:00Z starting
  BTC MA slope: 2.30%, ATR: 3.50%
  MA_short: 71500, MA_long: 69800
  -> RISK_ON (Clear BULLISH trend (slope=2.3%, ATR=3.5%))
  REGIME CHANGE: BASELINE -> RISK_ON
[regime] 2026-03-26T10:00:05Z done
```

---

### waifu evaluate

**Purpose:** Process pending scanner signals and execute approved trades.

**Inputs:**
- `state/pending-entries.json` — queued signals
- `outputs/autonomous-brain.json` — current policy
- `config/risk-regime.json` — regime check

**Outputs:**
- Executes trades via Senpi MCP
- Updates `memory/trade-journal.json`
- Clears processed entries

**Gate pipeline (10 gates):**
1. Entries allowed by regime
2. Auto-entry enabled
3. Valid strategy ID
4. Slots available
5. Scanner not blocked by brain
6. Score threshold met
7. Asset not banned (XYZ check)
8. Not in cooldown (2hr)
9. Directional exposure OK
10. Leverage clamped to 7-10x

**When to run:** Every 15 minutes. Always `--dry-run` first if uncertain.

**Example:**
```
$ waifu evaluate --dry-run
[evaluate] 2026-03-26T10:00:00Z starting (dry-run)
[evaluate] regime: BASELINE
[evaluate] 3 pending entries
[evaluate] Using strategy: wolf-primary (abc123def456...)
  APPROVE BTC LONG @ 8x (score=12, scanner=orca)
    DRY-RUN: would open BTC LONG @ 8x
  REJECT XYZ: Asset XYZ is BANNED
  REJECT ETH: Score 5 < min 6 for orca
[evaluate] 1 processed, 2 remaining
[evaluate] 2026-03-26T10:00:05Z done
```

---

### waifu review

**Purpose:** Portfolio health check with structured report.

**Shows:**
- Current equity and peak
- Drawdown percentage
- Daily PnL and win rate
- Open positions count
- Dead weight detection (positions that should be closed)
- Guardrail alerts

**Outputs:** `outputs/latest-report.json`, `outputs/arbiter-state.json`

**When to run:** Every 6 hours, or after significant market moves.

**Example:**
```
$ waifu review
[review] 2026-03-26T10:00:00Z starting
  Regime: BASELINE
  Equity: $2,150.00 | Peak: $2,300.00
  Drawdown: 6.5% | Daily PnL: $45.00
  Open: 2 | Daily closes: 4 (75.0% WR)
  DEAD WEIGHT: DOGE (-5.2% ROE, 45min)
[review] 2026-03-26T10:00:05Z done
```

---

### waifu howl

**Purpose:** Nightly 10-pillar self-improvement analysis.

**Pillars:**
1. Core metrics (trades, win rate, profit factor)
2. Scanner source breakdown
3. Monster trade dependency
4. Fee drag ratio
5. Rotation cost tracking
6. Holding period buckets
7. Direction regime (LONG vs SHORT win rates)
8. DSL tier distribution
9. Arena benchmarking
10. Drift detection

**Outputs:**
- `memory/howl-YYYY-MM-DD.md`
- Appends to `memory/MEMORY.md`

**Auto-applies (risk-reducing only):**
- Tighten entry score thresholds
- Reduce leverage caps
- Increase cooldowns
- Disable underperforming scanners (<25% WR, >10 trades)

**Never auto-applies:** Lowering thresholds, increasing leverage, new modes.

**When to run:** Daily at 23:55 UTC.

---

### waifu whale

**Purpose:** Copy-trade portfolio management.

**Process:**
1. Fetch top traders from Senpi Discovery
2. Score candidates (PnL 35%, WR 25%, consistency 20%, hold 10%, DD 10%)
3. Monitor existing slots (HOLD / WATCH / SWAP)
4. Fill empty slots with top candidates

**Outputs:** `outputs/whale-index-state.json`

**When to run:** Daily at 01:00 UTC.

---

### waifu arena

**Purpose:** Study Senpi Predators leaderboard for strategy intelligence.

**Compares:**
- Our win rate vs top 5 predators
- Our trade frequency vs optimal
- Generates actionable recommendations

**Outputs:** `outputs/arena-learnings.json`

**When to run:** Every 4 hours.

---

### waifu emergency-stop

**Purpose:** Immediate RISK_OFF with Telegram alert.

**Actions:**
1. Sets `riskMode: RISK_OFF` in config
2. Sends Telegram alert
3. Commits and pushes

**When to run:** Emergency situations only.

---

## Config Commands

### waifu config show

**Purpose:** Display all configuration values (secrets masked).

**Shows:**
- Required variables (SENPI_AUTH_TOKEN, GITHUB_TOKEN)
- Optional variables with defaults
- Source of each value (.env file, environment, or default)

**Example:**
```
$ waifu config show
=======================================================
  WAIFU CONFIGURATION
  Config file: /home/kt/senpi-waifu/.env
=======================================================

📋 Required:
   SENPI_AUTH_TOKEN: eyJh***...8llig
      (.env)
   GITHUB_TOKEN: gith********JohS
      (.env)

📦 Optional:
   SENPI_WAIFU_DIR: /home/kt/senpi-waifu
   GITHUB_REPO: tradewife/senpi-waifu
   TELEGRAM_BOT_TOKEN: 8660********8Fbs
      (.env)

✅ All required variables set
=======================================================
```

---

### waifu config set

**Purpose:** Set a configuration value (writes to .env file).

**Usage:** `waifu config set <key> <value>`

**Example:**
```
$ waifu config set SENPI_AUTH_TOKEN "eyJhbG..."
✅ Set SENPI_AUTH_TOKEN=eyJh***...8llig
   Written to /home/kt/senpi-waifu/.env
```

---

### waifu config validate

**Purpose:** Check that all required variables are set.

**Example:**
```
$ waifu config validate

📋 Configuration Validation

✅ All required variables are set

⚠️  Warnings:
   - Railway CLI may require login (RAILWAY_TOKEN not set)
```

---

### waifu config export

**Purpose:** Export configuration for Railway deployment.

**Usage:** `waifu config export [--format env|json]`

**Example:**
```
$ waifu config export
SENPI_AUTH_TOKEN="eyJhbG..."
GITHUB_TOKEN="github_pat_..."
GITHUB_REPO="tradewife/senpi-waifu"

# Copy above to Railway dashboard > Variables
```

---

## Debug Commands

### waifu debug logs

Stream Railway deployment logs.

```bash
waifu debug logs              # Last 50 lines
waifu debug logs -n 200       # Last 200 lines
waifu debug logs -f           # Follow mode (tail -f)
waifu debug logs -f --filter ORCA  # Filter to ORCA
```

### waifu debug status

Combined view: Railway deployment status + local health state + heartbeat freshness.

### waifu debug tail <scanner>

Follow logs filtered to a specific scanner:

```bash
waifu debug tail orca     # ORCA scanner only
waifu debug tail arbiter  # Risk arbiter
waifu debug tail brain    # Autonomous brain
```

Valid scanners: orca, mantis, fox, komodo, condor, polar, sentinel, rhino, dsl, arbiter, brain, watchdog, health, arena

### waifu debug deploy --trigger

Trigger a Railway redeploy.

---

## Dev Commands

### waifu dev list-skills

Browse the installable skill catalog:

```bash
waifu dev list-skills
```

Shows skills grouped by category, with installation status (available/installed/configured).

### waifu dev add-skill <name>

Install a skill from the catalog:

```bash
waifu dev add-skill orca
```

Copies config to `config/orca-config.json`. Edit before enabling.

### waifu dev create-skill <name>

Scaffold a new custom skill:

```bash
waifu dev create-skill my-strategy
```

Creates:
- `senpi-skills/my-strategy/SKILL.md`
- `senpi-skills/my-strategy/scripts/my-strategy_scanner.py`
- `senpi-skills/my-strategy/config/my-strategy-config.json`

### waifu dev show-skill <name>

Display a skill's documentation:

```bash
waifu dev show-skill orca
```

---

## Observability Patterns

### Is the system healthy?

```bash
waifu status
waifu debug status
```

Look for: stale crons, alerts, regime mismatches.

### Are trades being executed?

```bash
waifu debug logs -f --filter evaluate
```

Look for: "APPROVE" or "REJECT" lines from evaluate command.

### Is a specific scanner working?

```bash
waifu debug tail orca
```

Look for: "scan #N" lines, signals found.

### What signals are pending?

```bash
waifu status
```

Check "Pending entries" section.

---

## Safety Rules (Non-Negotiable)

These gates are hardcoded in Python. The CLI cannot override them:

| Gate | Value | Reason |
|------|-------|--------|
| XYZ equities | BANNED | Net negative across all agents |
| Leverage | 7-10x only | Sub-7x can't overcome fees, >10x blows up |
| Max positions | 3 | Concentration beats diversification |
| Daily loss limit | 10% | Fox's 10% limit bled 2.5x less than Vixen's 25% |
| Catastrophic DD | 20% | Auto-flatten from peak |
| Per-asset cooldown | 2 hours | Prevents re-entry after Phase 1 exit |
| 4H trend alignment | HARD gate | Never counter-trend |
| Stagnation TP | Mandatory | Positions that peaked then reversed |

---

## Troubleshooting

### "No pending entries" but scanner is running

Scanners may not be finding signals that meet thresholds. Check logs:

```bash
waifu debug tail orca
```

### Trades rejected but score looks good

Check the full gate pipeline. Common rejections:
- Asset in cooldown (2hr)
- Directional exposure cap
- Scanner blocked by brain policy

```bash
waifu evaluate --dry-run  # Shows rejection reasons
```

### Regime stuck in RISK_OFF

Either:
1. Daily loss limit hit (10%) — resets at midnight UTC
2. Manual RISK_OFF — run `waifu regime` to reclassify

### Railway deployment failing

```bash
waifu debug status
waifu debug deploy --trigger
```

Check logs for the deploy:
```bash
waifu debug logs -n 100
```

### CLI can't find files

Ensure SENPI_WAIFU_DIR is set:

```bash
export SENPI_WAIFU_DIR=/home/kt/senpi-waifu
waifu status
```

---

## File Reference

| Path | Purpose |
|------|---------|
| `config/risk-regime.json` | Current regime + guardrails |
| `config/wolf-strategies.json` | Strategy registry |
| `config/*-config.json` | Per-scanner configs |
| `state/pending-entries.json` | Queued signals |
| `state/*/dsl-*.json` | Position DSL state |
| `outputs/autonomous-brain.json` | Brain policy |
| `outputs/latest-report.json` | Last review |
| `outputs/arbiter-state.json` | Peak/DD tracking |
| `memory/trade-journal.json` | All trades |
| `memory/MEMORY.md` | Persistent context |
