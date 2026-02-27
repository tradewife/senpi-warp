# senpi-state

Shared state repository for the Senpi hybrid trading agent. Bridges a $5/mo VPS (high-frequency mechanical execution) with Warp Oz cloud agents (strategic LLM decisions).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     VPS ($5/mo)                         │
│  Real cron jobs — no LLM, near-zero latency             │
│                                                         │
│  60s   emerging-movers-cron.py  → signals + auto-entry  │
│  3min  dsl-combined-cron.sh     → trailing stop exits   │
│  5min  sm-flip-cron.sh          → conviction collapse   │
│  5min  watchdog-cron.sh         → margin/liq monitoring  │
│  10min health-check-cron.sh     → state validation      │
│  30s   risk-arbiter.py          → hard safety limits    │
│                                                         │
│  Writes state → git push                                │
└────────────────────────┬────────────────────────────────┘
                         │  senpi-state repo (this repo)
                         │  git pull ↔ git push
┌────────────────────────┴────────────────────────────────┐
│               Oz Cloud Agents (Warp)                    │
│  Scheduled LLM runs — strategic decisions               │
│                                                         │
│  15min  Trade Evaluator    → score + enter/skip         │
│  1hr   Regime Classifier  → RISK_ON/BASELINE/RISK_OFF  │
│  6hr   Portfolio Review   → risk rails + reporting      │
│  daily  HOWL              → nightly self-improvement    │
│  daily  Whale Index       → copy-trade rebalance        │
│                                                         │
│  Reads state → decides → writes config → git push       │
└─────────────────────────────────────────────────────────┘
```

## Why Hybrid?

Oz cloud agents are ephemeral tasks billed per run. Running a 60-second EM scan as a cloud agent would cost ~960 runs/day and burn through monthly credits in hours. The Senpi Python scripts need 1-2 API calls and zero LLM tokens — they belong on a VPS cron. Oz cloud agents handle the decisions that benefit from LLM reasoning at 15min+ intervals.

Result: **faster** than pure-cloud (VPS auto-entry in <2s vs 30s+ cloud agent bootstrap) at **1/50th the credit cost**.

## Directory Layout

```
senpi-state/
├── config/                    # Shared configuration (both layers read/write)
│   ├── risk-regime.json       # Current regime: RISK_ON/BASELINE/RISK_OFF
│   ├── scanner-config.json    # Thresholds, auto-entry rules, disqualifiers
│   └── wolf-strategies.json   # Strategy registry (wallets, budgets, slots)
├── state/                     # Runtime state (VPS writes, Oz reads)
│   ├── {strategy-key}/        # Per-strategy DSL state files
│   │   └── dsl-{ASSET}.json   # Position trailing stop state
│   ├── pending-entries.json   # Signals queued for Oz evaluation
│   └── scan-history.json      # Last 60 EM scans for momentum tracking
├── memory/                    # Cumulative knowledge (both layers write)
│   ├── MEMORY.md              # Persistent agent context
│   ├── howl-*.md              # Nightly HOWL reports
│   └── trade-journal.json     # All trades with entry source tags
├── outputs/                   # Reports and arbiter state
│   ├── latest-report.json     # Last portfolio review
│   └── arbiter-state.json     # Risk arbiter peak/drawdown tracking
└── scripts/
    ├── lib/senpi_common.py    # Shared Python library
    ├── vps/                   # VPS cron job scripts
    │   ├── provision-vps.sh   # One-shot VPS setup
    │   ├── emerging-movers-cron.py
    │   ├── risk-arbiter.py
    │   ├── dsl-combined-cron.sh
    │   ├── sm-flip-cron.sh
    │   ├── watchdog-cron.sh
    │   └── health-check-cron.sh
    └── oz/                    # Oz cloud agent setup
        └── setup-oz-agents.sh # Creates environment, secrets, schedules
```

## Setup

### 1. Create this repo on GitHub (private)

```bash
gh repo create senpi-state --private
git remote add origin git@github.com:YOUR_USER/senpi-state.git
git push -u origin main
```

### 2. Provision a VPS

Any $5/mo Ubuntu VPS (Hetzner, DigitalOcean, Linode, etc.):

```bash
# On the VPS (as root):
export SENPI_STATE_REPO="git@github.com:YOUR_USER/senpi-state.git"
curl -sL https://raw.githubusercontent.com/YOUR_USER/senpi-state/main/scripts/vps/provision-vps.sh | bash
```

Then create `/opt/senpi/.env`:
```
SENPI_API_KEY=your_senpi_api_key
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. Set up Oz cloud agents

On your local machine (with `oz` CLI installed):

```bash
export SENPI_API_KEY="..."
export SENPI_STATE_REPO="github.com/YOUR_USER/senpi-state"
bash scripts/oz/setup-oz-agents.sh
```

### 4. Add a trading strategy

You need a funded Senpi strategy wallet. Create one via mcporter:

```bash
mcporter call senpi strategy_create_custom_strategy --json '{"budgetUsd": 2000}'
```

Then register it:

```bash
# Edit config/wolf-strategies.json to add the strategy
# Or use the wolf-setup.py script from senpi-skills
```

### 5. Fund and go

Send USDC to the strategy wallet address. The VPS starts scanning immediately; Oz agents kick in on their schedules.

## Operations

### Monitor

```bash
# VPS logs
ssh vps 'tail -f /var/log/senpi/em.log'

# Oz cloud agent runs
oz run list
oz run get <run-id>

# Current state
cat config/risk-regime.json | jq .riskMode
cat memory/trade-journal.json | jq '.[-5:]'
```

### Emergency stop

```bash
# From anywhere with oz CLI:
oz agent run-cloud --environment $ENV_ID \
  --prompt 'EMERGENCY: Close ALL positions via mcporter. Set risk-regime.json to RISK_OFF. Commit and push.'
```

Or edit `config/risk-regime.json` directly and push — the VPS reads it every cycle.

### Adjust aggression

Edit `config/risk-regime.json` to change regime params, or `config/scanner-config.json` to tune entry thresholds. Push to git — VPS picks up changes within 10 minutes (or immediately on next health check).

## Credit Budget

| Oz Agent | Frequency | Est. Credits/Run | Monthly |
|---|---|---|---|
| Trade Evaluator | 96/day | 15 | ~1,440 |
| Regime Classifier | 24/day | 10 | ~240 |
| Portfolio Review | 4/day | 20 | ~80 |
| HOWL | 1/day | 30 | ~30 |
| Whale Index | 1/day | 25 | ~25 |
| **Total** | | | **~1,815** |

To trim: reduce Trade Evaluator to every 30min → ~1,095 total. VPS auto-entry on FIRST_JUMPs covers the gap.

## Key Design Decisions

- **VPS auto-enters on FIRST_JUMP** without waiting for Oz. Speed is edge. Oz reviews within 15min and can override.
- **Risk Arbiter is not an LLM.** Mechanical safety should never depend on a language model or cloud credits.
- **DSL-Tight by default.** 4-tier ROE locks (5/10/15/20%) with stagnation TP. Aggressive but disciplined.
- **Git as state bus.** Simple, auditable, works offline. Both layers can read/write independently.
- **HOWL only auto-applies risk-reducing changes.** Risk increases require manual approval.
