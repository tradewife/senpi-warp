# senpi-waifu
Vibe coded fork of senpi-skills for agents trading on hyperliquid perps. Runs on a $5/mo Railway container for deterministic execution, with Hermes (local cron jobs) for strategic supervision. Fully controllable from Telegram.

## How It Works

Three cooperating surfaces, one repo. The **mechanical layer** runs every 30-60 seconds with zero LLM cost — five active scanners (ORCA, KOMODO, CONDOR, SENTINEL, RHINO) hunt for entries, DSL trailing stops manage exits, and the Risk Arbiter enforces safety limits. The **in-container brain layer** runs inside the same Railway/runtime environment and builds a deterministic policy/playbook snapshot from regime, journal, pending signals, arena outputs, and health state. The **Hermes strategic layer** runs LLM agents locally via scheduled cron jobs for regime classification, trade evaluation, and self-improvement. All VPS scripts are native Python with no shell script or LLM dependencies on the hot path.

```
┌─────────────────────────────────────────────────────────┐
│              Railway Container ($5/mo)                   │
│                                                         │
│  worker.py (APScheduler)        dashboard/server.py     │
│  ┌────────────────────┐         ┌──────────────────┐    │
│  │ 60s  🐋 ORCA       │         │ FastAPI dashboard │    │
│  │ 5min 🦎 KOMODO     │         │ Telegram bot      │    │
│  │ 3min 🦅 CONDOR     │         │ Hermes dispatch   │    │
│  │ 3min 🛡 SENTINEL   │         └──────────────────┘    │
│  │ 3min 🦏 RHINO      │                                 │
│  │ [PAUSED] 🎣 BARRACUDA / 🦬 BISON / 🦈 SHARK         │
│  │ 3min 🔒 DSL HW     │         All /commands from       │
│  │ 5min 🧠 Brain      │         Telegram land here       │
│  │ 5min 🔄 Supervisor │                                 │
│  │ 5min 👁 Watchdog   │                                 │
│  │ 10m  🏥 Health     │                                 │
│  │ 15m  📊 Arena      │                                 │
│  │ 30s  🚨 Arbiter    │                                 │
│  └────────────────────┘                                 │
│                                                         │
│  Writes state + playbook → git push                     │
└────────────────────┬────────────────────────────────────┘
                     │  senpi-waifu repo (this repo)
                     │  git pull ↔ git push
┌────────────────────┴────────────────────────────────────┐
│          Hermes Strategic Layer (Local Cron)              │
│                                                         │
│  15min  Trade Evaluator    → validate scanner signals   │
│  1hr    Regime Classifier  → RISK_ON/BASELINE/RISK_OFF  │
│  4hr    Arena Learner      → study winning predators    │
│  6hr    Portfolio Review   → risk rails + reporting     │
│  daily  HOWL               → nightly self-improvement   │
│  daily  Whale Index        → copy-trade rebalance       │
│                                                         │
│  Reads state → advises/updates → writes config → git push│
└─────────────────────────────────────────────────────────┘
```

## Control Model

- **Deterministic hot path.** Entries, exits, risk checks, exposure caps, and position supervision run locally in Python with no LLM dependency.
- **In-container autonomous brain.** `scripts/vps/autonomous-brain.py` runs inside the Railway worker/runtime, synthesizes a policy layer from runtime state, and writes `outputs/autonomous-brain.json`, `outputs/playbook-state.json`, and `outputs/codebase-index.json`.
- **Hermes as supervisory intelligence.** Hermes runs locally as scheduled cron jobs for regime work, trade evaluation, reporting, and higher-order self-improvement. It can influence config and recommendations, but the mechanical layer remains authoritative on the hot path.
- **Git as state bus.** Mechanical state, playbook state, reports, and config changes remain auditable and easy to inspect.

## Railway ↔ Hermes Connection

- **Your laptop is not in the runtime path.** Once deployed, Railway runs the mechanical layer and the in-container brain whether or not your local machine is online.
- **State sync happens through the repo and API calls.** Railway writes state and can push it to GitHub. Hermes agents read that state, update config/reports, and push back. Railway pulls the latest changes during health checks and on scanner cycles.
- **Hermes runs locally on your machine.** The 6 strategic agent roles run as Hermes cron jobs on the local machine, reading/writing state via git pull/push.

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
| `/brain` | Current autonomous brain policy: entry status, caps, preferred scanners, blocked scanners |
| `/pending` | Queued scanner signals with local brain priority context and auto-entry status |

**Control**
| Command | What it does |
|---|---|
| `/risk_on` | Max 3 slots, 7-10x leverage, 35% allocation. Use when trend is clear |
| `/risk_off` | Block all new entries. Existing positions managed by DSL trailing stops |
| `/baseline` | Default balanced regime: 2 slots, 30% allocation, 60s loss cooldown |
| `/flatten` | Emergency close ALL positions (sets RISK_OFF locally + optionally dispatches Hermes) |

**Manual Triggers**
| Command | What it does |
|---|---|
| `/scan` | Run ORCA dual-mode scanner now (STALKER + STRIKER) |
| `/komodo` | Run KOMODO momentum event consensus scanner now |
| `/condor` | Run CONDOR multi-asset alpha hunter now |
| `/sentinel` | Run SENTINEL quality trader convergence scanner now |
| `/rhino` | Run RHINO momentum pyramider now |
| `/barracuda` | ⚠️ PAUSED — BARRACUDA removed from schedule |
| `/bison` | ⚠️ PAUSED — BISON removed from schedule |
| `/shark` | ⚠️ PAUSED — SHARK removed (Senpi paused, -4.3% ROI) |
| `/arbiter` | Run Risk Arbiter safety checks now |
| `/health` | Run health check + git sync now |
| `/arena` | Run arena monitor now (polls Senpi Predators leaderboard) |

**Reports**
| Command | What it does |
|---|---|
| `/howl` | Last HOWL nightly report (win rates, scanner comparison, fee drag, arena benchmarking) |
| `/journal` | Lifetime stats: total PnL, win rate, profit factor, breakdown by entry source |
| `/arena_insights` | Top 5 predator strategies, winning/losing traits, recommendations |

Any non-command text is dispatched to Hermes as a free-text prompt.

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

### 🦅 CONDOR — Multi-Asset Alpha Hunter (every 3min)

Follows a 3-mode lifecycle across BTC, ETH, SOL, HYPE:

1. **SCOUT** — scans 5m/15m/1h/4h candles for momentum, trend structure, and volume breakouts
2. **STALK** — tracks emerging setups for up to 4 hours, waiting for confirmation reload
3. **STRIKE** — enters when score ≥10, with correlation confirmation across paired assets

Gates: SM direction alignment, funding extreme filter, volume ratio spike, multi-timeframe trend structure. Uses fee-optimized limit orders. Conviction-scaled Phase 1 timeouts (30-60 min based on score).

> **⚠️ PAUSED:** BARRACUDA (funding decay), BISON (conviction trend), and SHARK (liquidation cascade, Senpi paused v1.0 -4.3% ROI) have been removed from the active schedule. Code is preserved. Re-enable by uncommenting in `worker.py`.

### 🛡 SENTINEL — Quality Trader Convergence (every 3min)

Inverts the usual momentum pipeline:

1. **Find rising assets first** → leaderboard assets where smart-money contribution is accelerating
2. **Check who is profiting** → momentum events filtered by trader quality tags (`TCS` and `TRP`)
3. **Cross-check top traders** → optional bonus if the asset is already showing up in top trader market sets

SENTINEL is designed to catch the period between "smart money is building in the asset" and "the asset is already obvious on the leaderboard." It is a higher-conviction, lower-frequency scanner that looks for quality-trader convergence rather than raw rank velocity.

### 🦏 RHINO — Momentum Pyramider (every 3min)

Builds into winners instead of entering full size immediately:

1. **SCOUT** → enter 30% of max size on a high-conviction top-10 OI/volume thesis
2. **CONFIRM** → add 40% more at `+10% ROE` if the 4H trend, SM alignment, and volume still hold
3. **CONVICTION** → add the final 30% at `+20% ROE` if the thesis still holds

RHINO uses one DSL High Water state for the full position and prioritizes adds over new entries.

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
- **Abnormal conditions** (API failures, funding spikes) → sets RISK_OFF

### 🧠 Autonomous Brain (every 5min)

Local strategic synthesizer. No execution authority by itself. It reads:
- `config/risk-regime.json`
- `memory/trade-journal.json`
- `state/pending-entries.json`
- `outputs/arena-state.json`
- `outputs/arena-learnings.json`
- `outputs/latest-report.json`
- health / heartbeat outputs

And writes:
- `outputs/autonomous-brain.json` → current policy, scanner priorities, caps, block reasons
- `outputs/playbook-state.json` → normalized execution playbook and scanner profiles
- `outputs/codebase-index.json` → indexed runtime map of the repo

### 🔄 Position Supervisor (every 5min)

Upgrades the old SM flip monitor into a broader deterministic supervisor:
- **Hard smart-money flip** → closes when conviction flips decisively against the position
- **Conviction collapse** → closes when trader participation / conviction / concentration decay below the position’s playbook thresholds
- **Dead-weight rotation** → closes stale losers only when better queued opportunities materially outrank them

This keeps rotation selective instead of churn-heavy.

### Hardcoded Gates

These are enforced in Python code, not config. Agents cannot override them:

| Gate | Why |
|---|---|
| XYZ equities banned | Net negative across all 22 live agents (Fox SNDK -$57) |
| 7-10x leverage only | Sub-7x can't overcome fees, >10x blows up (Dire Wolf 25x lesson) |
| Max 3 positions | Concentration beats diversification across all agents |
| Directional exposure cap | New entries are blocked when projected book concentration breaches the global directional cap |
| 10% daily loss limit | Fox's 10% > Vixen's 25% — tighter limit bled 2.5x less |
| 2hr per-asset cooldown | Prevents re-entry after Phase 1 exit (PAXG double-entry lesson) |
| Stagnation TP mandatory | Positions that peaked then reversed to zero (Mantis lesson) |

## Directory Layout

```
senpi-waifu/
├── config/                    # Shared configuration (both layers read/write)
│   ├── risk-regime.json       # RISK_ON / BASELINE / RISK_OFF + guardrails
│   ├── scanner-config.json    # ORCA + KOMODO thresholds (gates are in code)
│   ├── condor-config.json     # CONDOR assets, correlation map, DSL tiers
│   ├── sentinel-config.json   # SENTINEL quality-convergence thresholds
│   ├── rhino-config.json      # RHINO pyramid stages and trend filters
│   ├── barracuda-config.json  # [PAUSED] BARRACUDA funding thresholds
│   ├── bison-config.json      # [PAUSED] BISON trend/momentum params
│   ├── shark-config.json      # [PAUSED] SHARK OI/liquidation thresholds
│   └── wolf-strategies.json   # Strategy registry (wallets, budgets, slots)
├── state/                     # Runtime state (mechanical layer writes, brain/Hermes read)
│   ├── {strategy-key}/        # Per-strategy DSL state files
│   │   └── dsl-{ASSET}.json   # Position state (High Water Mode)
│   ├── pending-entries.json   # Signals queued with brain priority context
│   ├── orca-scan-history.json # Last 40 ORCA scans
│   ├── orca-cooldowns.json    # Per-asset 2hr cooldown tracking
│   ├── komodo-events.json     # KOMODO momentum event history
│   └── komodo-cooldowns.json  # KOMODO per-asset cooldowns
├── memory/                    # Cumulative knowledge (both layers write)
│   ├── MEMORY.md              # Persistent agent context
│   ├── howl-*.md              # Nightly HOWL reports
│   └── trade-journal.json     # All trades with entry source tags
├── outputs/                   # Reports and observational data
│   ├── autonomous-brain.json  # Local strategic policy snapshot
│   ├── latest-report.json     # Last portfolio review
│   ├── arbiter-state.json     # Risk arbiter peak/drawdown tracking
│   ├── arena-state.json       # Senpi Predators performance snapshot
│   ├── arena-learnings.json   # Arena-derived strategy recommendations
│   ├── playbook-state.json    # Normalized execution playbook + scanner profiles
│   └── codebase-index.json    # Indexed runtime map of this repo
├── dashboard/
│   ├── server.py              # FastAPI dashboard + Telegram bot
│   ├── telegram_bot.py        # Telegram command handlers
│   └── templates/index.html   # Mobile-first web dashboard
├── scripts/
│   ├── lib/senpi_common.py    # Shared Python library
│   ├── vps/                   # Cron job scripts (run by worker.py)
│   │   ├── orca-scanner-cron.py     # 🐋 ORCA dual-mode scanner
│   │   ├── komodo-scanner-cron.py   # 🦎 KOMODO momentum events
│   │   ├── condor-scanner-cron.py   # 🦅 CONDOR multi-asset hunter
│   │   ├── sentinel-scanner-cron.py # 🛡 SENTINEL quality convergence
│   │   ├── rhino-scanner-cron.py    # 🦏 RHINO momentum pyramider
│   │   ├── barracuda-scanner-cron.py # [PAUSED] BARRACUDA funding decay
│   │   ├── bison-scanner-cron.py    # [PAUSED] BISON conviction trend
│   │   ├── shark-scanner-cron.py    # [PAUSED] SHARK liquidation cascade
│   │   ├── dsl-runner.py            # 🔒 DSL High Water runner
│   │   ├── autonomous-brain.py      # 🧠 Local policy/playbook builder
│   │   ├── sm-flip-cron.py          # 🔄 Position supervisor (flip/collapse/rotation)
│   │   ├── watchdog-cron.py         # 👁 Watchdog (margin/liq)
│   │   ├── health-check-cron.py     # 🏥 Health check + git sync
│   │   ├── risk-arbiter.py          # 🚨 Mechanical safety
│   │   ├── arena-monitor.py         # 📊 Arena performance tracker
│   │   ├── reconcile-closes.py      # Trade journal close reconciler
│   │   └── provision-vps.sh         # One-shot VPS setup (alt deploy)
│   ├── oz/                          # Legacy Oz scripts (replaced by Hermes)
│   │   ├── setup-oz-agents.sh       # Creates Oz environment + schedules
│   │   └── agent-init.sh            # Runtime init for each Oz agent
│   └── hermes-*.sh                  # Hermes strategic layer cron scripts
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
| `senpi-worker` | `python3 worker.py` | Runs scanners, brain, supervisor, and safety checks |
| `senpi-dashboard` | `uvicorn dashboard.server:app --host 0.0.0.0 --port $PORT` | Web dashboard + Telegram bot |

### 3. Set environment variables

Both services:
```
SENPI_API_KEY        = <Senpi MCP auth token (same as SENPI_AUTH_TOKEN)>
GITHUB_TOKEN         = <GitHub fine-grained token, Contents read/write>
GITHUB_REPO          = YOUR_USER/senpi-waifu
SENPI_STATE_DIR      = /app
SENPI_SKILLS_DIR     = /opt/senpi/senpi-skills
TELEGRAM_BOT_TOKEN   = <from @BotFather>
TELEGRAM_CHAT_ID     = <your chat ID>
```

Dashboard service only:
```
WARP_API_KEY         = <legacy, optional — Warp API key for Oz dispatch>
OZ_ENVIRONMENT_ID    = <legacy, optional — from setup-oz-agents.sh>
DASH_TOKEN           = <secret for web dashboard auth>
```

### 4. Add a trading strategy

```bash
mcporter call senpi strategy_create_custom_strategy --json '{"budgetUsd": 2000}'
```

Register the returned strategy in `config/wolf-strategies.json`, fund the wallet with USDC, and the scanners start hunting immediately.

### 5. Set up Hermes strategic agents

The strategic layer runs locally as Hermes cron jobs — no cloud subscription required.

```bash
# Set required env vars
export SENPI_API_KEY="..."
export GITHUB_TOKEN="..."
export SENPI_WAIFU_DIR="/home/kt/senpi-waifu"

# Enable the 6 agent roles as cron jobs:
#   */15 * * * *   Trade Evaluator
#   0 * * * *      Regime Classifier
#   0 */6 * * *    Portfolio Review
#   55 23 * * *    HOWL Nightly
#   0 1 * * *      Whale Index
#   0 */4 * * *    Arena Learner
```

Start with Regime Classifier and Portfolio Review first (read-only). Add Trade Evaluator after 1-2 hours of stable operation. See `AGENTS.md` for full bootstrap procedure.

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

### Hermes Strategic Layer

Hermes runs locally as scheduled cron jobs — **zero additional cost** beyond your machine's compute. No cloud subscription or credit system required.

## Key Design Decisions

- **Hybrid architecture.** VPS handles deterministic execution and local brain synthesis. Hermes handles slower, higher-intelligence supervisory work via local cron jobs.
- **Local playbook layer.** Brain and playbook outputs let the execution layer consume strategy intelligence without waiting on Hermes or embedding LLM logic in the hot path.
- **Active scanner suite (5).** ORCA (emerging movers), KOMODO (momentum events), CONDOR (multi-asset alpha), SENTINEL (quality trader convergence), RHINO (momentum pyramiding). Different edge types, one shared DSL exit engine.
- **Paused scanners (3).** SHARK (Senpi paused, v1.0 -4.3% ROI), BARRACUDA and BISON removed pending performance review. Code preserved in `scripts/vps/` for reactivation.
- **Scanner-specific supervision.** Conviction collapse and dead-weight rotation are tuned per scanner rather than handled by one generic kill rule.
- **CONDOR correlation confirmation.** Cross-validates signals against paired assets (e.g. ETH↔BTC) to filter false breakouts on correlated pairs.
- **SENTINEL inverted discovery.** Finds assets where smart money is building first, then verifies that quality traders are the ones profiting from the move before entering.
- **RHINO staged deployment.** Risks only 30% of max size at scout entry, then adds to confirmed winners instead of going all-in at the least certain moment.
- **DSL High Water Mode.** No ceiling on trailing stops. The geometry that lets positions hold +200% winners.
- **Native Python everywhere.** All VPS scripts rewritten from shell to Python using senpi_common.py. No dependency on senpi-skills or OpenClaw at runtime.
- **Fee optimisation.** STALKER and KOMODO use ALO (maker) orders for ~60-80% fee reduction. Fee drag is the #1 killer across all 22 agents.
- **Risk Arbiter is not an LLM.** Mechanical safety should never depend on a language model or cloud credits.
- **Directional exposure enforcement.** New entries are blocked when projected long/short concentration would breach the portfolio cap.
- **Git as state bus.** Simple, auditable, works offline. Both layers read/write independently.
- **Telegram-first control.** Full monitoring and manual override from your phone. Free-text messages dispatch to Hermes.
- **Arena-informed learning.** Hermes studies 24 competing predator strategies and auto-applies risk-reducing improvements.
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
