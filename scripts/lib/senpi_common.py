"""
senpi_common.py — Shared utilities for all VPS cron scripts.

Handles: config loading, state read/write, mcporter calls, git sync,
Telegram alerts, and position management.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _load_env_file():
    """Load .env file from SENPI_WAIFU_DIR or project root into os.environ."""
    env_paths = []
    waifu_dir = os.environ.get("SENPI_WAIFU_DIR", "")
    if waifu_dir:
        env_paths.append(Path(waifu_dir) / ".env")
    env_paths.append(Path(__file__).parent.parent.parent / ".env")
    for env_file in env_paths:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = val
            break


_load_env_file()


STATE_DIR = Path(os.environ.get("SENPI_WAIFU_DIR", "/app"))
CONFIG_DIR = STATE_DIR / "config"
POSITION_STATE_DIR = STATE_DIR / "state"
MEMORY_DIR = STATE_DIR / "memory"
OUTPUTS_DIR = STATE_DIR / "outputs"

SKILLS_DIR = Path(os.environ.get("SENPI_SKILLS_DIR", "/opt/senpi/senpi-skills"))

RISK_REGIME_FILE = CONFIG_DIR / "risk-regime.json"
SCANNER_CONFIG_FILE = CONFIG_DIR / "scanner-config.json"
STRATEGIES_FILE = CONFIG_DIR / "wolf-strategies.json"
PENDING_ENTRIES_FILE = POSITION_STATE_DIR / "pending-entries.json"
SCAN_HISTORY_FILE = POSITION_STATE_DIR / "scan-history.json"
TRADE_JOURNAL_FILE = MEMORY_DIR / "trade-journal.json"
BRAIN_STATE_FILE = OUTPUTS_DIR / "autonomous-brain.json"
CODEBASE_INDEX_FILE = OUTPUTS_DIR / "codebase-index.json"
PLAYBOOK_STATE_FILE = OUTPUTS_DIR / "playbook-state.json"

LOCKFILE_DIR = Path("/tmp/senpi-locks")
TRADE_LOCK_FILE = Path("/tmp/senpi-trade.lock")


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def load_json(path: Path, default=None):
    """Load a JSON file, returning `default` if missing or corrupt."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path: Path, data, *, indent=2):
    """Atomically write JSON (write to unique .tmp then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=indent, default=str)
            f.write("\n")
        tmp.rename(path)
    except OSError:
        # Fallback: direct write if atomic rename fails
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        with open(path, "w") as f:
            json.dump(data, f, indent=indent, default=str)
            f.write("\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Risk regime
# ---------------------------------------------------------------------------


def load_regime() -> dict:
    """Return the full regime config."""
    return load_json(RISK_REGIME_FILE)


def load_brain_state() -> dict:
    """Return the latest strategic brain snapshot if available."""
    return load_json(BRAIN_STATE_FILE, default={})


def load_playbook_state() -> dict:
    """Return the latest normalized playbook snapshot if available."""
    return load_json(PLAYBOOK_STATE_FILE, default={})


def current_scanner_profile(scanner: str) -> dict:
    """Return the active scanner profile from the brain/playbook snapshot."""
    scanner_key = str(scanner or "unknown").lower()
    brain = load_brain_state()
    signal_policy = brain.get("signalPolicy", {}) if isinstance(brain, dict) else {}
    profiles = signal_policy.get("scannerProfiles", {})
    if scanner_key in profiles:
        return profiles.get(scanner_key, {})

    playbook = load_playbook_state()
    return (
        playbook.get("scannerProfiles", {}).get(scanner_key, {})
        if isinstance(playbook, dict)
        else {}
    )


def current_brain_policy() -> dict:
    brain = load_brain_state()
    if not isinstance(brain, dict):
        return {}
    policy = brain.get("executionPolicy", {})
    return policy if isinstance(policy, dict) else {}


def _apply_brain_policy(params: dict) -> dict:
    """Overlay risk-reducing brain directives on top of regime params."""
    policy = current_brain_policy()
    if not params:
        params = {}
    effective = dict(params)

    if policy.get("blockNewEntries"):
        effective["newEntriesAllowed"] = False
        effective["autoEntryEnabled"] = False

    if policy.get("allowAutoEntry") is False:
        effective["autoEntryEnabled"] = False

    max_slots_cap = policy.get("maxSlotsCap")
    if isinstance(max_slots_cap, (int, float)) and "maxSlots" in effective:
        effective["maxSlots"] = min(int(effective["maxSlots"]), int(max_slots_cap))

    max_leverage_cap = policy.get("maxLeverageCap")
    if isinstance(max_leverage_cap, (int, float)) and "maxLeverageCrypto" in effective:
        effective["maxLeverageCrypto"] = min(
            float(effective["maxLeverageCrypto"]), float(max_leverage_cap)
        )

    alloc_pct_cap = policy.get("allocPctCap")
    if isinstance(alloc_pct_cap, (int, float)) and "allocPctPerSlot" in effective:
        effective["allocPctPerSlot"] = min(
            float(effective["allocPctPerSlot"]), float(alloc_pct_cap)
        )

    if policy:
        effective["_brainPolicy"] = {
            "generatedAt": policy.get("generatedAt"),
            "mode": policy.get("mode"),
            "reasonCount": len(policy.get("reasons", [])),
        }

    return effective


def current_regime_params() -> dict:
    """Return the active regime's parameter block."""
    regime = load_regime()
    mode = regime.get("riskMode", "BASELINE")
    regimes = regime.get("regimes", {})
    params = regimes.get(mode) or regimes.get("BASELINE", {})
    return _apply_brain_policy(params)


def is_entries_allowed() -> bool:
    params = current_regime_params()
    return params.get("newEntriesAllowed", False)


def is_auto_entry_enabled() -> bool:
    params = current_regime_params()
    return params.get("autoEntryEnabled", False)


def set_risk_mode(mode: str, reason: str, updated_by: str = "vps-script"):
    """Update the risk regime mode. Only the Risk Arbiter or Oz should call this."""
    regime = load_regime()
    regime["riskMode"] = mode
    regime["updatedAt"] = now_iso()
    regime["updatedBy"] = updated_by
    regime["reason"] = reason
    save_json(RISK_REGIME_FILE, regime)


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------


def load_strategies() -> dict:
    return load_json(STRATEGIES_FILE)


def get_enabled_strategies() -> list[dict]:
    """Return list of enabled strategy dicts, each with its key injected."""
    data = load_strategies()
    result = []
    for key, strat in data.get("strategies", {}).items():
        if strat.get("enabled", True):
            strat["_key"] = key
            result.append(strat)
    return result


def get_strategy_state_dir(strategy_key: str) -> Path:
    d = POSITION_STATE_DIR / strategy_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_open_positions(strategy_key: str) -> list[dict]:
    """Return list of active DSL state dicts for a strategy."""
    d = get_strategy_state_dir(strategy_key)
    positions = []
    for f in d.glob("dsl-*.json"):
        state = load_json(f)
        if state and state.get("active", False):
            state["_file"] = str(f)
            positions.append(state)
    return positions


def get_all_open_positions() -> list[dict]:
    """Return all active positions across enabled strategies."""
    positions = []
    for strat in get_enabled_strategies():
        positions.extend(get_open_positions(strat["_key"]))
    return positions


def compute_roe_pct(
    entry_price: float, current_price: float, direction: str, leverage: float
) -> float:
    """Compute leverage-adjusted ROE percentage."""
    if entry_price <= 0 or leverage <= 0:
        return 0.0
    if direction.upper() == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - current_price) / entry_price
    return pnl_pct * leverage * 100


def _position_notional_usd(position: dict) -> float:
    margin = float(position.get("margin", 0) or 0)
    leverage = float(position.get("leverage", 0) or 0)
    if margin > 0 and leverage > 0:
        return abs(margin * leverage)
    size = float(position.get("size", 0) or 0)
    entry_price = float(position.get("entryPrice", 0) or 0)
    if size > 0 and entry_price > 0:
        return abs(size * entry_price)
    return 0.0


def directional_exposure_snapshot(
    *,
    additional_direction: str | None = None,
    additional_margin: float = 0.0,
    additional_leverage: float = 1.0,
    additional_position: bool = True,
) -> dict:
    """Summarize current and projected directional notional exposure."""
    positions = get_all_open_positions()
    long_notional = 0.0
    short_notional = 0.0

    for pos in positions:
        direction = str(pos.get("direction", "")).upper()
        notional = _position_notional_usd(pos)
        if direction == "LONG":
            long_notional += notional
        elif direction == "SHORT":
            short_notional += notional

    additional_notional = max(
        0.0, float(additional_margin or 0) * max(float(additional_leverage or 0), 1.0)
    )
    projected_long = long_notional
    projected_short = short_notional
    if additional_direction:
        if additional_direction.upper() == "LONG":
            projected_long += additional_notional
        elif additional_direction.upper() == "SHORT":
            projected_short += additional_notional

    current_total = long_notional + short_notional
    projected_total = projected_long + projected_short
    projected_open_positions = len(positions) + (
        1 if additional_notional > 0 and additional_position else 0
    )
    return {
        "currentOpenPositions": len(positions),
        "projectedOpenPositions": projected_open_positions,
        "longNotional": round(long_notional, 2),
        "shortNotional": round(short_notional, 2),
        "totalNotional": round(current_total, 2),
        "projectedLongNotional": round(projected_long, 2),
        "projectedShortNotional": round(projected_short, 2),
        "projectedTotalNotional": round(projected_total, 2),
        "projectedLongPct": round(projected_long / projected_total * 100, 2)
        if projected_total > 0
        else 0.0,
        "projectedShortPct": round(projected_short / projected_total * 100, 2)
        if projected_total > 0
        else 0.0,
    }


def check_directional_exposure_limit(
    direction: str,
    additional_margin: float,
    additional_leverage: float,
    *,
    additional_position: bool = True,
) -> tuple[bool, dict]:
    """Check whether a new or expanded position would breach the directional cap.

    The first position is allowed. After that, the projected book must respect
    the configured directional cap.
    """
    regime = load_regime()
    guardrails = regime.get("globalGuardrails", {})
    cap_pct = float(guardrails.get("directionalCapPct", 70) or 70)
    snapshot = directional_exposure_snapshot(
        additional_direction=direction,
        additional_margin=additional_margin,
        additional_leverage=additional_leverage,
        additional_position=additional_position,
    )
    offending_pct = (
        snapshot["projectedLongPct"]
        if direction.upper() == "LONG"
        else snapshot["projectedShortPct"]
    )
    snapshot["capPct"] = cap_pct
    snapshot["offendingPct"] = offending_pct

    if snapshot["projectedOpenPositions"] <= 1:
        return True, snapshot
    return offending_pct <= cap_pct, snapshot


def build_position_playbook_metadata(
    *,
    scanner: str,
    score: int | float = 0,
    margin: float = 0,
    leverage: float = 0,
    reasons: list[str] | None = None,
    sm_snapshot: dict | None = None,
    setup: dict | None = None,
) -> dict:
    """Build normalized position metadata for the local playbook/supervisor."""
    scanner_key = str(scanner or "unknown").lower()
    profile = current_scanner_profile(scanner_key)
    signal_policy = load_brain_state().get("signalPolicy", {})
    priority = profile.get(
        "priority", signal_policy.get("priorityByScanner", {}).get(scanner_key, 50)
    )
    fast_scanners = {"orca", "komodo", "sentinel", "shark", "rhino"}
    dead_weight_min = profile.get(
        "deadWeightMin", 20 if scanner_key in fast_scanners else 45
    )
    return {
        "schemaVersion": "1.0",
        "scanner": scanner_key,
        "profileVersion": profile.get("version", "default"),
        "priority": priority,
        "entry": {
            "score": float(score or 0),
            "marginUsd": round(float(margin or 0), 2),
            "leverage": float(leverage or 0),
            "notionalUsd": round(float(margin or 0) * float(leverage or 0), 2),
        },
        "signal": {
            "reasons": list(reasons or [])[:8],
            "setup": setup or {},
        },
        "smSnapshot": sm_snapshot or {},
        "rotation": {
            "eligible": True,
            "deadWeightMin": dead_weight_min,
            "minHighWaterRoe": profile.get("minHighWaterRoe", 2.0),
            "closeIfNegative": True,
            "priorityGap": profile.get("rotationPriorityGap", 8),
        },
        "collapse": {
            "minTraderRatio": profile.get("minTraderRatio", 0.2),
            "minTraderCountFloor": profile.get("minTraderCountFloor", 24),
            "minConvictionRatio": profile.get("minConvictionRatio", 0.5),
            "minConcentrationRatio": profile.get("minConcentrationRatio", 0.5),
        },
        "realizedEdge": {
            "score": profile.get("realizedEdgeScore", 0.0),
            "confidence": profile.get("sampleConfidence", 0.0),
            "closes": profile.get("sampleCloses", 0),
        },
    }


def attach_position_playbook(
    dsl_state: dict,
    *,
    scanner: str,
    margin: float,
    leverage: float,
    score: int | float = 0,
    reasons: list[str] | None = None,
    sm_snapshot: dict | None = None,
    setup: dict | None = None,
) -> dict:
    """Attach normalized playbook metadata to a DSL state dict."""
    playbook = build_position_playbook_metadata(
        scanner=scanner,
        score=score,
        margin=margin,
        leverage=leverage,
        reasons=reasons,
        sm_snapshot=sm_snapshot,
        setup=setup,
    )
    dsl_state["scanner"] = str(scanner or "unknown").lower()
    dsl_state["margin"] = round(float(margin or dsl_state.get("margin", 0) or 0), 2)
    dsl_state["notionalUsd"] = round(
        dsl_state["margin"] * float(leverage or dsl_state.get("leverage", 0) or 0),
        2,
    )
    dsl_state["playbook"] = playbook

    snapshot = playbook.get("smSnapshot", {})
    if "traderCount" in snapshot:
        dsl_state["entrySmTraderCount"] = snapshot["traderCount"]
    if "conviction" in snapshot:
        dsl_state["entrySmConviction"] = snapshot["conviction"]
    if "concentration" in snapshot:
        dsl_state["entrySmConcentration"] = snapshot["concentration"]
    return dsl_state


def count_open_slots(strategy: dict) -> int:
    """How many slots are free in this strategy. Respects gate state and dynamic unlocking."""
    if strategy.get("gateState", "OPEN") != "OPEN":
        return 0
    max_slots = strategy.get("maxSlots", 2)
    regime_slots = current_regime_params().get("maxSlots")
    if isinstance(regime_slots, (int, float)):
        max_slots = min(max_slots, int(regime_slots))

    policy = current_brain_policy()
    strategy_caps = policy.get("strategyCaps", {})
    strat_cap = strategy_caps.get(strategy.get("_key", ""), {})
    strat_max_slots = strat_cap.get("maxSlotsCap")
    if isinstance(strat_max_slots, (int, float)):
        max_slots = min(max_slots, int(strat_max_slots))

    # Dynamic slot unlocking (senpi-skills v6.3 pattern)
    dynamic = strategy.get("dynamicSlots", {})
    if dynamic.get("enabled", False):
        absolute_max = dynamic.get("absoluteMax", max_slots)
        # Compute today's realized PnL from trade journal
        journal = load_trade_journal()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = sum(
            float(t.get("realizedPnl", 0))
            for t in journal
            if t.get("action") == "CLOSE"
            and t.get("strategyKey") == strategy["_key"]
            and t.get("recordedAt", "").startswith(today)
        )
        for threshold in sorted(
            dynamic.get("unlockThresholds", []),
            key=lambda x: x.get("pnl", 0),
            reverse=True,
        ):
            if daily_pnl >= threshold.get("pnl", 0):
                max_slots = min(threshold.get("maxEntries", max_slots), absolute_max)
                break

    open_count = len(get_open_positions(strategy["_key"]))
    return max(0, max_slots - open_count)


# ---------------------------------------------------------------------------
# Pending entries queue
# ---------------------------------------------------------------------------


def load_pending_entries() -> list[dict]:
    return load_json(PENDING_ENTRIES_FILE, default=[])


def save_pending_entries(entries: list[dict]):
    save_json(PENDING_ENTRIES_FILE, entries)


def add_pending_entry(entry: dict):
    """Append an entry to the pending queue."""
    entries = load_pending_entries()
    entry["queuedAt"] = now_iso()
    brain = load_brain_state()
    policy = brain.get("executionPolicy", {}) if isinstance(brain, dict) else {}
    signal_policy = brain.get("signalPolicy", {}) if isinstance(brain, dict) else {}
    scanner = (
        entry.get("scanner")
        or entry.get("source")
        or entry.get("entryMode")
        or entry.get("mode")
        or "unknown"
    )
    scanner_key = str(scanner).lower()
    entry["brainContext"] = {
        "brainAt": brain.get("generatedAt"),
        "mode": policy.get("mode", "UNSET"),
        "priority": signal_policy.get("priorityByScanner", {}).get(scanner_key, 0),
        "blockedScanner": scanner_key in signal_policy.get("blockedScanners", []),
        "preferredScanner": scanner_key in signal_policy.get("preferredScanners", []),
    }
    entries.append(entry)
    save_pending_entries(entries)


# ---------------------------------------------------------------------------
# Trade journal
# ---------------------------------------------------------------------------


def load_trade_journal() -> list[dict]:
    return load_json(TRADE_JOURNAL_FILE, default=[])


def record_trade(trade: dict):
    """Append a trade to the journal."""
    journal = load_trade_journal()
    trade["recordedAt"] = now_iso()
    journal.append(trade)
    save_json(TRADE_JOURNAL_FILE, journal)


def is_rotation_cooled_down(asset: str, cooldown_minutes: int = 45) -> bool:
    """Check if an asset was closed too recently (rotation cooldown).

    Per senpi-skills v6.3: prevents re-entry within 45 min of closing
    a position on the same asset, avoiding churn from close+reopen cycles.
    Returns True if the asset is still in cooldown (should NOT enter).
    """
    journal = load_trade_journal()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    for trade in reversed(journal):
        recorded = trade.get("recordedAt", "")
        if not recorded:
            continue
        try:
            trade_time = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
            if trade_time < cutoff:
                break  # No more recent trades to check
            if trade.get("action") == "CLOSE" and trade.get("asset") == asset:
                return True
        except (ValueError, TypeError):
            continue
    return False


# ---------------------------------------------------------------------------
# Hard safety gates (non-negotiable)
# ---------------------------------------------------------------------------

# Default guardrails — used if globalGuardrails is missing from risk-regime.json
DEFAULT_GLOBAL_GUARDRAILS = {
    "dailyLossLimitPct": 10,
    "catastrophicDrawdownPct": 20,
    "maxConsecutiveStopOuts": 4,
    "directionalCapPct": 70,
    "minLeverage": 7,
    "maxLeverage": 10,
    "maxPositionsTotal": 3,
    "perAssetCooldownMinutes": 120,
    "bannedAssetPrefixes": ["xyz:"],
}


def load_global_guardrails() -> dict:
    """Load globalGuardrails from risk-regime.json, falling back to defaults."""
    regime = load_regime()
    guardrails = regime.get("globalGuardrails", {})
    merged = dict(DEFAULT_GLOBAL_GUARDRAILS)
    merged.update({k: v for k, v in guardrails.items() if v is not None})
    return merged


def clamp_leverage(leverage: float) -> int:
    """Clamp leverage to the hard 7-10x band. Returns clamped integer."""
    guardrails = load_global_guardrails()
    min_lev = int(guardrails.get("minLeverage", 7))
    max_lev = int(guardrails.get("maxLeverage", 10))
    return max(min_lev, min(max_lev, int(leverage)))


def is_asset_banned(asset: str) -> bool:
    """Check if an asset is banned (XYZ equities or other configured bans)."""
    guardrails = load_global_guardrails()
    prefixes = guardrails.get("bannedAssetPrefixes", ["xyz:"])
    asset_lower = str(asset).lower()
    for prefix in prefixes:
        if asset_lower.startswith(prefix.lower()):
            return True
    return False


def check_hard_cooldown(asset: str) -> bool:
    """Check 120-minute per-asset cooldown. Returns True if STILL IN COOLDOWN (should NOT enter)."""
    guardrails = load_global_guardrails()
    cooldown_min = int(guardrails.get("perAssetCooldownMinutes", 120))
    return is_rotation_cooled_down(asset, cooldown_min)


# ---------------------------------------------------------------------------
# Senpi MCP interface (CENTRALIZED ENTRY POINT)
# ---------------------------------------------------------------------------
# IMPORTANT: mcporter_call() is the ONLY function that should interact with
# the Senpi MCP server. All components must use this wrapper for MCP calls.
# Do NOT create alternative MCP interfaces or direct HTTP calls to Senpi.
# ---------------------------------------------------------------------------


READ_ONLY_TOOLS = frozenset(
    {
        "leaderboard_get_markets",
        "leaderboard_get_momentum_events",
        "leaderboard_get_top",
        "market_get_asset_data",
        "market_get_candles",
        "market_get_orderbook",
        "market_get_instrument_specs",
        "market_get_prices",
        "market_list_instruments",
        "market_get_all_instruments",
        "account_get_portfolio",
    }
)


def mcporter_read(tool: str, args: dict, *, timeout: int = 30) -> dict:
    """Read-only MCP wrapper for scanner scripts. Blocks all write tools."""
    if tool not in READ_ONLY_TOOLS:
        return {
            "error": f"mcporter_read: write tool '{tool}' blocked in scanner context",
            "success": False,
        }
    return mcporter_call(tool, args, timeout=timeout)


def _senpi_mcp_request(tool: str, args: dict, *, timeout: int = 30) -> dict:
    """
    Direct HTTP JSON-RPC call to Senpi MCP server.
    Bypasses mcporter CLI which is broken for Senpi.
    """
    import urllib.request
    import urllib.error
    import json

    url = os.environ.get("SENPI_MCP_URL", "https://mcp.prod.senpi.ai/mcp")
    auth_token = os.environ.get("SENPI_AUTH_TOKEN", "")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }

    headers = {
        "Content-Type": "application/json",
    }
    if auth_token:
        headers["Authorization"] = auth_token

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            # MCP returns content array with text items
            if "result" in result and "content" in result["result"]:
                for item in result["result"]["content"]:
                    if item.get("type") == "text":
                        try:
                            return json.loads(item["text"])
                        except json.JSONDecodeError:
                            return {"data": item["text"], "success": True}
            if "error" in result:
                return {"error": result["error"], "success": False}
            return result.get("result", {"success": True})
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}", "success": False}
    except urllib.error.URLError as e:
        return {"error": f"URL error: {e.reason}", "success": False}
    except Exception as e:
        return {"error": str(e), "success": False}


def mcporter_call(tool: str, args: dict, *, timeout: int = 30) -> dict:
    """
    CENTRALIZED ENTRY POINT for all Senpi MCP server interactions.

    Uses direct HTTP JSON-RPC (mcporter CLI bypassed for reliability).
    Returns parsed JSON response or dict with 'error' key on failure.
    """
    return _senpi_mcp_request(tool, args, timeout=timeout)


def _mcporter_call_legacy(tool: str, args: dict, *, timeout: int = 30) -> dict:
    """
    LEGACY: Call a Senpi MCP tool via mcporter CLI.
    Kept for reference but not used - mcporter is broken for Senpi.
    """
    import subprocess
    import json
    import time

    inner_cmd = ["mcporter", "call", f"senpi.{tool}"]
    for k, v in args.items():
        if isinstance(v, (list, dict)):
            inner_cmd.append(f"{k}={json.dumps(v)}")
        elif isinstance(v, bool):
            inner_cmd.append(f"{k}={'true' if v else 'false'}")
        else:
            inner_cmd.append(f"{k}={v}")

    # Wrap with `timeout` command for reliable kill
    cmd = ["timeout", "--signal=KILL", str(timeout)] + inner_cmd

    last_result = {}
    for attempt in range(3):
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            stdout, stderr = proc.communicate()
            if proc.returncode == 137:  # killed by timeout
                last_result = {"error": "timeout", "success": False}
                if attempt < 2:
                    time.sleep(1)
                    continue
                return last_result
            if proc.returncode != 0:
                err = stderr.strip()
                last_result = {"error": err, "success": False}
                # Retry on transient MCP errors
                if (
                    "Connection closed" in err
                    or "appears offline" in err
                    or "timeout" in err.lower()
                ):
                    if attempt < 2:
                        time.sleep(1.5)
                        continue
                return last_result
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "error": f"invalid json: {stdout[:200] if stdout else 'empty'}",
                "success": False,
            }
        except Exception as e:
            last_result = {"error": str(e), "success": False}
            if attempt < 2:
                time.sleep(1)
                continue
    return last_result


def mcporter_call_retry(
    tool: str,
    args: dict,
    *,
    timeout: int = 30,
    max_attempts: int = 4,
    delay: float = 1.0,
) -> dict:
    """mcporter_call with retry logic.

    Retries up to max_attempts times with delay between attempts.
    Only retries on transient errors (timeout, connection), not on valid API errors.
    """
    last_result = {}
    for attempt in range(max_attempts):
        result = mcporter_call(tool, args, timeout=timeout)
        if "error" not in result:
            return result
        err = result.get("error", "")
        # Don't retry on non-transient errors (valid API error responses)
        if err not in ("timeout",) and "timed out" not in err and "URLError" not in err:
            return result
        last_result = result
        if attempt < max_attempts - 1:
            time.sleep(delay)
    return last_result


# ---------------------------------------------------------------------------
# Atomic trade locking (position count modifications)
# ---------------------------------------------------------------------------


@contextmanager
def acquire_trade_lock():
    """Acquire exclusive file lock for position count operations.

    Ensures only one component can check or modify the position count at a time.
    Uses flock(LOCK_EX) for atomic locking across processes.

    Usage:
        with acquire_trade_lock():
            count = len(get_all_open_positions())
            # ... modify positions ...
    """
    TRADE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOCK_FILE, "w") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Locking (prevent cron overlap)
# ---------------------------------------------------------------------------


def acquire_lock(name: str) -> bool:
    """Simple file-based lock. Returns True if acquired."""
    LOCKFILE_DIR.mkdir(parents=True, exist_ok=True)
    lockfile = LOCKFILE_DIR / f"{name}.lock"
    if lockfile.exists():
        # Check if stale (>5 min old)
        age = time.time() - lockfile.stat().st_mtime
        if age < 60:
            return False
        log(f"Stale lock for {name} ({age:.0f}s old), removing")
    lockfile.write_text(str(os.getpid()))
    return True


def release_lock(name: str):
    lockfile = LOCKFILE_DIR / f"{name}.lock"
    lockfile.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Git sync
# ---------------------------------------------------------------------------


def git_sync(message: str = "auto: state update"):
    """Stage all changes in STATE_DIR and push. Uses a global lock to prevent concurrent pushes."""
    if not acquire_lock("git-sync"):
        log("git sync: another sync in progress — skipping")
        return
    try:
        subprocess.run(
            ["git", "add", "-A"], cwd=STATE_DIR, capture_output=True, timeout=10
        )
        # Only commit if there are changes
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=STATE_DIR,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:  # There are staged changes
            subprocess.run(
                ["git", "commit", "-m", message, "--no-verify"],
                cwd=STATE_DIR,
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["git", "push", "--quiet"],
                cwd=STATE_DIR,
                capture_output=True,
                timeout=30,
            )
    except subprocess.TimeoutExpired:
        log("git sync timeout — will retry next cycle")
    finally:
        release_lock("git-sync")


def git_pull():
    """Pull latest state (Oz agents may have pushed config changes)."""
    try:
        subprocess.run(
            ["git", "pull", "--rebase", "--quiet"],
            cwd=STATE_DIR,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        log("git pull timeout")


# ---------------------------------------------------------------------------
# Cron heartbeat monitoring
# ---------------------------------------------------------------------------

HEARTBEAT_FILE = OUTPUTS_DIR / "cron-heartbeats.json"


def record_heartbeat(cron_name: str):
    """Record that a cron job has just run. Called at start of each scanner."""
    heartbeats = load_json(HEARTBEAT_FILE, default={})
    heartbeats[cron_name] = now_iso()
    save_json(HEARTBEAT_FILE, heartbeats)


def check_stale_heartbeats(
    max_stale_minutes: dict[str, int] | None = None,
) -> list[str]:
    """Return list of cron names that haven't run within their expected window.

    max_stale_minutes maps cron name → max minutes before considered stale.
    Defaults to 2x the expected interval for safety margin.
    """
    defaults = {
        "orca": 3,  # runs every 60s, stale after 3 min
        "mantis": 4,  # runs every 90s, stale after 4 min
        "fox": 4,  # runs every 90s, stale after 4 min
        "komodo": 12,  # runs every 5min, stale after 12 min
        "condor": 8,  # runs every 3min, stale after 8 min
        "polar": 8,  # runs every 3min, stale after 8 min
        # PAUSED: barracuda — removed from schedule
        # PAUSED: bison     — removed from schedule
        # PAUSED: shark     — Senpi paused (v1.0, -4.3% ROI)
        "rhino": 8,  # runs every 3min, stale after 8 min
        "sentinel": 8,  # runs every 3min, stale after 8 min
        "dsl-runner": 8,  # runs every 3min, stale after 8 min
        "sm-flip": 12,  # runs every 5min, stale after 12 min
        "watchdog": 12,  # runs every 5min, stale after 12 min
        "risk-arbiter": 3,  # runs every 30s, stale after 3 min
        "arena": 35,  # runs every 15min, stale after 35 min
        "brain": 12,  # runs every 5min, stale after 12 min
    }
    if max_stale_minutes:
        defaults.update(max_stale_minutes)

    heartbeats = load_json(HEARTBEAT_FILE, default={})
    now = datetime.now(timezone.utc)
    stale = []

    for cron_name, max_min in defaults.items():
        last_run = heartbeats.get(cron_name)
        if not last_run:
            continue  # Never ran — don't alert on first boot
        try:
            last_time = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            if (now - last_time).total_seconds() > max_min * 60:
                stale.append(cron_name)
        except (ValueError, TypeError):
            continue

    return stale


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram(message: str):
    """Send a Telegram alert. Reads token and chat_id from env."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import urllib.request

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps(
            {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        ).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Telegram send failed: {e}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)
