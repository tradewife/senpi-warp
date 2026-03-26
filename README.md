***

# 🐺 senpi-waifu: Strategic Trading CLI
### *Mechanical Strength. Strategic Sovereignty.*

**senpi-waifu** is a high-integrity CLI and autonomous worker for operating a Hyperliquid perpetual futures trading system. It integrates **Senpi MCP strategies** with a hardened, tiered governance layer that separates mechanical safety from strategic decision-making.

The system has been re-engineered to eliminate "ghost signals" and race conditions through a **Single Execution Path** architecture: scanners are passive probes, while all trade execution is centralized through the `TradeEvaluator` governance engine.

---

## 🏗 Core Architecture: Tiered Governance

The system operates on three distinct layers of authority to ensure profitable selectivity and account safety:

1.  **Passive Probes (Scanners):** ORCA, MANTIS, FOX, and others continuously scan the markets for signals. They have **zero trading authority** and only queue detections into `state/pending-entries.json`.
2.  **Manual Gateway (`waifu evaluate`):** The primary Human-in-the-Loop (HITL) interface. It applies the **10 Hardcoded Safety Gates** and notifies you via Telegram for approval on valid signals.
3.  **Autonomous Overlay (`waifu jido`):** A high-conviction wrapper that imports the evaluation engine. It executes trades automatically **only** if a signal passes your specific "Autonomous Rules" (e.g., Scanner ROI > 15% in `arena-learnings.json`).

---

## 🛡 Non-Negotiable Safety Gates
These gates are hardcoded in the Python core (`senpi_common.py`) and cannot be overridden by strategic config or AI agents:

| Gate | Value | Description |
| :--- | :--- | :--- |
| **Max Positions** | 3 | Concentration over diversification to beat fees. |
| **Leverage Band** | 7–10x | Minimum to overcome fees; max to prevent blowups. |
| **Daily Loss Limit** | 10% | Auto-triggers **RISK_OFF** regime if hit. |
| **Catastrophic DD** | 20% | Immediate flattening of all positions from equity peak. |
| **Asset Ban** | XYZ:* | Any assets prefixed with `xyz:` are strictly prohibited. |
| **Cooldown** | 2 Hours | Mandatory per-asset waiting period after a position exit. |
| **Trend Alignment** | 4H | No counter-trend entries allowed against the 4H window. |

---

## 🚀 Quick Start

### 1. Installation
Install the CLI in editable mode to link the `waifu_cli` package:
```bash
pip install -e .
```

### 2. Configuration
The system uses a unified `/app` state directory for both local and Railway environments. Initialize your environment:
```bash
waifu config validate
```
**Required Variables:**
*   `SENPI_AUTH_TOKEN`: Your Senpi MCP authentication.
*   `GITHUB_TOKEN`: Fine-grained token for state synchronization.

### 3. Deploy to Railway
Pushing to your repository triggers an automatic build. The `worker.py` will initialize the unified `/app` path and schedule the **JIDO Executor** to run every 5 minutes.

---

## 🕹 Command Reference

| Command | Purpose | Path |
| :--- | :--- | :--- |
| `waifu status` | Snapshot of regime, positions, and cron health. | Read-Only |
| `waifu evaluate` | **Manual Gateway:** Process queue and request approval. | HITL |
| **`waifu jido`** | **Autonomous Wrapper:** Execute high-ROI signals. | Autonomous |
| `waifu regime` | Classify market as RISK_ON, BASELINE, or RISK_OFF. | Strategic |
| `waifu howl` | Nightly 10-pillar self-improvement and analysis. | Analytics |
| `waifu arena` | Study winning predator strategies for ROI benchmarks. | Intelligence |
| `waifu config rules` | View and edit user-defined strategic rules via chat [User Intent]. | Strategic |

---

## 📡 Chat Integration
The Telegram bot provides real-time control over your trading strategy:
*   **`/status`**: Instant dashboard view.
*   **`/rules`**: View current strategic thresholds for `jido`.
*   **`/rules set <key> <val>`**: Dynamically update ROI or score requirements without a code push [User Intent].
*   **`/flatten`**: Emergency close of all positions via the Oz strategic layer.

---

## 🛠 Developer Notes
*   **Environment:** All state is persisted in `/app` on Railway.
*   **Atomic Locking:** All position count modifications use the `acquire_trade_lock()` at `/tmp/senpi-trade.lock` to prevent race conditions.
*   **Skills:** The system automatically pulls strategy updates via `update_skills()` in the health loop.

---
*Built with ❤️ for the Senpi Ecosystem.*
