#!/usr/bin/env python3
"""
Watchdog — Margin/Liquidation Monitor. Runs every 5 minutes via APScheduler.

Monitors all open positions for dangerous margin conditions:
  - Liquidation distance < 30% → warning alert
  - Liquidation distance < 15% → emergency close
  - ROE < -15% → warning alert (DSL should catch, but watchdog is backup)

Native Python implementation using senpi_common.py.
No dependency on senpi-skills or OpenClaw.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from senpi_common import (
    acquire_lock, release_lock, log, now_iso,
    load_json, save_json,
    get_enabled_strategies, get_open_positions,
    mcporter_call, send_telegram, record_trade, git_sync,
    record_heartbeat,
)

# Watchdog thresholds
LIQ_WARN_PCT = 30       # Alert if liq distance < 30%
LIQ_EMERGENCY_PCT = 15  # Emergency close if liq distance < 15%
ROE_WARN_PCT = -15       # Alert if ROE < -15%


def get_portfolio_positions() -> list[dict]:
    """Fetch live position data from broker via mcporter."""
    result = mcporter_call("account_get_portfolio", {"strategyStatus": "ACTIVE"}, timeout=15)
    if "error" in result:
        log(f"Watchdog: portfolio fetch failed: {result['error']}")
        return []

    data = result.get("data", result)
    positions = data.get("positions", data.get("openPositions", []))
    if isinstance(positions, dict):
        positions = positions.get("positions", [])
    return positions if isinstance(positions, list) else []


def find_live_position(live_positions: list[dict], asset: str) -> dict | None:
    """Match a DSL state to its live broker position."""
    for p in live_positions:
        token = p.get("token", p.get("asset", p.get("coin", "")))
        if token == asset:
            return p
    return None


def emergency_close(dsl_state: dict, reason: str):
    """Emergency close a position."""
    asset = dsl_state["asset"]
    strategy_id = dsl_state.get("strategyId")

    log(f"WATCHDOG EMERGENCY CLOSE: {asset} reason={reason}")

    mcporter_call("strategy_close_position", {
        "strategyId": strategy_id,
        "asset": asset,
    }, timeout=15)

    dsl_state["active"] = False
    dsl_state["closedAt"] = now_iso()
    dsl_state["closeReason"] = f"watchdog_{reason}"
    save_json(Path(dsl_state["_file"]), dsl_state)

    record_trade({
        "action": "CLOSE",
        "asset": asset,
        "direction": dsl_state.get("direction", ""),
        "entryPrice": dsl_state.get("entryPrice", 0),
        "closePrice": 0,
        "size": dsl_state.get("size", 0),
        "leverage": dsl_state.get("leverage", 0),
        "strategyKey": dsl_state.get("strategyKey", ""),
        "entrySource": dsl_state.get("entryMode", "unknown"),
        "entryScore": dsl_state.get("entryScore", 0),
        "entryMode": dsl_state.get("entryMode", ""),
        "closeReason": f"watchdog_{reason}",
        "realizedPnl": 0,
        "entryCreatedAt": dsl_state.get("createdAt", ""),
        "closedAt": dsl_state["closedAt"],
        "highWaterRoe": 0,
        "finalTierIndex": dsl_state.get("currentTierIndex", -1),
    })

    send_telegram(
        f"🚨 WATCHDOG EMERGENCY: {asset}\n"
        f"Reason: {reason}\n"
        f"Position force-closed.\n"
        f"Strategy: {dsl_state.get('strategyKey', '?')}"
    )


def main():
    if not acquire_lock("watchdog"):
        return

    try:
        record_heartbeat("watchdog")
        strategies = get_enabled_strategies()
        if not strategies:
            return

        # Fetch live positions once
        live_positions = get_portfolio_positions()
        if not live_positions and any(get_open_positions(s["_key"]) for s in strategies):
            log("Watchdog: could not fetch live positions but have active DSL states — alerting")
            send_telegram("⚠️ WATCHDOG: Cannot fetch portfolio. Broker API may be down.")
            return

        had_emergency = False
        for strat in strategies:
            positions = get_open_positions(strat["_key"])
            for pos in positions:
                asset = pos.get("asset", "")
                live = find_live_position(live_positions, asset)

                if not live:
                    # DSL says active but broker doesn't have it — stale state
                    log(f"Watchdog: {asset} in DSL but not on broker — marking inactive")
                    pos["active"] = False
                    pos["closedAt"] = now_iso()
                    pos["closeReason"] = "watchdog_ghost_position"
                    save_json(Path(pos["_file"]), pos)
                    continue

                # Check liquidation distance
                liq_price = float(live.get("liquidationPrice", live.get("liqPrice", 0)) or 0)
                mark_price = float(live.get("markPrice", live.get("price", 0)) or 0)

                if liq_price > 0 and mark_price > 0:
                    direction = pos.get("direction", "LONG").upper()
                    if direction == "LONG":
                        liq_distance_pct = (mark_price - liq_price) / mark_price * 100
                    else:
                        liq_distance_pct = (liq_price - mark_price) / mark_price * 100

                    if liq_distance_pct < LIQ_EMERGENCY_PCT:
                        emergency_close(pos, f"liq_distance_{liq_distance_pct:.0f}pct")
                        had_emergency = True
                        continue
                    elif liq_distance_pct < LIQ_WARN_PCT:
                        send_telegram(
                            f"⚠️ WATCHDOG: {asset} liq distance {liq_distance_pct:.1f}%\n"
                            f"Liq: ${liq_price:.4f} | Mark: ${mark_price:.4f}\n"
                            f"Direction: {direction}"
                        )

                # Check ROE
                roe = float(live.get("roe", live.get("roePct", live.get("returnOnEquity", 0))) or 0)
                if roe < ROE_WARN_PCT:
                    send_telegram(
                        f"⚠️ WATCHDOG: {asset} ROE {roe:.1f}%\n"
                        f"Below {ROE_WARN_PCT}% threshold. DSL should be managing this."
                    )

        if had_emergency:
            git_sync("auto: watchdog emergency close")

    finally:
        release_lock("watchdog")


if __name__ == "__main__":
    main()
