#!/usr/bin/env python3
"""
Job 1: Emerging Movers Scanner — runs every 60 seconds.

Calls Senpi's leaderboard_get_markets, detects acceleration signals,
and auto-enters on FIRST_JUMP/CONTRIB_EXPLOSION when criteria are met.

This is the speed edge: <2s from signal to position, no LLM needed.
"""

import sys
from pathlib import Path

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, git_pull, git_sync, log,
    load_json, save_json, now_iso,
    SCAN_HISTORY_FILE, POSITION_STATE_DIR, SCANNER_CONFIG_FILE,
    load_regime, current_regime_params, is_entries_allowed, is_auto_entry_enabled,
    get_enabled_strategies, count_open_slots, get_strategy_state_dir,
    add_pending_entry, record_trade, send_telegram,
    mcporter_call,
)

MAX_SCAN_HISTORY = 60  # Keep last 60 scans (~1 hour at 60s)


def fetch_leaderboard() -> list[dict]:
    """Single API call to get SM profit concentration leaderboard."""
    result = mcporter_call("leaderboard_get_markets", {})
    if "error" in result:
        log(f"Leaderboard fetch failed: {result['error']}")
        return []
    # Normalize: result may be nested under various keys depending on mcporter version
    markets = result.get("markets", result.get("data", result))
    if isinstance(markets, list):
        return markets
    return []


def detect_signals(current: list[dict], history: list[dict]) -> list[dict]:
    """Compare current scan with history to detect acceleration signals."""
    if not current:
        return []

    prev = history[-1]["markets"] if history else []
    prev_by_asset = {m.get("asset", ""): m for m in prev}

    signals = []
    for i, market in enumerate(current[:50]):  # Top 50 only
        asset = market.get("asset", "")
        rank = i + 1
        direction = market.get("direction", market.get("side", ""))
        contrib = float(market.get("contribution", market.get("pctOfTotal", 0)))
        traders = int(market.get("traderCount", market.get("traders", 0)))

        prev_market = prev_by_asset.get(asset)
        prev_rank = None
        prev_contrib = 0
        if prev_market:
            prev_rank = prev.index(prev_market) + 1 if prev_market in prev else None
            prev_contrib = float(prev_market.get("contribution", prev_market.get("pctOfTotal", 0)))

        # Build reasons list
        reasons = []
        signal_type = None

        # FIRST_JUMP: 10+ rank jump from #25+ in ONE scan
        if prev_rank and prev_rank >= 25 and (prev_rank - rank) >= 10:
            reasons.append("FIRST_JUMP")
            signal_type = "FIRST_JUMP"
        elif prev_rank is None and rank <= 20:
            reasons.append("NEW_ENTRY_DEEP")
            signal_type = "NEW_ENTRY_DEEP"

        # CONTRIB_EXPLOSION: 3x+ contribution increase
        if prev_contrib > 0 and contrib >= prev_contrib * 3 and rank <= 20:
            reasons.append("CONTRIB_EXPLOSION")
            if signal_type != "FIRST_JUMP":
                signal_type = "CONTRIB_EXPLOSION"

        # DEEP_CLIMBER: 5+ rank jump from #25+
        if prev_rank and prev_rank >= 25 and (prev_rank - rank) >= 5 and signal_type is None:
            reasons.append("DEEP_CLIMBER")
            signal_type = "DEEP_CLIMBER"

        # RANK_UP: 2+ positions
        if prev_rank and (prev_rank - rank) >= 2 and not reasons:
            reasons.append("RANK_UP")

        # Compute velocity from history
        velocity = 0
        if len(history) >= 2:
            older = history[-2]["markets"] if len(history) >= 2 else []
            older_by_asset = {m.get("asset", ""): m for m in older}
            if asset in older_by_asset:
                old_contrib = float(older_by_asset[asset].get("contribution", 0))
                if old_contrib > 0:
                    velocity = (contrib - old_contrib) / old_contrib

        # Quality filters (v3.1)
        erratic = _check_erratic(asset, history)
        low_velocity = velocity < 0.03 if signal_type in ("FIRST_JUMP", "CONTRIB_EXPLOSION") else False

        if reasons:
            signals.append({
                "asset": asset,
                "direction": direction,
                "rank": rank,
                "prevRank": prev_rank,
                "contribution": contrib,
                "traderCount": traders,
                "reasons": reasons,
                "signalType": signal_type,
                "contribVelocity": round(velocity, 4),
                "erratic": erratic,
                "lowVelocity": low_velocity and signal_type != "FIRST_JUMP",  # First jumps exempt
                "timestamp": now_iso(),
            })

    return signals


def _check_erratic(asset: str, history: list[dict]) -> bool:
    """Check if asset has >5 rank reversals in scan history."""
    ranks = []
    for scan in history[-10:]:
        for i, m in enumerate(scan.get("markets", [])[:50]):
            if m.get("asset") == asset:
                ranks.append(i + 1)
                break
    if len(ranks) < 3:
        return False
    reversals = 0
    for i in range(2, len(ranks)):
        if (ranks[i] - ranks[i-1]) * (ranks[i-1] - ranks[i-2]) < 0:
            reversals += 1
    return reversals > 5


def try_auto_entry(signal: dict):
    """
    Attempt immediate entry on a high-conviction signal.
    This is where the speed edge lives.
    """
    config = load_json(SCANNER_CONFIG_FILE)
    auto_cfg = config.get("emAutoEntry", {})

    if not auto_cfg.get("enabled", False):
        return
    if not is_auto_entry_enabled():
        return
    if signal["signalType"] not in auto_cfg.get("signalTypes", []):
        return
    if len(signal["reasons"]) < auto_cfg.get("minReasons", 2):
        return
    if signal["rank"] > auto_cfg.get("maxEntryRank", 25):
        return
    if signal["traderCount"] < auto_cfg.get("minTraderCount", 10):
        return
    if signal["erratic"]:
        return

    # Find a strategy with free slots
    regime_params = current_regime_params()
    strategies = get_enabled_strategies()
    target_strategy = None

    for strat in strategies:
        # Check if this asset is already open in this strategy
        state_dir = get_strategy_state_dir(strat["_key"])
        dsl_file = state_dir / f"dsl-{signal['asset']}.json"
        existing = load_json(dsl_file, default=None)
        if existing and existing.get("active", False):
            continue  # Already holding this asset in this strategy

        if count_open_slots(strat) > 0:
            target_strategy = strat
            break

    if not target_strategy:
        log(f"Auto-entry: no free slots for {signal['asset']}")
        return

    # Calculate position size
    budget = target_strategy.get("budget", 1000)
    alloc_pct = regime_params.get("allocPctPerSlot", 30) / 100
    leverage = min(
        target_strategy.get("defaultLeverage", 10),
        regime_params.get("maxLeverageCrypto", 10),
    )
    margin = budget * alloc_pct

    # Execute entry via mcporter
    log(f"AUTO-ENTRY: {signal['direction']} {signal['asset']} | "
        f"margin=${margin:.0f} lev={leverage}x | "
        f"signal={signal['signalType']} reasons={signal['reasons']}")

    entry_result = mcporter_call("strategy_create_position", {
        "strategyId": target_strategy.get("strategyId"),
        "asset": signal["asset"],
        "direction": signal["direction"],
        "marginUsd": margin,
        "leverage": leverage,
    })

    if "error" in entry_result:
        log(f"Auto-entry FAILED for {signal['asset']}: {entry_result['error']}")
        return

    entry_price = float(entry_result.get("entryPrice", 0))
    size = float(entry_result.get("size", 0))

    # Create DSL state file
    dsl_state = _create_dsl_state(
        asset=signal["asset"],
        direction=signal["direction"],
        leverage=leverage,
        entry_price=entry_price,
        size=size,
        wallet=target_strategy.get("wallet"),
        strategy_id=target_strategy.get("strategyId"),
        strategy_key=target_strategy["_key"],
    )
    state_dir = get_strategy_state_dir(target_strategy["_key"])
    save_json(state_dir / f"dsl-{signal['asset']}.json", dsl_state)

    # Record in trade journal
    record_trade({
        "action": "OPEN",
        "asset": signal["asset"],
        "direction": signal["direction"],
        "entryPrice": entry_price,
        "size": size,
        "margin": margin,
        "leverage": leverage,
        "strategyKey": target_strategy["_key"],
        "entrySource": f"auto-{signal['signalType']}",
        "signal": signal,
    })

    # Also mark in pending entries for Oz review
    add_pending_entry({
        **signal,
        "autoEntered": True,
        "strategyKey": target_strategy["_key"],
        "entryPrice": entry_price,
        "margin": margin,
        "leverage": leverage,
    })

    send_telegram(
        f"🐺 AUTO-ENTRY: {signal['direction']} {signal['asset']}\n"
        f"Signal: {signal['signalType']} ({', '.join(signal['reasons'])})\n"
        f"Entry: ${entry_price:.4f} | Margin: ${margin:.0f} | Lev: {leverage}x\n"
        f"Strategy: {target_strategy.get('name', target_strategy['_key'])}"
    )


def _create_dsl_state(*, asset, direction, leverage, entry_price, size,
                       wallet, strategy_id, strategy_key) -> dict:
    """Create a DSL-Tight state file for a new position."""
    return {
        "active": True,
        "asset": asset,
        "direction": direction,
        "leverage": leverage,
        "entryPrice": entry_price,
        "size": size,
        "wallet": wallet,
        "strategyId": strategy_id,
        "strategyKey": strategy_key,
        "phase": 1,
        "phase1": {
            "retraceThreshold": 0.05,
            "consecutiveBreachesRequired": 3,
        },
        "phase2TriggerTier": 0,
        "phase2": {
            "retraceThreshold": 0.015,
            "consecutiveBreachesRequired": 3,
        },
        "tiers": [
            {"triggerPct": 5,  "lockPct": 2.5,  "retrace": 0.015, "breachesRequired": 2},
            {"triggerPct": 10, "lockPct": 6.5,   "retrace": 0.012, "breachesRequired": 2},
            {"triggerPct": 15, "lockPct": 11.25, "retrace": 0.010, "breachesRequired": 2},
            {"triggerPct": 20, "lockPct": 17.0,  "retrace": 0.006, "breachesRequired": 1},
        ],
        "breachDecay": "hard",
        "stagnation": {
            "enabled": True,
            "minRoePct": 8,
            "maxStaleSec": 3600,
        },
        "currentTierIndex": -1,
        "tierFloorPrice": None,
        "highWaterPrice": entry_price,
        "floorPrice": None,
        "currentBreachCount": 0,
        "phase1MaxDurationSec": 5400,  # 90 minutes
        "createdAt": now_iso(),
    }


def main():
    if not acquire_lock("emerging-movers"):
        return  # Previous run still active

    try:
        git_pull()

        # Fetch leaderboard
        markets = fetch_leaderboard()
        if not markets:
            log("No leaderboard data — skipping")
            return

        # Load scan history
        history = load_json(SCAN_HISTORY_FILE, default=[])

        # Detect signals
        signals = detect_signals(markets, history)

        # Save current scan to history
        history.append({
            "timestamp": now_iso(),
            "markets": markets[:50],
        })
        # Trim to MAX_SCAN_HISTORY
        if len(history) > MAX_SCAN_HISTORY:
            history = history[-MAX_SCAN_HISTORY:]
        save_json(SCAN_HISTORY_FILE, history)

        if not signals:
            return  # Silent — no alerts

        # Log signals
        for sig in signals:
            log(f"Signal: {sig['signalType']} {sig['direction']} {sig['asset']} "
                f"rank={sig['rank']} reasons={sig['reasons']} "
                f"vel={sig['contribVelocity']:.3f}")

        # Process signals by priority
        immediate_signals = [s for s in signals
                            if s["signalType"] in ("FIRST_JUMP", "CONTRIB_EXPLOSION")
                            and not s["erratic"]
                            and not s.get("lowVelocity", False)]

        # Auto-enter on highest-priority signals
        if is_entries_allowed() and immediate_signals:
            for sig in immediate_signals[:2]:  # Max 2 auto-entries per scan
                try_auto_entry(sig)

        # Queue remaining signals for Oz evaluation
        for sig in signals:
            if sig.get("signalType") in ("DEEP_CLIMBER", "NEW_ENTRY_DEEP"):
                add_pending_entry({**sig, "autoEntered": False})

        git_sync("auto: EM scan")

    finally:
        release_lock("emerging-movers")


if __name__ == "__main__":
    main()
