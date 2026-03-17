# senpi-waifu

Autonomous hybrid trading agent for crypto perpetual futures. Runs on a $5/mo Railway container (mechanical execution) with optional Oz cloud agents (strategic LLM decisions). Fully controllable from Telegram.

## How It Works

Two layers, one repo. The **mechanical layer** runs every 30-60 seconds with zero LLM cost — scanning markets, managing trailing stops, and enforcing safety limits. The **strategic layer** runs LLM agents on longer intervals for regime classification, trade evaluation, and self-improvement.

```
┌─────────────────────────────────────────────────────────┐
│              Railway Container ($5/mo)                   │
│                                                         │
│  worker.py (APScheduler)        dashboard/server.py     │
│  ┌────────────────────┐         ┌──────────────────┐    │
│  │ 60s  🐋 ORCA       │         │ FastAPI dashboard │    │
│  │ 5min 🦎 KOMODO     │         │ Telegram bot      │    │
│  │ 3min 🔒 DSL HW     │         │ Oz dispatch       │    │
│  │ 5min 🔄 SM Flip    │         └──────────────────┘    │
│  │ 5min 👁 Watchdog   │                                 │
│  │ 10m  🏥 Health     │         All /commands from       │
│  │ 15m  📊 Arena      │         Telegram land here       │
│  │ 30s  🚨 Arbiter    │                                 │
│  └────────────────────┘                                 │
│                                                         │
│  Writes state → git push                                │
└────────────────────┬────────────────────────────────────┘
                     │  senpi-waifu repo (this repo)
                     │  git pull ↔ git push
┌────────────────────┴────────────────────────────────────┐
│              Oz Cloud Agents (Warp)                      │
│                                                         │
│  15min  Trade Evaluator    → validate scanner signals   │
│  1hr    Regime Classifier  → RISK_ON/BASELINE/RISK_OFF  │
│  4hr    Arena Learner      → study winning predators    │
│  6hr    Portfolio Review   → risk rails + reporting     │
│  daily  HOWL               → nightly self-improvement   │
│  daily  Whale Index        → copy-trade rebalance       │
│                                                         │
│  Reads state → decides → writes config → git push       │
└─────────────────────────────────────────────────────────┘
```

## Telegram Bot

The bot runs inside the dashboard service and gives you full control from your phone. Type `/` in the chat to see all commands.

**Status & Monitoring**
| Command | What it does |
|---|---|
| `/status` | Regime, open positions, daily PnL, equity, drawdown, arbiter status |
| `/positions` | Each position's direction, asset, entry, leverage, DSL tier, high-water mark, breach count |
| `/trades` | Last 10 trades with PnL, close reason (DSL trailing stop, Phase 1 timeout, stagnation TP, etc.) |
| `/equity` | Current equity, day start, peak, drawdown %, proximity to safety limits |
| `/regime` | Active regime parameters: slots, leverage, allocation, guardrails |
| `/pending` | Queued scanner signals awaiting Oz review or already auto-entered |

**Control**
| Command | What it does |
|---|---|
| `/risk_on` | Max 3 slots, 7-10x leverage, 35% allocation. Use when trend is clear |
| `/risk_off` | Block all new entries. Existing positions managed by DSL trailing stops |
| `/baseline` | Default balanced regime: 2 slots, 30% allocation, 60s loss cooldown |
| `/flatten` | Emergency close ALL positions (sets RISK_OFF + dispatches Oz agent) |

**Manual Triggers**
| Command | What it does |
|---|---|
| `/scan` | Run ORCA dual-mode scanner now (STALKER + STRIKER) |
| `/komodo` | Run KOMODO momentum event consensus scanner now |
| `/arbiter` | Run Risk Arbiter safety checks now |
| `/health` | Run health check + git sync now |
| `/arena` | Run arena monitor now (polls Senpi Predators leaderboard) |

**Reports**
| Command | What it does |
|---|---|
| `/howl` | Last HOWL nightly report (win rates, scanner comparison, fee drag, arena benchmarking) |
| `/journal` | Lifetime stats: total PnL, win rate, profit factor, breakdown by entry source |
| `/arena_insights` | Top 5 predator strategies, winning/losing traits, recommendations |

Any non-command text is dispatched to an Oz cloud agent as a free-text prompt.

## Scanners

### 🐋 ORCA — Dual-Mode Emerging Movers (every 60s)

Two entry modes, both with hardcoded safety gates:

**STALKER mode** — detects smart money accumulation before the crowd notices. Looks for assets climbing the leaderboard over 3+ consecutive scans with building contribution. Score ≥6 to enter. Uses ALO (maker) orders for ~60-80% fee reduction.

**STRIKER mode** — catches violent first-jump breakouts. Requires 15+ rank jump with 1.5x volume confirmation. Score ≥9 to enter. Uses MARKET orders for speed.

### 🦎 KOMODO — Momentum Event Consensus (every 5min)

Five-gate entry model using real-time momentum threshold crossings ($2M+/$5.5M+/$10M+ delta PnL):

1. **Momentum events** → 2+ unique traders crossing thresholds on same asset/direction
2. **Trader quality** → TCS must be Elite or Reliable, TAS not Degen, concentration ≥0.4
3. **Market confirmation** → 5+ SM traders on the asset
4. **Volume confirmation** → 1h volume ≥ 0.5x of 6h average
5. **Regime filter** → counter-trend penalty (-3), aligned bonus (+1)

Score ≥10 to enter. ALO orders for fee savings.

### 🔒 DSL High Water Mode (every 3min)

7-tier infinite trailing stop that locks increasingly large percentages of peak ROE:

| Trigger | Lock |
|---|---|
| +5% ROE | 20% of high water |
| +10% | 40% |
| +20% | 55% |
| +30% | 70% |
| +50% | 80% |
| +75% | 85% |
| +100% | 90% |

No ceiling. This is the geometry that lets positions hold +200% winners while protecting gains.

**Phase 1** (proving period): Conviction-scaled tolerance. Score ≥10 gets 30 min hard timeout and -30% ROE floor. Score 6-7 gets 15 min and -20%. Stagnation TP fires at 10% ROE if high water hasn't moved in 45 min.

**Phase 2** (trailing): High Water tiers above. Position rides the trend indefinitely.

## Safety Architecture

### 🚨 Risk Arbiter (every 30s)

Mechanical safety net. No LLM dependency. Checks:
- **Daily loss limit** (10%) → sets RISK_OFF
- **Catastrophic drawdown** (20% from peak) → flattens ALL positions + RISK_OFF
- **Consecutive stop-outs** (4 in 2 hours) → sets RISK_OFF

### Hardcoded Gates

These are enforced in Python code, not config. Agents cannot override them:

| Gate | Why |
|---|---|
| XYZ equities banned | Net negative across all 22 live agents (Fox SNDK -$57) |
| 7-10x leverage only | Sub-7x can't overcome fees, >10x blows up (Dire Wolf 25x lesson) |
| Max 3 positions | Concentration beats diversification across all agents |
| 10% daily loss limit | Fox's 10% > Vixen's 25% — tighter limit bled 2.5x less |
| 2hr per-asset cooldown | Prevents re-entry after Phase 1 exit (PAXG double-entry lesson) |
| Stagnation TP mandatory | Positions that peaked then reversed to zero (Mantis lesson) |

## Directory Layout

```
senpi-waifu/
├── config/                    # Shared configuration (both layers read/write)
│   ├── risk-regime.json       # RISK_ON / BASELINE / RISK_OFF + guardrails
│   ├── scanner-config.json    # ORCA + KOMODO thresholds (gates are in code)
│   └── wolf-strategies.json   # Strategy registry (wallets, budgets, slots)
├── state/                     # Runtime state (VPS writes, Oz reads)
│   ├── {strategy-key}/        # Per-strategy DSL state files
│   │   └── dsl-{ASSET}.json   # Position state (High Water Mode)
│   ├── pending-entries.json   # Signals queued for Oz evaluation
│   ├── orca-scan-history.json # Last 40 ORCA scans
│   ├── orca-cooldowns.json    # Per-asset 2hr cooldown tracking
│   ├── komodo-events.json     # KOMODO momentum event history
│   └── komodo-cooldowns.json  # KOMODO per-asset cooldowns
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
│   ├── server.py              # FastAPI dashboard + Telegram bot
│   ├── telegram_bot.py        # Telegram command handlers + Oz dispatch
│   └── templates/index.html   # Mobile-first web dashboard
├── scripts/
│   ├── lib/senpi_common.py    # Shared Python library
│   ├── vps/                   # Cron job scripts (run by worker.py)
│   │   ├── orca-scanner-cron.py     # 🐋 ORCA dual-mode scanner
│   │   ├── komodo-scanner-cron.py   # 🦎 KOMODO momentum events
│   │   ├── risk-arbiter.py          # 🚨 Mechanical safety
│   │   ├── arena-monitor.py         # 📊 Arena performance tracker
│   │   ├── reconcile-closes.py      # Trade journal close reconciler
│   │   ├── dsl-combined-cron.sh     # 🔒 DSL High Water runner
│   │   ├── sm-flip-cron.sh          # 🔄 SM flip detector
│   │   ├── watchdog-cron.sh         # 👁 Watchdog
│   │   ├── health-check-cron.sh     # 🏥 Health check + git sync
│   │   └── provision-vps.sh         # One-shot VPS setup (alt deploy)
│   └── oz/
│       ├── setup-oz-agents.sh       # Creates Oz environment + schedules
│       └── agent-init.sh            # Runtime init for each Oz agent
├── worker.py              # APScheduler — replaces crontab for Railway
├── Dockerfile             # Python 3.11 + Node + git + mcporter
├── railway.toml           # Railway deployment config
└── requirements.txt       # Python dependencies
```

## Deployment (Railway)

### 1. Push this repo to GitHub

```bash
gh repo create senpi-waifu --private
git remote add origin git@github.com:YOUR_USER/senpi-waifu.git
git push -u origin main
```

### 2. Create Railway services

Create two services from the same GitHub repo in Railway dashboard:

| Service | Start Command | Purpose |
|---|---|---|
| `senpi-worker` | `python3 worker.py` | Runs all scanners + safety checks |
| `senpi-dashboard` | `uvicorn dashboard.server:app --host 0.0.0.0 --port $PORT` | Web dashboard + Telegram bot |

### 3. Set environment variables

Both services:
```
SENPI_API_KEY        = <Senpi MCP authentication token>
GITHUB_TOKEN         = <GitHub fine-grained token, Contents read/write>
GITHUB_REPO          = YOUR_USER/senpi-waifu
SENPI_STATE_DIR      = /app
SENPI_SKILLS_DIR     = /opt/senpi/senpi-skills
TELEGRAM_BOT_TOKEN   = <from @BotFather>
TELEGRAM_CHAT_ID     = <your chat ID>
```

Dashboard service only:
```
WARP_API_KEY         = <Warp API key for Oz dispatch>
OZ_ENVIRONMENT_ID    = <from setup-oz-agents.sh>
DASH_TOKEN           = <secret for web dashboard auth>
```

### 4. Add a trading strategy

```bash
mcporter call senpi strategy_create_custom_strategy --json '{"budgetUsd": 2000}'
```

Register the returned strategy in `config/wolf-strategies.json`, fund the wallet with USDC, and the scanners start hunting immediately.

### 5. Set up Oz cloud agents (optional)

```bash
export SENPI_API_KEY="..."
export GITHUB_TOKEN="..."
export SENPI_WAIFU_REPO="github.com/YOUR_USER/senpi-waifu"
bash scripts/oz/setup-oz-agents.sh
```

## Alternative Deployment (VPS)

For a standalone $5/mo VPS instead of Railway:

```bash
export SENPI_WAIFU_REPO="git@github.com:YOUR_USER/senpi-waifu.git"
curl -sL https://raw.githubusercontent.com/YOUR_USER/senpi-waifu/main/scripts/vps/provision-vps.sh | bash
```

Create `/opt/senpi/.env` with your secrets, then verify: `crontab -l && tail -f /var/log/senpi/orca.log`

## Cost

### Railway ($5/mo Hobby plan)

| Resource | Usage | Cost |
|---|---|---|
| CPU | ~0.07 vCPU avg | ~$1.44/mo |
| RAM | ~220 MB avg | ~$2.20/mo |
| Egress | ~1 GB (API + Telegram + git) | ~$0.05/mo |
| **Total** | | **~$3.69/mo** |

Fits within the $5 included credit. Worst case under heavy manual triggering: ~$6-7/mo.

### Oz Cloud Agents (optional, billed by Warp)

| Agent | Frequency | Est. Credits/Run | Monthly |
|---|---|---|---|
| Trade Evaluator | 96/day | 15 | ~1,440 |
| Regime Classifier | 24/day | 10 | ~240 |
| Arena Learner | 6/day | 15 | ~90 |
| Portfolio Review | 4/day | 20 | ~80 |
| HOWL | 1/day | 30 | ~30 |
| Whale Index | 1/day | 25 | ~25 |
| **Total** | | | **~1,905** |

## Key Design Decisions

- **Hybrid architecture.** VPS handles mechanical execution (sub-2s entry) at zero LLM cost. Cloud agents handle strategic decisions at 15min+ intervals. 1/50th the credit cost of pure-cloud.
- **ORCA dual-mode.** STALKER catches accumulation before explosions. STRIKER catches the explosion itself. Both modes were missed by single-mode scanners.
- **KOMODO momentum events.** Real-time threshold crossings, not stale position data. Fixes the fundamental data source bug that killed Scorpion (-24.2%).
- **DSL High Water Mode.** No ceiling on trailing stops. The geometry that lets positions hold +200% winners.
- **Fee optimisation.** STALKER and KOMODO use ALO (maker) orders for ~60-80% fee reduction. Fee drag is the #1 killer across all 22 agents.
- **Risk Arbiter is not an LLM.** Mechanical safety should never depend on a language model or cloud credits.
- **Git as state bus.** Simple, auditable, works offline. Both layers read/write independently.
- **Telegram-first control.** Full monitoring and manual override from your phone. Free-text messages dispatch to Oz.
- **Arena-informed learning.** Oz studies 24 competing predator strategies and auto-applies risk-reducing improvements.
- **HOWL only auto-applies risk-reducing changes.** Risk increases require manual approval.

## Emergency Stop

From Telegram:
```
/flatten
```

Or from anywhere:
```bash
# Edit directly and push:
echo '{"riskMode":"RISK_OFF",...}' > config/risk-regime.json
git commit -am "RISK_OFF" && git push
```

The VPS reads `risk-regime.json` every cycle and will stop all new entries immediately.
