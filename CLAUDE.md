# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**senpi-waifu** is an autonomous Hyperliquid perpetual futures trading system. It runs scanner probes that detect trading signals, passes them through a 10-gate safety pipeline, and executes approved trades via the Senpi MCP API. The system deploys to Railway with a scheduler (`worker.py`) replacing crontab.

## Development Commands

```bash
# Activate environment
source venv/bin/activate

# Install
pip install -e .

# Run CLI commands (entry point: ./waifu or python -m waifu_cli)
waifu status
waifu evaluate --dry-run
waifu jido --dry-run
waifu regime --dry-run
waifu review
waifu howl
waifu arena
waifu config validate

# Run worker locally (APScheduler with all scanner crons)
python3 worker.py

# Run dashboard
uvicorn dashboard.server:app --host 0.0.0.0 --port 8080

# No test suite exists. Use --dry-run flags for safe validation.
```

## Architecture

### Three-Layer Governance

1. **Passive Probes** (`scripts/vps/*-scanner-cron.py`) — Scanners (ORCA, MANTIS, FOX, ROACH, KOMODO, CONDOR, POLAR, SENTINEL, RHINO) have zero trading authority. They write signals to `state/pending-entries.json`.

2. **Safety Pipeline** (`waifu_cli/safety.py`) — `evaluate_entry()` runs all 10 gates. This is the single entry point. No trade can bypass it. Called by both `waifu evaluate` (manual HITL) and `waifu jido` (autonomous).

3. **Mechanical Safety** (`scripts/vps/risk-arbiter.py`) — Runs every 30s. Enforces daily loss limit (10%) and catastrophic drawdown (20%). Can force RISK_OFF regime.

### Key Code Paths

- **`waifu_cli/main.py`** — Click CLI group, registers all subcommands
- **`waifu_cli/commands/evaluate.py`** — `TradeEvaluator` class: reads pending entries, runs gate pipeline, executes via Senpi MCP
- **`waifu_cli/commands/jido.py`** — Autonomous executor: same gate pipeline but auto-executes if scanner ROI exceeds threshold
- **`waifu_cli/safety.py`** — `evaluate_entry()`: the 10-gate pipeline. Pure function, no side effects
- **`scripts/lib/senpi_common.py`** — Shared library (~900 lines): config loading, state I/O, Senpi MCP HTTP calls, git sync, Telegram alerts, lock files, guardrail checks. All cron scripts import this.
- **`worker.py`** — APScheduler `BlockingScheduler` with 8-thread pool. Runs all crons. Also launches Telegram bot in a daemon thread.
- **`dashboard/telegram_bot.py`** — Telegram bot (python-telegram-bot). Routes commands to CLI functions. Free text goes to Hermes Apollo (LLM brain).
- **`dashboard/server.py`** — FastAPI dashboard for web UI

### Config & State (all JSON)

- `config/risk-regime.json` — Current regime (RISK_ON/BASELINE/RISK_OFF) + per-regime params + global guardrails
- `config/user-rules.json` — User-adjustable thresholds (scores, leverage, TP/SL, ROI). Sits above DSL defaults, below safety gates.
- `config/wolf-strategies.json` — Strategy registry with Senpi strategy IDs
- `config/*-config.json` — Per-scanner configs
- `config/hermes-soul.md` — LLM brain identity/constraints
- `state/pending-entries.json` — Queued scanner signals (input to evaluate/jido)
- `state/*/dsl-*.json` — Per-position DSL trailing stop state (gitignored)
- `memory/trade-journal.json` — All trade records
- `memory/MEMORY.md` — Persistent context log for LLM brain
- `outputs/autonomous-brain.json` — Brain policy snapshot
- `outputs/arbiter-state.json` — Peak equity / drawdown tracking

### Environment Variables

Required: `SENPI_AUTH_TOKEN`, `GITHUB_TOKEN`. Optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SENPI_WAIFU_DIR` (defaults to repo root locally, `/app` on Railway), `SENPI_SKILLS_DIR` (defaults to `/opt/senpi/senpi-skills`).

The `.env` file is loaded by `scripts/lib/senpi_common.py` at import time. CLI commands (`waifu_cli/`) add `scripts/lib` to `sys.path` to access it.

### Critical Safety Invariants

These are hardcoded in Python, not configurable. They must never be weakened:
- XYZ equities are permanently banned
- Leverage is clamped to 7–10x
- Max 3 concurrent positions
- 10% daily loss limit → auto RISK_OFF
- 20% drawdown from peak → auto-flatten
- 2-hour per-asset cooldown after exit
- All trade entries flow only through `waifu evaluate` or `waifu jido`
- The Risk Arbiter is the sole authority for RISK_OFF transitions

### Hermes Apollo (LLM Brain)

An agentic coding assistant (Hermes Apollo) can be invoked via Telegram free-text. It reads all state files and may modify `config/user-rules.json` and per-scanner configs, but **cannot** bypass the 10 safety gates. Its identity and constraints are defined in `config/hermes-soul.md`.

### Deployment

Dockerfile builds with Python 3.11, installs `mcporter` (Senpi MCP client), clones `senpi-skills` and `hermes-apollo` repos. Railway runs two services from the same image: `worker` (default CMD) and `dashboard` (uvicorn override).
