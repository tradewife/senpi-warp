# 🐺 senpi-waifu: High-Integrity Trading CLI

### *Mechanical Strength. Strategic Sovereignty.*

**senpi-waifu** is a high-integrity CLI and autonomous worker for operating a Hyperliquid perpetual futures trading system. It integrates **Senpi MCP strategies** with a hardened governance layer that separates mechanical safety from high-level decision-making. 

The system utilizes a **Single Execution Path** architecture: scanners act as passive probes, while all trade execution is centralized through the `TradeEvaluator` governance engine to eliminate "ghost signals" and race conditions.

---

## 🏗 Core Architecture: Tiered Governance

The system operates on three layers of authority to ensure selectivity and account protection:

1.  **Passive Probes (Scanners):** Strategies like **POLAR** and **ROACH** continuously monitor markets for signals. They have **zero trading authority** and only queue detections into `state/pending-entries.json`.
2.  **Manual Gateway (`waifu evaluate`):** The primary Human-in-the-Loop (HITL) interface. It applies **10 Hardcoded Safety Gates** and notifies you via Telegram for approval on valid signals.
3.  **Autonomous Overlay (`waifu jido`):** A high-conviction wrapper that executes trades automatically **only** if a signal passes user-defined "Autonomous Rules" (e.g., Scanner ROI > 15%).

---

## 🧠 The Brain: Hermes Apollo

**Hermes Apollo** serves as the system's "Intelligent Ceiling". It is an opinionated distribution of the `hermes-agent` tuned for technical workflows and rigorous software auditing.

*   **Python-Native Integration:** Built primarily on **Python (91.9%)**, Hermes Apollo shares a native language with the `waifu-cli` (91.7%) and Senpi Skills (100%). This allows the Brain to perform deep structural audits of the system's core logic and state.
*   **Persistent Identity:** Unlike standard agents, Apollo maintains a global identity via **`SOUL.md`**, ensuring it adheres to a consistent long-term trading thesis.
*   **Environment Sovereignty:** It supports `${ENV_VAR}` substitution, allowing it to respect your environment variables and map protocols like **GLM** for stable connectivity with LLM providers.

### **Telegram Remote Control**
You can chat directly with the Hermes Apollo agent via Telegram to manage your desk with natural language.
*   **/status**: Instant dashboard view of regimes, positions, and cron health.
*   **/rules**: View or update (`/rules set <key> <val>`) thresholds for the autonomous executor.
*   **/flatten**: Trigger an emergency close of all open positions via the strategic layer.
*   **Structural Auditing:** Ask the agent to investigate environment issues, such as stale data or connection hiccups [Conversation History].

---

## 🛡 Non-Negotiable Safety Gates

These gates are hardcoded in the Python core (`senpi_common.py`) and cannot be overridden by any autonomous logic or agent:

| Gate | Value | Description |
| :--- | :--- | :--- |
| **Max Positions** | 3 | Concentration over diversification to beat fees. |
| **Leverage Band** | 7–10x | Minimum to overcome fees; maximum to prevent blowups. |
| **Daily Loss Limit**| 10% | Auto-triggers **RISK_OFF** regime if hit. |
| **Catastrophic DD** | 20% | Immediate flattening of all positions from equity peak. |
| **Asset Ban** | `XYZ:*` | Any assets prefixed with `xyz:` are strictly prohibited. |
| **Trend Alignment**| 4H | No counter-trend entries allowed against the 4H window. |

---

## 📡 Integrated Scanners (Senpi Skills)

The following scanners are currently integrated as the active "Passive Probes" for the `waifu-cli`:

*   **🐻❄ POLAR (v1.0):** ETH alpha hunter (+21.6% ROE). Uses a three-mode lifecycle—**HUNT → RIDE → STALK → RELOAD**.
*   **🪳 ROACH (v1.0):** **Recommended first skill**. A "Striker-only" experiment focusing exclusively on high-conviction explosion detection.
*   **🦗 MANTIS (v3.0):** Hardened smart money sniper (+8.0% ROE) utilizing a contribution acceleration quality gate.
*   **🦊 FOX (v2.0):** Dual-mode scanner that uses a `minReasons` gate to filter for high-conviction setups.

---

## 🛠 Setup & LLM Integration

### 1. LLM Configuration
While Hermes is provider-agnostic, we recommend the **Z.AI Coding Plan** (utilizing `glm-4` or `glm-5-turbo`) for its reliability and technical alignment. For detailed provider setup and configuration, see the [Hermes Configuration Guide](https://hermes-agent.nousresearch.com/docs/user-guide/configuration).

**Required Environment Variables:**
*   **`GLM_API_KEY`**: Your Z.AI **API Key** (Note: This is a standard API key, not a JWT).
*   **`GLM_BASE_URL`**: `https://api.z.ai/api/coding/paas/v4`.
*   **`HERMES_INFERENCE_PROVIDER`**: `zai`.
*   **`HERMES_MODEL`**: `glm-5-turbo` (or preferred model).

### 2. Trading Configuration
*   `SENPI_AUTH_TOKEN`: Your Senpi MCP authentication token.
*   `GITHUB_TOKEN`: Fine-grained token for state synchronization.

### 3. Deployment
Pushing to your repository triggers an automatic build. The `worker.py` schedules the **JIDO Executor** (every 5 mins) and the **Regime Classifier** (every 15 mins) to ensure the mechanical floor remains data-driven.

*Built with ❤ for the Senpi Ecosystem.*
