# waifu — Strategic Trading CLI

A CLI for operating a Hyperliquid perps trading system. The mechanical layer (Railway) runs scanners and enforces safety. You run this CLI for strategic decisions, observability, and skill management.

## Quick Start

```bash
# Check system state
waifu status

# Watch live logs from Railway
waifu debug logs -f

# Classify market regime
waifu regime

# Process pending signals (dry-run first)
waifu evaluate --dry-run
waifu evaluate
```

## Installation

```bash
git clone https://github.com/YOUR_USER/senpi-waifu.git
cd senpi-waifu
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure (first-time setup)
cp .env.example .env
$EDITOR .env  # Add your tokens
waifu config validate

# Verify
waifu --help
```

## Configuration

Run `waifu config validate` to check your setup. Set required values:

```bash
waifu config set SENPI_AUTH_TOKEN your_senpi_token
waifu config set GITHUB_TOKEN your_github_token
```

**Required variables:**

| Variable | Purpose |
|----------|---------|
| `SENPI_AUTH_TOKEN` | Senpi MCP authentication (get from senpi.ai) |
| `GITHUB_TOKEN` | GitHub fine-grained token with repo read/write |

**Optional variables:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `SENPI_WAIFU_DIR` | Auto-detect | Project root |
| `GITHUB_REPO` | `tradewife/senpi-waifu` | Repo for state sync |
| `TELEGRAM_BOT_TOKEN` | None | Telegram alerts |
| `TELEGRAM_CHAT_ID` | None | Telegram chat ID |
| `RAILWAY_TOKEN` | None | Railway CLI |

For Railway deployment, export and copy to dashboard:
```bash
waifu config export
```

## Commands

| Command | Purpose | Schedule |
|---------|---------|----------|
| `waifu status` | System overview (read-only) | On-demand |
| `waifu regime` | Classify RISK_ON/BASELINE/RISK_OFF | Hourly |
| `waifu evaluate` | Process signals, execute trades | Every 15min |
| `waifu review` | Portfolio health report | Every 6hr |
| `waifu howl` | Nightly self-improvement | Daily 23:55 |
| `waifu whale` | Copy-trade rebalance | Daily 01:00 |
| `waifu arena` | Study top predators | Every 4hr |
| `waifu emergency-stop` | Immediate RISK_OFF | On-demand |

### Config Commands

| Command | Purpose |
|---------|---------|
| `waifu config show` | View current config (secrets masked) |
| `waifu config set <key> <val>` | Set a value (writes to .env) |
| `waifu config validate` | Check required vars |
| `waifu config export` | Export for Railway |

### Debug Commands

| Command | Purpose |
|---------|---------|
| `waifu debug logs [-f]` | Railway logs (follow mode with -f) |
| `waifu debug status` | Deployment + local health |
| `waifu debug tail <scanner>` | Filter logs to one scanner |
| `waifu debug deploy --trigger` | Redeploy to Railway |

### Dev Commands

| Command | Purpose |
|---------|---------|
| `waifu dev list-skills` | Browse installable skills |
| `waifu dev add-skill <name>` | Install a skill |
| `waifu dev create-skill <name>` | Scaffold a new skill |
| `waifu dev show-skill <name>` | Display skill documentation |

## Architecture

```
┌─────────────────────────────────────────┐
│  Railway (Mechanical Layer)             │
│  • Scanners hunt entries (60s-5min)     │
│  • DSL trailing stops manage exits      │
│  • Risk Arbiter enforces safety         │
└───────────────┬─────────────────────────┘
                │ git pull/push
┌───────────────┴─────────────────────────┐
│  waifu CLI (Strategic Layer)            │
│  • Regime classification                │
│  • Signal evaluation & execution        │
│  • Reports & self-improvement           │
└─────────────────────────────────────────┘
```

The mechanical layer is authoritative. This CLI can only influence config and execute trades through the same API — it cannot bypass hardcoded safety gates.

## Safety Constraints (Non-Negotiable)

These are enforced in Python code:

| Gate | Value |
|------|-------|
| Max positions | 3 |
| Leverage | 7–10x only |
| Daily loss limit | 10% → auto RISK_OFF |
| Catastrophic drawdown | 20% → auto flatten |
| XYZ equities | BANNED |
| Per-asset cooldown | 2 hours |

## Cron Setup (Optional)

For autonomous operation, schedule the commands:

```cron
*/15 * * * *  cd /home/kt/senpi-waifu && source venv/bin/activate && waifu evaluate
0 * * * *     cd /home/kt/senpi-waifu && source venv/bin/activate && waifu regime
0 */6 * * *   cd /home/kt/senpi-waifu && source venv/bin/activate && waifu review
55 23 * * *   cd /home/kt/senpi-waifu && source venv/bin/activate && waifu howl
0 1 * * *     cd /home/kt/senpi-waifu && source venv/bin/activate && waifu whale
0 */4 * * *   cd /home/kt/senpi-waifu && source venv/bin/activate && waifu arena
```

## Emergency Stop

```bash
waifu emergency-stop --reason "Manual intervention"
```

Or edit directly:
```bash
echo '{"riskMode":"RISK_OFF",...}' > config/risk-regime.json
git commit -am "RISK_OFF" && git push
```

## Further Reading

- `AGENTS.md` — Full operating manual for AI and human operators
- `senpi-skills/` — Available trading skills
