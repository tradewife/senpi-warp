#!/usr/bin/env python3
"""
DSL Runner — High-Water Trailing Stop Manager.

Runs every 3 minutes via APScheduler. Iterates all active DSL state files
across all strategies and enforces the high-water stop logic:

  Phase 1: Absolute floor + hard timeout + weak-peak stagnation cut + dead weight
  Phase 2: Tiered high-water lock (% of peak ROE → floor price)
  Stagnation TP: Take profit if ROE > threshold but HW stale
  HL SL Sync: Sets real stop-loss orders on Hyperliquid via edit_position

No LLM. Pure mechanical stop management. Uses senpi_common.py exclusively.
"""

import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, log, now_iso,
    load_json, save_json,
    get_enabled_strategies, get_strategy_state_dir, get_open_positions,
    mcporter_call, send_telegram, record_trade, git_sync,
)


def get_current_price(asset: str, dex: str = "") -> float | None:
    """Fetch current price for an asset via mcporter."""
    asset_name = f"{dex}:{asset}" if dex else asset
    result = mcporter_call("market_get_asset_data", {"asset": asset_name}, timeout=15)
    if "error" in result:
        return None
    data = result.get("data", result)
    # Try common response shapes
    price = data.get("markPrice", data.get("price", data.get("lastPrice")))
    if price is not None:
        return float(price)
    # Try nested under market
    market = data.get("market", {})
    price = market.get("markPrice", market.get("price"))
    if price is not None:
        return float(price)
    return None


def compute_roe(entry_price: float, current_price: float, direction: str, leverage: float) -> float:
    """Compute ROE percentage: (price_change / entry) * leverage * 100."""
    if entry_price <= 0:
        return 0
    if direction.upper() == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price
    else:  # SHORT
        pnl_pct = (entry_price - current_price) / entry_price
    return pnl_pct * leverage * 100


def compute_floor_price(entry_price: float, floor_roe: float, direction: str, leverage: float) -> float:
    """Convert an ROE-based floor into an actual price level for HL SL orders.

    floor_roe is in percentage (e.g. -20 means -20% ROE).
    """
    if leverage <= 0:
        return entry_price
    # ROE = (price_change / entry) * leverage * 100
    # price_change = ROE * entry / (leverage * 100)
    price_delta = floor_roe * entry_price / (leverage * 100)
    if direction.upper() == "LONG":
        return entry_price + price_delta
    else:  # SHORT
        return entry_price - price_delta


def sync_hl_stop_loss(dsl_state: dict, floor_price: float, phase: int):
    """Set a real stop-loss order on Hyperliquid via edit_position.

    Per DSL v5.3.1: Phase 1 uses MARKET SL (fast exit on loss),
    Phase 2 uses LIMIT SL (fee-optimized exit on profit).
    """
    asset = dsl_state.get("asset", "")
    strategy_id = dsl_state.get("strategyId", "")
    sl_type = "MARKET" if phase == 1 else "LIMIT"

    result = mcporter_call("edit_position", {
        "strategyId": strategy_id,
        "asset": asset,
        "stopLossPrice": round(floor_price, 6),
        "slOrderType": sl_type,
    }, timeout=15)

    if "error" in result:
        log(f"DSL HL SL sync failed for {asset}: {result['error']}")
    else:
        dsl_state["lastSlPrice"] = round(floor_price, 6)
        dsl_state["lastSlType"] = sl_type
        dsl_state["lastSlSyncAt"] = now_iso()


def close_position(dsl_state: dict, reason: str, current_price: float, roe: float):
    """Close position via mcporter and deactivate DSL state."""
    asset = dsl_state["asset"]
    strategy_id = dsl_state.get("strategyId")

    log(f"DSL CLOSE: {asset} reason={reason} roe={roe:.1f}% price={current_price:.4f}")

    # Call mcporter to close
    mcporter_call("strategy_close_position", {
        "strategyId": strategy_id,
        "asset": asset,
    }, timeout=15)

    # Update DSL state
    dsl_state["active"] = False
    dsl_state["closedAt"] = now_iso()
    dsl_state["closeReason"] = reason
    dsl_state["closePrice"] = current_price
    dsl_state["closeRoe"] = round(roe, 2)
    save_json(Path(dsl_state["_file"]), dsl_state)

    # Record trade
    record_trade({
        "action": "CLOSE",
        "asset": asset,
        "direction": dsl_state.get("direction", ""),
        "entryPrice": dsl_state.get("entryPrice", 0),
        "closePrice": current_price,
        "size": dsl_state.get("size", 0),
        "leverage": dsl_state.get("leverage", 0),
        "strategyKey": dsl_state.get("strategyKey", ""),
        "entrySource": dsl_state.get("entryMode", "unknown"),
        "entryScore": dsl_state.get("entryScore", 0),
        "entryMode": dsl_state.get("entryMode", ""),
        "closeReason": reason,
        "realizedPnl": 0,  # Actual PnL calculated by broker
        "entryCreatedAt": dsl_state.get("createdAt", ""),
        "closedAt": dsl_state["closedAt"],
        "highWaterRoe": round(compute_roe(
            dsl_state.get("entryPrice", 0),
            dsl_state.get("highWaterPrice", current_price),
            dsl_state.get("direction", "LONG"),
            dsl_state.get("leverage", 1),
        ), 2),
        "finalTierIndex": dsl_state.get("currentTierIndex", -1),
    })

    # Telegram
    emoji = "🟢" if roe >= 0 else "🔴"
    send_telegram(
        f"{emoji} DSL CLOSE: {dsl_state.get('direction', '')} {asset}\n"
        f"Reason: {reason}\n"
        f"ROE: {roe:+.1f}% | Entry: ${dsl_state.get('entryPrice', 0):.4f} → ${current_price:.4f}\n"
        f"Mode: {dsl_state.get('entryMode', '?')} | Score: {dsl_state.get('entryScore', '?')}"
    )


def process_phase1(dsl_state: dict, current_price: float, roe: float) -> bool:
    """Phase 1: Absolute floor + hard timeout + weak peak cut.

    Per DSL High Water spec v1.0:
      - Absolute floor: conviction-scaled max ROE loss
      - Hard timeout: close if position hasn't hit phase2TriggerRoe within timeout
      - Weak peak cut: close if peak ROE < 3% and declining at weak peak mark

    Returns True if closed.
    """
    p1 = dsl_state.get("phase1", {})
    created_at = dsl_state.get("createdAt", "")
    phase2_trigger = dsl_state.get("phase2TriggerRoe", 7)

    # Absolute floor — conviction-scaled max ROE loss
    abs_floor_roe = p1.get("absoluteFloorRoe", -20)
    if abs_floor_roe != 0 and roe <= abs_floor_roe:
        close_position(dsl_state, "phase1_floor_breach", current_price, roe)
        return True

    # Hard timeout — spec: close if ROE hasn't reached phase2TriggerRoe within timeout
    hard_timeout = p1.get("hardTimeoutSec", 1800)
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - created).total_seconds()
        if elapsed >= hard_timeout and roe < phase2_trigger:
            close_position(dsl_state, "phase1_hard_timeout", current_price, roe)
            return True
    except (ValueError, TypeError):
        pass

    # Weak peak cut — spec: close if peak ROE < 3% and declining at weak peak mark
    weak_peak_sec = p1.get("weakPeakCutSec", 900)
    hw_updated = dsl_state.get("highWaterUpdatedAt", created_at)
    try:
        hw_time = datetime.fromisoformat(hw_updated.replace("Z", "+00:00"))
        hw_stale = (datetime.now(timezone.utc) - hw_time).total_seconds()
        entry_price = dsl_state.get("entryPrice", 0)
        direction = dsl_state.get("direction", "LONG")
        leverage = dsl_state.get("leverage", 1)
        hw_price = dsl_state.get("highWaterPrice", entry_price)
        peak_roe = compute_roe(entry_price, hw_price, direction, leverage)

        if hw_stale >= weak_peak_sec and peak_roe < 3 and roe < peak_roe:
            close_position(dsl_state, "phase1_weak_peak_cut", current_price, roe)
            return True
    except (ValueError, TypeError):
        pass

    return False


def process_dead_weight(dsl_state: dict, current_price: float, roe: float) -> bool:
    """Dead weight cut: close if position has NEVER gone positive after configured time.

    Per VIXEN spec: positions that have negative ROE their entire lifetime
    are cut after deadWeightCutMin (10-20 min depending on score).
    Returns True if closed.
    """
    p1 = dsl_state.get("phase1", {})
    dead_weight_min = p1.get("deadWeightCutMin", 0)
    if dead_weight_min <= 0:
        return False

    # Only trigger if ROE has never been positive (high water never exceeded entry)
    entry_price = dsl_state.get("entryPrice", 0)
    hw_price = dsl_state.get("highWaterPrice", entry_price)
    direction = dsl_state.get("direction", "LONG").upper()
    leverage = dsl_state.get("leverage", 1)
    peak_roe = compute_roe(entry_price, hw_price, direction, leverage)

    if peak_roe > 1:  # Ever went meaningfully positive — not dead weight
        return False

    # Check age
    created_at = dsl_state.get("createdAt", "")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
        if age_min >= dead_weight_min and roe < 0:
            close_position(dsl_state, "phase1_dead_weight", current_price, roe)
            return True
    except (ValueError, TypeError):
        pass

    return False


def process_phase2(dsl_state: dict, current_price: float, roe: float) -> bool:
    """Phase 2: Tiered high-water lock. Returns True if closed."""
    tiers = dsl_state.get("tiers", [])
    current_tier = dsl_state.get("currentTierIndex", -1)
    entry_price = dsl_state.get("entryPrice", 0)
    direction = dsl_state.get("direction", "LONG").upper()
    leverage = dsl_state.get("leverage", 1)
    hw_price = dsl_state.get("highWaterPrice", entry_price)

    hw_roe = compute_roe(entry_price, hw_price, direction, leverage)

    # Walk up tiers
    new_tier = current_tier
    for i, tier in enumerate(tiers):
        if i > current_tier and hw_roe >= tier["triggerPct"]:
            new_tier = i

    if new_tier > current_tier:
        dsl_state["currentTierIndex"] = new_tier
        dsl_state["currentBreachCount"] = 0
        log(f"DSL {dsl_state['asset']}: tier up → {new_tier} (hw_roe={hw_roe:.1f}%)")

    # Check floor breach at current tier
    if new_tier >= 0 and new_tier < len(tiers):
        tier = tiers[new_tier]
        lock_pct = tier["lockHwPct"] / 100
        floor_roe = hw_roe * lock_pct

        if roe < floor_roe:
            breach_count = dsl_state.get("currentBreachCount", 0) + 1
            dsl_state["currentBreachCount"] = breach_count
            required = tier.get("consecutiveBreachesRequired", 1)

            if breach_count >= required:
                close_position(dsl_state, f"dsl_breach_tier{new_tier}", current_price, roe)
                return True
            else:
                log(f"DSL {dsl_state['asset']}: breach {breach_count}/{required} at tier {new_tier}")
        else:
            dsl_state["currentBreachCount"] = 0

    return False


def process_stagnation_tp(dsl_state: dict, current_price: float, roe: float) -> bool:
    """Take profit if ROE > threshold but high-water hasn't moved. Returns True if closed."""
    stp = dsl_state.get("stagnationTp", {})
    if not stp.get("enabled", False):
        return False

    roe_min = stp.get("roeMin", 10)
    hw_stale_min = stp.get("hwStaleMin", 45)

    if roe < roe_min:
        return False

    hw_updated = dsl_state.get("highWaterUpdatedAt", dsl_state.get("createdAt", ""))
    try:
        hw_time = datetime.fromisoformat(hw_updated.replace("Z", "+00:00"))
        stale_minutes = (datetime.now(timezone.utc) - hw_time).total_seconds() / 60
        if stale_minutes >= hw_stale_min:
            close_position(dsl_state, "stagnation_tp", current_price, roe)
            return True
    except (ValueError, TypeError):
        pass

    return False


def process_position(dsl_state: dict):
    """Run DSL logic on a single active position."""
    asset = dsl_state["asset"]
    dex = dsl_state.get("dex", "")
    entry_price = dsl_state.get("entryPrice", 0)
    direction = dsl_state.get("direction", "LONG")
    leverage = dsl_state.get("leverage", 1)

    current_price = get_current_price(asset, dex)
    if current_price is None:
        log(f"DSL: could not fetch price for {asset} — skipping")
        return

    roe = compute_roe(entry_price, current_price, direction, leverage)

    # Update high water
    hw_price = dsl_state.get("highWaterPrice", entry_price)
    if direction.upper() == "LONG" and current_price > hw_price:
        dsl_state["highWaterPrice"] = current_price
        dsl_state["highWaterUpdatedAt"] = now_iso()
    elif direction.upper() == "SHORT" and current_price < hw_price:
        dsl_state["highWaterPrice"] = current_price
        dsl_state["highWaterUpdatedAt"] = now_iso()

    phase = dsl_state.get("phase", 1)
    phase2_trigger = dsl_state.get("phase2TriggerRoe", 5)

    # Check if we should transition to Phase 2
    if phase == 1 and roe >= phase2_trigger:
        dsl_state["phase"] = 2
        log(f"DSL {asset}: Phase 1 → Phase 2 (roe={roe:.1f}%)")

    # Run phase logic
    current_phase = dsl_state.get("phase", 1)
    if current_phase == 1:
        if process_phase1(dsl_state, current_price, roe):
            return
        # Dead weight cut (Phase 1 only)
        if process_dead_weight(dsl_state, current_price, roe):
            return
        # Compute Phase 1 floor price for HL SL sync
        p1 = dsl_state.get("phase1", {})
        abs_floor_roe = p1.get("absoluteFloorRoe", -20)
        if abs_floor_roe != 0:
            floor_price = compute_floor_price(entry_price, abs_floor_roe, direction, leverage)
            sync_hl_stop_loss(dsl_state, floor_price, phase=1)
    else:
        if process_phase2(dsl_state, current_price, roe):
            return
        # Compute Phase 2 floor price for HL SL sync
        tiers = dsl_state.get("tiers", [])
        tier_idx = dsl_state.get("currentTierIndex", -1)
        if 0 <= tier_idx < len(tiers):
            tier = tiers[tier_idx]
            lock_pct = tier["lockHwPct"] / 100
            hw_roe = compute_roe(entry_price, hw_price, direction, leverage)
            floor_roe = hw_roe * lock_pct
            floor_price = compute_floor_price(entry_price, floor_roe, direction, leverage)
            sync_hl_stop_loss(dsl_state, floor_price, phase=2)

    # Stagnation TP (any phase)
    if process_stagnation_tp(dsl_state, current_price, roe):
        return

    # Save updated state
    save_json(Path(dsl_state["_file"]), dsl_state)


def main():
    if not acquire_lock("dsl-runner"):
        return

    try:
        strategies = get_enabled_strategies()
        if not strategies:
            return

        position_count = 0
        for strat in strategies:
            positions = get_open_positions(strat["_key"])
            for pos in positions:
                position_count += 1
                try:
                    process_position(pos)
                except Exception as e:
                    log(f"DSL error on {pos.get('asset', '?')}: {e}")

        if position_count > 0:
            git_sync("auto: DSL runner")

    finally:
        release_lock("dsl-runner")


if __name__ == "__main__":
    main()
