# senpi-warp

Shared state repository for the Senpi ORCA hybrid trading agent. Bridges a $5/mo VPS (high-frequency mechanical execution) with Warp Oz cloud agents (strategic LLM decisions). Synced to the latest [senpi-skills](https://github.com/Senpi-ai/senpi-skills) and informed by the [Senpi Predators arena](https://strategies.senpi.ai/).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     VPS ($5/mo)                             │
│  Real cron jobs — no LLM, near-zero latency                 │
│                                                             │
│  60s   🐋 ORCA Scanner      → dual-mode (STALKER+STRIKER)  │
│  5min  🦎 KOMODO Scanner    → momentum event consensus      │
│  3min  🔒 DSL v5 HW         → High Water infinite trailing  │
│  5min  🔄 SM Flip           → conviction collapse           │
│  5min  👁 Watchdog          → margin/liq monitoring          │
│  10min 🏥 Health Check      → state validation + git sync   │
│  15min 📊 Arena Monitor     → track predator performance    │
│  30s   🚨 Risk Arbiter      → hard safety limits            │
│                                                             │
│  Writes state → git push                                    │
└────────────────────────┬────────────────────────────────────┘
                         │  senpi-state repo (this repo)
                         │  git pull ↔ git push
┌────────────────────────┴────────────────────────────────────┐
│               Oz Cloud Agents (Warp)                        │
│  Scheduled LLM runs — strategic decisions                   │
│                                                             │
│  15min  Trade Evaluator    → ORCA/KOMODO signal validation  │
│  1hr   Regime Classifier  → RISK_ON/BASELINE/RISK_OFF      │
│  4hr   Arena Learner      → study winning predators         │
│  6hr   Portfolio Review   → risk rails + arena comparison   │
│  daily  HOWL              → nightly self-improvement        │
│  daily  Whale Index       → copy-trade rebalance            │
│                                                             │
│  Reads state → decides → writes config → git push           │
└─────────────────────────────────────────────────────────────┘
```

## What Changed (ORCA Hybrid Upgrade)

| Component | Before | After |
|---|---|---|
| Scanner | EM v3.1 (FIRST_JUMP only) | ORCA dual-mode (STALKER + STRIKER) + KOMODO momentum events |
| DSL | DSL-Tight (fixed % tiers) | DSL v5.3.1 High Water Mode (up to 90% of peak, 7-tier infinite trailing) |
| Entry gates | Config-based | Hardcoded in scanner code (agent cannot override) |
| XYZ equities | Allowed | Banned at scan level (net negative across all 22 agents) |
| Leverage | 7-15x | 7-10x hard cap (Dire Wolf 25x lesson) |
| Stagnation TP | Optional | Mandatory (10% ROE / 45 min) |
| Asset cooldown | None | 2-hour per-asset after Phase 1 exit |
| Arena | None | VPS polls performance tracker; Oz learns from winning strategies |
| Daily loss limit | 5% | 10% (Fox's setting — Vixen at 25% bled 2.5x more) |

## Hardcoded Lessons (in the code, not instructions)

These gates are enforced in `orca-scanner-cron.py` and cannot be changed via config:

| Lesson Source | What Happened | Gate |
|---|---|---|
| Fox SNDK -$57 | XYZ equities are noise | Filtered at scan parse level |
| Dire Wolf 25x blowup | Agent raised leverage after losses | 7-10x hard cap in scanner output |
| Vixen daily loss 25% | Agent raised limit, bled 2.5x more | 10% daily loss in constraints |
| PAXG double-entry | Re-entered after Phase 1 cut | 2-hour per-asset cooldown |
| Mantis removed stagnation TP | Positions peaked then reversed to zero | Stagnation TP mandatory |
| Ghost Fox 740 trades | More trades = more churn = more fees | Max 3 positions |

## Why Hybrid?

Oz cloud agents are ephemeral tasks billed per run. Running a 90-second ORCA scan as a cloud agent would cost ~960 runs/day. The VPS handles mechanical execution with 1-2 API calls and zero LLM tokens. Oz handles decisions that benefit from LLM reasoning at 15min+ intervals.

Result: **faster** than pure-cloud (VPS auto-entry in <2s vs 30s+ cloud agent bootstrap) at **1/50th the credit cost**.

## Arena Integration

The VPS arena monitor polls [strategies.senpi.ai](https://strategies.senpi.ai/) every 15 minutes via the performance tracker API. It writes `outputs/arena-state.json` with leaderboard rankings, top performer details, and computed insights.

The Oz Arena Strategy Learner (every 4 hours) reads this data and compares our performance against the 24 Senpi Predators. It generates data-driven recommendations: which entry mode is working, whether to tighten scores, how our fee drag compares, etc. Only risk-reducing changes are auto-applied.

## Directory Layout

```
senpi-warp/
├── config/                    # Shared configuration (both layers read/write)
│   ├── risk-regime.json       # Current regime: RISK_ON/BASELINE/RISK_OFF
│   ├── scanner-config.json    # ORCA + KOMODO thresholds (hardcoded gates documented)
│   └── wolf-strategies.json   # Strategy registry (wallets, budgets, slots)
├── state/                     # Runtime state (VPS writes, Oz reads)
│   ├── {strategy-key}/        # Per-strategy DSL state files
│   │   └── dsl-{ASSET}.json   # Position state (High Water Mode)
│   ├── pending-entries.json   # Signals queued for Oz evaluation
│   ├── orca-scan-history.json # Last 40 ORCA scans (~60 min)
│   ├── orca-cooldowns.json    # Per-asset 2hr cooldown tracking
│   ├── komodo-events.json     # KOMODO momentum event history
│   ├── komodo-cooldowns.json  # KOMODO per-asset cooldowns
│   └── komodo-entries.json    # Daily KOMODO entry counter
├── memory/                    # Cumulative knowledge (both layers write)
│   ├── MEMORY.md              # Persistent agent context
│   ├── howl-*.md              # Nightly HOWL reports
│   └── trade-journal.json     # All trades with entry source tags
├── outputs/                   # Reports and observational data
│   ├── latest-report.json     # Last portfolio review
│   ├── arbiter-state.json     # Risk arbiter peak/drawdown tracking
│   ├── arena-state.json       # Senpi Predators performance snapshot
│   └── arena-learnings.json   # Arena-derived strategy recommendations
├── dashboard/
│   ├── server.py              # FastAPI dashboard + chat
│   └── templates/
└── scripts/
    ├── lib/senpi_common.py    # Shared Python library
    ├── vps/                   # VPS cron job scripts
    │   ├── provision-vps.sh   # One-shot VPS setup
    │   ├── orca-scanner-cron.py     # 🐋 ORCA dual-mode scanner
    │   ├── komodo-scanner-cron.py   # 🦎 KOMODO momentum events
    │   ├── arena-monitor.py         # 📊 Arena performance tracker
    │   ├── risk-arbiter.py          # 🚨 Mechanical safety
    │   ├── dsl-combined-cron.sh     # 🔒 DSL High Water runner
    │   ├── sm-flip-cron.sh          # 🔄 SM flip detector
    │   ├── watchdog-cron.sh         # 👁 Watchdog
    │   ├── health-check-cron.sh     # 🏥 Health check + close reconciliation
    │   ├── reconcile-closes.py      # 📒 Trade journal close reconciler
    │   └── emerging-movers-cron.py  # (legacy — replaced by ORCA)
    └── oz/
        └── setup-oz-agents.sh # Creates environment, secrets, schedules
```

## Setup

### 1. Create this repo on GitHub (private)

```bash
gh repo create senpi-warp --private
git remote add origin git@github.com:YOUR_USER/senpi-warp.git
git push -u origin main
```

### 2. Provision a VPS

Any $5/mo Ubuntu VPS (Hetzner, DigitalOcean, Linode, etc.):

```bash
# On the VPS (as root):
export SENPI_STATE_REPO="git@github.com:YOUR_USER/senpi-warp.git"
curl -sL https://raw.githubusercontent.com/YOUR_USER/senpi-warp/main/scripts/vps/provision-vps.sh | bash
```

Then create `/opt/senpi/.env`:
```
SENPI_API_KEY=your_senpi_api_key
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. MCP Setup

The provisioning script auto-configures mcporter with the Senpi MCP server at `https://mcp.prod.senpi.ai`. To manually configure or refresh:

```bash
source /opt/senpi/.env
mcporter config add senpi --command npx \
  --env SENPI_AUTH_TOKEN="$SENPI_API_KEY" \
  -- mcp-remote "https://mcp.prod.senpi.ai/mcp" \
  --header "Authorization: Bearer ${SENPI_AUTH_TOKEN}"
```

If the token expires, refresh via:
```bash
curl -s -X POST http://127.0.0.1:8080/setup/api/senpi-token \
  -H "Content-Type: application/json" \
  -d '{"token": "NEW_TOKEN"}'
```

### 4. Set up Oz cloud agents

```bash
export SENPI_API_KEY="..."
export SENPI_STATE_REPO="github.com/YOUR_USER/senpi-warp"
bash scripts/oz/setup-oz-agents.sh
```

### 5. Add a trading strategy

```bash
mcporter call senpi strategy_create_custom_strategy --json '{"budgetUsd": 2000}'
```

Then register in `config/wolf-strategies.json`.

### 6. Fund and go

Send USDC to the strategy wallet address. The VPS ORCA scanner starts hunting immediately; Oz agents kick in on their schedules.

## Operations

### Monitor

```bash
# VPS logs
ssh vps 'tail -f /var/log/senpi/orca.log'    # ORCA scanner
ssh vps 'tail -f /var/log/senpi/komodo.log'   # KOMODO events
ssh vps 'tail -f /var/log/senpi/arena.log'    # Arena tracker

# Oz cloud agent runs
oz run list
oz run get <run-id>

# Current state
cat config/risk-regime.json | jq .riskMode
cat outputs/arena-state.json | jq '.insights'
cat memory/trade-journal.json | jq '.[-5:]'
```

### Emergency stop

```bash
# From anywhere with oz CLI:
oz agent run-cloud --environment $ENV_ID \
  --prompt 'EMERGENCY: Close ALL positions via mcporter. Set risk-regime.json to RISK_OFF. Commit and push.'
```

Or edit `config/risk-regime.json` directly and push — the VPS reads it every cycle.

## Credit Budget

| Oz Agent | Frequency | Est. Credits/Run | Monthly |
|---|---|---|---|
| Trade Evaluator | 96/day | 15 | ~1,440 |
| Regime Classifier | 24/day | 10 | ~240 |
| Arena Learner | 6/day | 15 | ~90 |
| Portfolio Review | 4/day | 20 | ~80 |
| HOWL | 1/day | 30 | ~30 |
| Whale Index | 1/day | 25 | ~25 |
| **Total** | | | **~1,905** |

## Key Design Decisions

- **ORCA dual-mode scanner.** STALKER catches accumulation before explosions (ZEC pattern). STRIKER catches the explosion itself (FARTCOIN pattern). Both modes missed by single-mode scanners.
- **KOMODO momentum events.** Real-time threshold crossings, not stale position data. Fixes the fundamental data source bug that killed Scorpion (-24.2%) and Mantis.
- **DSL High Water Mode.** 7-tier trailing from 20% to 90% of peak ROE. No ceiling. The geometry that lets FOX hold +200% winners.
- **Fee optimisation.** STALKER and KOMODO entries use ALO (maker) orders for ~60-80% fee reduction. STRIKER uses MARKET for speed. Fee drag is the #1 killer across all 22 agents.
- **Hardcoded gates.** XYZ ban, 7-10x leverage, stagnation TP — enforced in Python code, not agent instructions. Agents can't drift.
- **Arena-informed decisions.** Oz learns from 24 competing predator strategies in real-time. Data-driven self-improvement, not blind tuning.
- **Risk Arbiter is not an LLM.** Mechanical safety should never depend on a language model or cloud credits.
- **Git as state bus.** Simple, auditable, works offline. Both layers can read/write independently. Global git lock prevents concurrent pushes.
- **HOWL only auto-applies risk-reducing changes.** Risk increases require manual approval.
- **Trade close reconciliation.** Health check reconciles DSL closes into the trade journal every 10 minutes, enabling accurate PnL tracking.
