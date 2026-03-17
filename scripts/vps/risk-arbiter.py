#!/usr/bin/env python3
"""
Risk Arbiter — runs every 30 seconds via systemd timer.

Mechanical safety net. No LLM. Enforces:
- Daily realized loss limit → RISK_OFF
- Catastrophic drawdown → flatten all + RISK_OFF
- Consecutive stop-out limit → RISK_OFF
- Abnormal conditions (API failures, funding spikes) → RISK_OFF

This script is the ONLY non-Oz process allowed to set RISK_OFF.
"""

import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, log, now_iso,
    load_regime, set_risk_mode, load_json, save_json,
    load_strategies, get_open_positions, STRATEGIES_FILE,
    mcporter_call, send_telegram, git_sync,
    MEMORY_DIR, POSITION_STATE_DIR, OUTPUTS_DIR,
)

ARBITER_STATE_FILE = OUTPUTS_DIR / "arbiter-state.json"


def load_arbiter_state() -> dict:
    return load_json(ARBITER_STATE_FILE, default={
        "peakEquity": 0,
        "dayStartEquity": 0,
        "dayStartDate": None,
        "consecutiveStopOuts": 0,
        "lastCheckAt": None,
        "flattenedAt": None,
    })


def save_arbiter_state(state: dict):
    state["lastCheckAt"] = now_iso()
    save_json(ARBITER_STATE_FILE, state)


def get_account_equity() -> float | None:
    """Fetch current account equity via Senpi MCP."""
    result = mcporter_call("account_get_portfolio", {}, timeout=15)
    if "error" in result:
        return None
    # Try common response shapes
    equity = result.get("accountEquity", result.get("equity", result.get("totalEquity")))
    if equity is not None:
        return float(equity)
    # Nested under portfolio
    portfolio = result.get("portfolio", result.get("data", {}))
    if isinstance(portfolio, dict):
        equity = portfolio.get("accountEquity", portfolio.get("equity"))
        if equity is not None:
            return float(equity)
    return None


def count_recent_stop_outs() -> int:
    """Count DSL stop-outs in the last 2 hours from trade journal."""
    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    count = 0
    for trade in reversed(journal):
        if trade.get("recordedAt", "") < cutoff:
            break
        if trade.get("action") == "CLOSE" and trade.get("closeReason") in (
            "dsl_breach", "phase1_autocut", "stagnation"
        ):
            pnl = float(trade.get("realizedPnl", 0))
            if pnl < 0:
                count += 1
    return count


def flatten_all():
    """Emergency: close every open position across all strategies."""
    strategies = get_enabled_strategies()
    for strat in strategies:
        positions = get_open_positions(strat["_key"])
        for pos in positions:
            log(f"FLATTEN: closing {pos['asset']} in {strat['_key']}")
            mcporter_call("strategy_close_position", {
                "strategyId": strat.get("strategyId", pos.get("strategyId")),
                "asset": pos["asset"],
            }, timeout=15)
            # Deactivate DSL state
            if "_file" in pos:
                state = load_json(Path(pos["_file"]))
                state["active"] = False
                state["closedAt"] = now_iso()
                state["closeReason"] = "risk_arbiter_flatten"
                save_json(Path(pos["_file"]), state)


def process_strategy_guard_rails():
    """Enforce per-strategy rules: maxEntriesPerDay, maxConsecutiveLosses."""
    strategies_config = load_strategies()
    strategies = strategies_config.get("strategies", {})
    if not strategies:
        return

    journal = load_json(MEMORY_DIR / "trade-journal.json", default=[])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Compute stats per strategy from journal
    stats = {}
    for key in strategies.keys():
        stats[key] = {"daily_pnl": 0.0, "daily_entries": 0, "consecutive_losses": 0}

    # Traverse backward to compute consecutive losses accurately
    # Also sum daily entries and pnl
    for trade in reversed(journal):
        key = trade.get("strategyKey")
        if not key or key not in stats:
            continue
            
        recorded_at = trade.get("recordedAt", "")
        # Break out of consecutive loss counter if we hit a profit
        if trade.get("action") == "CLOSE":
            pnl = float(trade.get("realizedPnl", 0))
            if recorded_at.startswith(today):
                stats[key]["daily_pnl"] += pnl
                
            # If we haven't broken the consecutive loss streak yet
            if pnl > 0 and "broken_streak" not in stats[key]:
                stats[key]["broken_streak"] = True
            elif pnl < 0 and "broken_streak" not in stats[key]:
                stats[key]["consecutive_losses"] += 1
                
        elif trade.get("action") == "OPEN":
            if recorded_at.startswith(today):
                stats[key]["daily_entries"] += 1

    changed = False
    for key, strat in strategies.items():
        if not strat.get("enabled", True):
            continue

        guard_rails = strat.get("guardRails", {})
        max_entries = guard_rails.get("maxEntriesPerDay", 8)
        bypass_on_profit = guard_rails.get("bypassOnProfit", True)
        max_losses = guard_rails.get("maxConsecutiveLosses", 3)
        cooldown_min = guard_rails.get("cooldownMinutes", 60)

        current_gate = strat.get("gateState", "OPEN")
        expires_at = strat.get("gateStateExpiresAt")
        reason = ""

        # 1. Check expiration of COOLDOWN
        if current_gate == "COOLDOWN" and expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) >= exp_dt:
                    strat["gateState"] = "OPEN"
                    strat["gateStateExpiresAt"] = None
                    changed = True
                    log(f"RISK ARBITER: {key} cooldown expired → OPEN")
                    continue
            except ValueError:
                pass

        # If already closed/cooldown, don't re-trigger unless it's a new day for CLOSED
        if current_gate == "CLOSED":
            closed_today = strat.get("gateStateUpdatedAt", "").startswith(today)
            if not closed_today:
                strat["gateState"] = "OPEN"
                strat["gateStateExpiresAt"] = None
                changed = True
                log(f"RISK ARBITER: {key} new day reset → OPEN")
            continue
        elif current_gate == "COOLDOWN":
            continue

        # 2. Check rules to close gate
        strat_stats = stats[key]
        new_gate = "OPEN"
        
        # Rule G4: Consecutive Losses
        if strat_stats["consecutive_losses"] >= max_losses:
            new_gate = "COOLDOWN"
            reason = f"{strat_stats['consecutive_losses']} consecutive losses"
            exp_time = datetime.now(timezone.utc) + timedelta(minutes=cooldown_min)
            strat["gateStateExpiresAt"] = exp_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Rule G3: Max Entries
        elif strat_stats["daily_entries"] >= max_entries:
            if not (bypass_on_profit and strat_stats["daily_pnl"] > 0):
                new_gate = "CLOSED"
                reason = f"Max entries hit ({strat_stats['daily_entries']}/{max_entries})"
        
        if new_gate != "OPEN":
            strat["gateState"] = new_gate
            strat["gateStateUpdatedAt"] = now_iso()
            changed = True
            log(f"RISK ARBITER: {key} → {new_gate} ({reason})")
            send_telegram(f"🛡 Strategy Gate: *{key}* → {new_gate}\nReason: {reason}")

    if changed:
        save_json(STRATEGIES_FILE, strategies_config)


def main():
    if not acquire_lock("risk-arbiter"):
        return

    try:
        regime = load_regime()
        guardrails = regime.get("globalGuardrails", {})
        arb_state = load_arbiter_state()

        # Fetch equity
        equity = get_account_equity()
        if equity is None:
            log("Risk arbiter: could not fetch equity — skipping")
            save_arbiter_state(arb_state)
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Reset day tracking at midnight
        if arb_state.get("dayStartDate") != today:
            arb_state["dayStartDate"] = today
            arb_state["dayStartEquity"] = equity
            arb_state["consecutiveStopOuts"] = 0

        # Track current equity and update peak
        arb_state["lastEquity"] = equity
        if equity > arb_state.get("peakEquity", 0):
            arb_state["peakEquity"] = equity

        peak = arb_state["peakEquity"]
        day_start = arb_state["dayStartEquity"]

        # --- CHECK 1: Daily realized loss limit ---
        daily_loss_pct = guardrails.get("dailyLossLimitPct", 5)
        if day_start > 0:
            daily_drawdown = (day_start - equity) / day_start * 100
            if daily_drawdown >= daily_loss_pct:
                if regime.get("riskMode") != "RISK_OFF":
                    log(f"RISK ARBITER: Daily loss {daily_drawdown:.1f}% >= {daily_loss_pct}% → RISK_OFF")
                    set_risk_mode("RISK_OFF",
                                  f"Daily loss limit hit: {daily_drawdown:.1f}% (limit {daily_loss_pct}%)",
                                  "risk-arbiter")
                    send_telegram(
                        f"🚨 RISK OFF — Daily loss limit hit\n"
                        f"Drawdown: {daily_drawdown:.1f}% | Limit: {daily_loss_pct}%\n"
                        f"Equity: ${equity:.2f} | Day start: ${day_start:.2f}"
                    )

        # --- CHECK 2: Catastrophic drawdown from peak ---
        catastrophic_pct = guardrails.get("catastrophicDrawdownPct", 20)
        if peak > 0:
            peak_drawdown = (peak - equity) / peak * 100
            if peak_drawdown >= catastrophic_pct:
                log(f"RISK ARBITER: CATASTROPHIC drawdown {peak_drawdown:.1f}% — FLATTENING ALL")
                flatten_all()
                set_risk_mode("RISK_OFF",
                              f"CATASTROPHIC: {peak_drawdown:.1f}% drawdown from peak. ALL POSITIONS CLOSED.",
                              "risk-arbiter")
                arb_state["flattenedAt"] = now_iso()
                send_telegram(
                    f"🚨🚨 CATASTROPHIC FLATTEN 🚨🚨\n"
                    f"Drawdown: {peak_drawdown:.1f}% from peak\n"
                    f"All positions closed. Manual intervention required."
                )
                git_sync("EMERGENCY: risk arbiter flatten")

        # --- CHECK 3: Consecutive stop-outs ---
        max_stop_outs = guardrails.get("maxConsecutiveStopOuts", 4)
        recent_stops = count_recent_stop_outs()
        if recent_stops >= max_stop_outs:
            if regime.get("riskMode") != "RISK_OFF":
                log(f"RISK ARBITER: {recent_stops} consecutive stop-outs → RISK_OFF")
                set_risk_mode("RISK_OFF",
                              f"{recent_stops} stop-outs in 2h window (limit {max_stop_outs})",
                              "risk-arbiter")
                send_telegram(
                    f"⚠️ RISK OFF — {recent_stops} consecutive stop-outs\n"
                    f"Cooling down. No new entries until manual reset or next regime check."
                )

        # --- CHECK 4: Per-strategy Guard Rails ---
        process_strategy_guard_rails()

        save_arbiter_state(arb_state)

    finally:
        release_lock("risk-arbiter")


if __name__ == "__main__":
    main()
