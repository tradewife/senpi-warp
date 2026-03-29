# 🐺 senpi-waifu: High-Integrity Trading CLI

### *Mechanical Strength. Decision Sovereignty.*

**senpi-waifu** is a high-integrity CLI and autonomous worker for operating a Hyperliquid perpetual futures trading system. It integrates [Senpi MCP](https://senpi.ai) strategies with a hardened governance layer that separates mechanical safety from high-level decision-making.

The system utilizes a **Single Execution Path** architecture: scanners act as passive probes, while all trade execution is centralized through the `TradeEvaluator` governance engine (`waifu_cli/safety.py`) to eliminate ghost signals and race conditions.

---

## 🏗 Core Architecture: Tiered Governance

The system operates on three layers of authority to ensure selectivity and account protection:

1.  **Passive Probes (Scanners):** Strategies like POLAR, ORCA, MANTIS, and others continuously monitor markets for signals. They have **zero trading authority** and only queue detections into `state/pending-entries.json`.
2.  **Manual Gateway (`waifu evaluate`):** The primary Human-in-the-Loop (HITL) interface. It applies **10 Safety Gates** and notifies you via Telegram for approval on valid signals.
3.  **Autonomous Overlay (`waifu jido`):** A high-conviction wrapper that imports the same `TradeEvaluator` engine. Executes trades automatically **only** if a signal passes all 10 safety gates AND the scanner's ROI exceeds your threshold (default 15% from `arena-learnings.json`). Below threshold, falls back to Telegram for manual approval.

---

## 🧠 The Brain: Hermes Apollo

[Hermes Apollo](https://hermes-agent.nousresearch.com/) serves as the system's strategic ceiling — an agentic coding assistant running inside the Railway container.

*   **Python-Native Integration:** The `waifu-cli` codebase is ~16,000 lines of Python across 50 modules. Hermes Apollo (91.9% Python) shares a native language, allowing the brain to perform deep structural audits of the system's core logic and state.
*   **Persistent Identity:** Apollo maintains a global identity via **`SOUL.md`** (`config/hermes-soul.md`), defining its authority and constraints. It may read all state files and modify `config/user-rules.json` for strategic tuning, but **cannot bypass the 10 Safety Gates**.
*   **Telegram Chat:** Send any non-command text to the bot to converse with the brain. It has full filesystem access to analyze trade journals, scanner performance, and regime data.

### Telegram Commands

| Command | Purpose |
| :--- | :--- |
| `/start` | Full control panel with live status + inline buttons |
| `/status` | Regime, positions, PnL, cron health |
| `/settings` | Unified view of rules, gates, and scanner scores |
| `/set <key> <val>` | Change any setting (rules, gates, or scores) |
| `/jido` | Trigger autonomous executor |
| `/evaluate` | Process pending scanner signals |
| `/regime` | Run regime classifier |
| `/review` | Portfolio health report |
| `/howl` | Nightly self-improvement analysis |
| `/whale` | Copy-trade rebalance |
| `/arena` | Predator leaderboard |
| `/emergency_stop` | Immediate RISK\_OFF + alert |
| Free text | Chat with the Strategic Brain |

---

## 🛡 Safety Gates

These 10 gates are enforced in `waifu_cli/safety.py` by `TradeEvaluator`. Gates 1–3 and 5 are automatic (regime/brain-controlled). Gates 4, 6–10 are user-configurable via `/set` in Telegram or `/gates_set`, with the defaults shown below:

| # | Gate | Default | Description |
| :--- | :--- | :--- | :--- |
| 1 | Entries Allowed | Regime-gated | RISK_OFF blocks all entries |
| 2 | Auto-Entry Enabled | Regime + brain | Must be enabled by both regime and brain policy |
| 3 | Valid Strategy | Required | Strategy ID must be configured and valid |
| 4 | Slots Available | 3 max | Concentration over diversification |
| 5 | Scanner Not Blocked | Brain policy | Brain can disable underperforming scanners |
| 6 | Score Threshold | Per-scanner | ORCA ≥7, MANTIS ≥7, FOX ≥7, ROACH ≥9, KOMODO ≥10, etc. |
| 7 | Asset Ban | `xyz:*` | Prefixed assets strictly prohibited |
| 8 | Cooldown | 2 hours | Mandatory per-asset waiting period after exit |
| 9 | Directional Exposure | 70% cap | Prevents one-directional concentration |
| 10 | Leverage Clamp | 7–10x | Min to overcome fees; max to prevent blowups |

**Additional guardrails** (enforced by Risk Arbiter, `scripts/vps/risk-arbiter.py`):

| Guardrail | Value | Description |
| :--- | :--- | :--- |
| Daily Loss Limit | 10% | Auto-triggers RISK_OFF regime |
| Catastrophic Drawdown | 20% | Immediate flattening from equity peak |

### Customizing Rules

Users control strategic parameters via `config/user-rules.json` or the `/set` Telegram command:

```json
{
  "evaluate": { "minScore": 7, "maxLeverage": 10, "maxPositions": 3 },
  "jido": { "roi_threshold_auto": 0.16, "autoExecuteEnabled": true },
  "fixed_tp_roe": { "enabled": false, "tpRoePct": null },
  "fixed_sl_roe": { "enabled": false, "slRoePct": null },
  "partial_tp": { "enabled": false, "tp1RoePct": null, "tp1ClosePct": 50 },
  "partial_sl": { "enabled": false, "sl1RoePct": null, "sl1ClosePct": 50 }
}
```

These rules sit **above** DSL defaults but **below** the 10-gate safety floor. They cannot bypass account-level safety.

---

## 📊 Scanner Fleet

### Active Scanners (Scheduled in `worker.py`)

| Scanner | Version | Interval | Description |
| :--- | :--- | :--- | :--- |
| 🐋 ORCA | v1.3 | 3min | Hardened dual-mode scanner |
| 🦗 MANTIS | v3.0 | 90s | Smart money sniper with contribution acceleration gate |
| 🦊 FOX | v2.0 | 90s | Dual-mode scanner with minReasons gate |
| 🪳 ROACH | v1.0 | 90s | Striker-only explosion signals |
| 🦎 KOMODO | v1.0 | 5min | Momentum event consensus |
| 🦅 CONDOR | v2.0 | 3min | Multi-asset alpha hunter |
| 🐻‍❄️ POLAR | v2.0 | 3min | ETH alpha hunter (HUNT → RIDE → STALK → RELOAD lifecycle) |
| 🛡 SENTINEL | v1.0 | 3min | Quality trader convergence |
| 🦏 RHINO | v1.0 | 3min | Momentum pyramider |

### Paused

- 🦈 SHARK — Senpi-paused (v1.0, -4.3% ROI)
- 🎣 BARRACUDA — Removed from schedule
- 🦬 BISON — Removed from schedule

### Arena Leaderboard (Live from `outputs/arena-state.json`)

| Rank | Strategy | ROI | Trades | PnL |
| :--- | :--- | :--- | :--- | :--- |
| 1 | Polar | 28.09% | 29 | $280.95 |
| 2 | Orca v1.1 | 9.49% | 737 | $94.91 |
| 3 | Roach | 9.42% | 57 | $94.21 |
| 4 | Mantis | 5.56% | 460 | $55.58 |

### Available in Senpi Skills Catalog

Install with `waifu dev add-skill <name>`. Browse with `waifu dev list-skills`.

Wolverine, Roach, Cheetah, Raptor, Jaguar, Cobra, Mamba, Hawk, Hydra, Jackal, Kodiak, Viper, and others.

### Support Systems (Scheduled in `worker.py`)

| System | Interval | Purpose |
| :--- | :--- | :--- |
| DSL Runner | 3min | Dynamic stop-loss / high-water trailing |
| SM Flip | 5min | Smart-money flip detector |
| Watchdog | 5min | Margin and liquidation monitoring |
| Health Check | 10min | mcporter connectivity + git sync |
| Arena Monitor | 15min | Senpi Predators leaderboard analysis |
| Regime Classifier | 15min | BTC macro regime (RISK_ON / BASELINE / RISK_OFF) |
| Autonomous Brain | 5min | Strategic brain policy updates |
| Risk Arbiter | 30s | Mechanical safety (drawdown, daily loss) |
| Reconcile | 15min | Close detection and journal sync |
| ELITE Trader | 30min | Full research-to-execution loop |
| JIDO Executor | 5min | Autonomous trade executor |

---

## 🛠 Setup & LLM Integration

### 1. Installation
```bash
pip install -e .
```

### 2. Configuration
```bash
waifu config validate
```

**Required Variables:**

| Variable | Description |
| :--- | :--- |
| `SENPI_AUTH_TOKEN` | Senpi MCP authentication token |
| `GITHUB_TOKEN` | Fine-grained token for state synchronization |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (from @BotFather) |
| `TELEGRAM_CHAT_ID` | Your chat ID (bot only responds to this) |

### 3. LLM Configuration (Strategic Brain)

While Hermes is provider-agnostic, we recommend the [Z.AI Coding Plan](https://z.ai) with `glm-4-plus` or `glm-5-turbo`. See the [Hermes Configuration Guide](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) for all provider options.

| Variable | Value |
| :--- | :--- |
| `GLM_API_KEY` | Your Z.AI API Key (Standard Key, not JWT) |
| `GLM_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` |
| `HERMES_INFERENCE_PROVIDER` | `zai` |
| `HERMES_MODEL` | `glm-5-turbo` (or preferred model) |

### 4. Deploy to Railway

Push to your repository to trigger an automatic build. The `worker.py` schedules all scanners, the JIDO executor (every 5 min), and the Regime Classifier (every 15 min).

---

## 🕹 CLI Command Reference

| Command | Purpose | Mode |
| :--- | :--- | :--- |
| `waifu status` | Regime, positions, PnL, cron health | Read-Only |
| `waifu evaluate [--dry-run]` | Process queue through 10 safety gates | HITL |
| **`waifu jido [--dry-run]`** | Autonomous executor (ROI-gated) | Autonomous |
| `waifu regime [--dry-run]` | Classify BTC macro regime | Strategic |
| `waifu review` | Portfolio health report | Analytics |
| `waifu howl` | Nightly 10-pillar self-improvement | Analytics |
| `waifu arena` | Predator leaderboard analysis | Intelligence |
| `waifu whale` | Copy-trade portfolio management | Intelligence |
| `waifu emergency-stop` | Immediate RISK_OFF + Telegram alert | Emergency |
| `waifu dev brain-ping` | LLM provider connectivity check | Diagnostic |
| `waifu dev list-skills` | Browse Senpi Skills catalog | Dev |
| `waifu dev add-skill <name>` | Install a skill from the catalog | Dev |

---

## 🗂 File Reference

| Path | Purpose |
| :--- | :--- |
| `waifu_cli/safety.py` | 10-gate safety pipeline |
| `waifu_cli/commands/evaluate.py` | TradeEvaluator engine |
| `waifu_cli/commands/jido.py` | Autonomous executor |
| `config/risk-regime.json` | Current regime + guardrails |
| `config/user-rules.json` | User-configurable strategic rules |
| `config/wolf-strategies.json` | Strategy registry |
| `config/hermes-soul.md` | Brain identity (SOUL.md) |
| `state/pending-entries.json` | Queued scanner signals |
| `outputs/autonomous-brain.json` | Brain policy snapshot |
| `outputs/arena-state.json` | Arena leaderboard |
| `memory/trade-journal.json` | All trade records |
| `worker.py` | Railway scheduler (all crons) |
| `dashboard/telegram_bot.py` | Telegram bot + Hermes bridge |

---

*Built with ❤️ for the [Senpi Ecosystem](https://senpi.ai).*
